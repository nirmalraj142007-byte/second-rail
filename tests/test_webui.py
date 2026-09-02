"""Tests for src/webui/app.py, the read-only companion dashboard.

Covers the phase spec's five required checks, plus one extra: theme.css
stays byte-for-byte in sync with src/ui/theme.py's colour constants (the
whole point of mirroring them into a shared file is that they can't
silently drift apart).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from src.audit.writer import AuditWriter
from src.db.migrate import get_connection, migrate
from src.db.repo import insert_customer_if_absent, insert_episode, start_run
from src.ui.approve import app as approve_cli_app
from src.ui.approve import enqueue_pending
from src.webui.app import DEFAULT_HOST, STATIC_DIR, app

ROOT = Path(__file__).resolve().parent.parent
MAKEFILE = ROOT / "Makefile"
THEME_PY = ROOT / "src" / "ui" / "theme.py"
THEME_CSS = ROOT / "src" / "webui" / "static" / "theme.css"


def _isolate_env(monkeypatch, tmp_path: Path) -> None:
    """Isolate DB, audit dir, and demo/approval_queue.json (a relative
    path, resolved against CWD) for one test."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "second_rail.db"))
    monkeypatch.setenv("AUDIT_DIR", str(tmp_path / "audit"))


def _seed_run_with_episode(
    db_path: Path, *, run_id: str, episode_id: str, payment_id: str, customer_id: str
) -> None:
    migrate(db_path)
    conn = get_connection(db_path)
    try:
        start_run(conn, run_id=run_id, started_at="2026-09-02T12:00:00+05:30", mode="dry_run")
        insert_customer_if_absent(
            conn, customer_id=customer_id, synthetic_name="Test", contact_hash="x",
            email_hash=None, segment="repeat", opted_out=False, opt_out_ts=None,
            created_at="2026-09-01T00:00:00+05:30",
        )
        insert_episode(
            conn, episode_id=episode_id, payment_id=payment_id, order_id=None,
            customer_id=customer_id, amount_paise=123456, currency="INR", instrument="upi",
            issuer_family="BANK_A", error_code="BAD_REQUEST_ERROR", error_description=None,
            error_source=None, error_step=None, error_reason="insufficient_funds",
            failed_at="2026-09-02T11:55:00+05:30", received_at="2026-09-02T11:56:00+05:30",
            split="train", is_synthetic=True, harvested_from=None,
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1. GET /api/runs/latest — 404 with no run, 200 with the right shape
# ---------------------------------------------------------------------------


def test_latest_run_404_then_200(monkeypatch, tmp_path):
    _isolate_env(monkeypatch, tmp_path)
    client = TestClient(app)

    resp = client.get("/api/runs/latest")
    assert resp.status_code == 404

    db_path = tmp_path / "second_rail.db"
    _seed_run_with_episode(
        db_path, run_id="RUN_A", episode_id="ep_a", payment_id="pay_a", customer_id="cust_a"
    )

    resp = client.get("/api/runs/latest")
    assert resp.status_code == 200
    body = resp.json()
    assert body["run_id"] == "RUN_A"
    assert body["mode"] == "dry_run"
    assert "started_at" in body
    assert "config_hash" in body


# ---------------------------------------------------------------------------
# 2. GET /api/runs/{id}/episodes?since=N — only seq > N
# ---------------------------------------------------------------------------


def test_episodes_since_cursor_filters_by_seq(monkeypatch, tmp_path):
    _isolate_env(monkeypatch, tmp_path)
    db_path = tmp_path / "second_rail.db"
    audit_dir = tmp_path / "audit"
    migrate(db_path)
    conn = get_connection(db_path)
    audit = AuditWriter("RUN_B", audit_dir, conn)
    try:
        for i in range(5):
            audit.append(stage="gate", actor="system", episode_id=f"ep_{i}", outcome="pending")
    finally:
        audit.close()
        conn.close()

    client = TestClient(app)
    resp = client.get("/api/runs/RUN_B/episodes?since=-1")
    assert resp.status_code == 200
    all_records = resp.json()
    assert [r["seq"] for r in all_records] == [0, 1, 2, 3, 4]

    resp = client.get("/api/runs/RUN_B/episodes?since=2")
    assert resp.status_code == 200
    filtered = resp.json()
    assert [r["seq"] for r in filtered] == [3, 4]
    assert all(r["seq"] > 2 for r in filtered)

    resp = client.get("/api/runs/RUN_B/episodes?since=4")
    assert resp.json() == []

    # a run_id with no audit file at all -> empty list, not an error
    resp = client.get("/api/runs/NEVER_EXISTED/episodes")
    assert resp.status_code == 200
    assert resp.json() == []


# ---------------------------------------------------------------------------
# 3. POST decide (web) vs the CLI path — byte-identical audit record shape
# ---------------------------------------------------------------------------


def test_web_decide_matches_cli_decide_audit_shape(monkeypatch, tmp_path):
    _isolate_env(monkeypatch, tmp_path)
    db_path = tmp_path / "second_rail.db"
    audit_dir = tmp_path / "audit"

    _seed_run_with_episode(
        db_path, run_id="RUN_PARITY", episode_id="ep_cli", payment_id="pay_cli",
        customer_id="cust_cli",
    )
    conn = get_connection(db_path)
    try:
        insert_customer_if_absent(
            conn, customer_id="cust_web", synthetic_name="Test", contact_hash="y",
            email_hash=None, segment="repeat", opted_out=False, opt_out_ts=None,
            created_at="2026-09-01T00:00:00+05:30",
        )
        insert_episode(
            conn, episode_id="ep_web", payment_id="pay_web", order_id=None,
            customer_id="cust_web", amount_paise=123456, currency="INR", instrument="upi",
            issuer_family="BANK_A", error_code="BAD_REQUEST_ERROR", error_description=None,
            error_source=None, error_step=None, error_reason="insufficient_funds",
            failed_at="2026-09-02T11:55:00+05:30", received_at="2026-09-02T11:56:00+05:30",
            split="train", is_synthetic=True, harvested_from=None,
        )
    finally:
        conn.close()

    # Identical field values on both queue items except episode_id/
    # payment_id, so their audit records should be identical except for
    # exactly the fields the test explicitly excludes.
    common = dict(
        run_id="RUN_PARITY", amount_paise=123456, cause="C1_issuer_decline",
        chosen_action="open_ticket", admissible_actions=["open_ticket", "no_action"],
        gate_reason="amount_paise > auto_approve_ceiling_paise",
    )
    enqueue_pending(episode_id="ep_cli", **common)
    enqueue_pending(episode_id="ep_web", **common)

    # -- CLI path: invoke the actual Typer app, not a re-implementation --
    result = CliRunner().invoke(approve_cli_app, ["--id", "ep_cli"])
    assert result.exit_code == 0, result.output

    # -- web path --
    client = TestClient(app)
    resp = client.post("/api/approvals/ep_web/decide", json={"action": "approve", "reason": None})
    assert resp.status_code == 200
    assert resp.json()["status"] == "approved"

    records = [
        json.loads(line)
        for line in (audit_dir / "RUN_PARITY.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    approve_records = [r for r in records if r["stage"] == "approve"]
    assert len(approve_records) == 2

    def _strip(rec: dict) -> dict:
        excluded = {"event_id", "ts", "seq", "hash", "prev_hash", "episode_id", "payment_id"}
        return {k: v for k, v in rec.items() if k not in excluded}

    cli_record = next(r for r in approve_records if r["episode_id"] == "ep_cli")
    web_record = next(r for r in approve_records if r["episode_id"] == "ep_web")
    assert _strip(cli_record) == _strip(web_record)
    assert cli_record["outcome"] == "approved"
    assert web_record["outcome"] == "approved"


# ---------------------------------------------------------------------------
# 4. Binds to 127.0.0.1, not 0.0.0.0
# ---------------------------------------------------------------------------


def test_webui_binds_localhost_only():
    assert DEFAULT_HOST == "127.0.0.1"

    makefile_text = MAKEFILE.read_text(encoding="utf-8")
    match = re.search(r"^webui:\n(?:\t.*\n?)+", makefile_text, re.MULTILINE)
    assert match is not None, "no `webui:` target found in Makefile"
    target_body = match.group(0)
    assert "--host 127.0.0.1" in target_body
    assert "0.0.0.0" not in target_body


# ---------------------------------------------------------------------------
# 5. Static files served
# ---------------------------------------------------------------------------


def test_static_files_served():
    client = TestClient(app)
    for path, content_type_prefix in [
        ("/", "text/html"),
        ("/app.js", "javascript"),  # Starlette/mimetypes serves .js as application/javascript
        ("/style.css", "text/css"),
        ("/theme.css", "text/css"),
    ]:
        resp = client.get(path)
        assert resp.status_code == 200, f"{path} -> {resp.status_code}"
        assert content_type_prefix in resp.headers["content-type"], (
            f"{path}: {resp.headers['content-type']!r}"
        )


def test_static_dir_actually_contains_the_expected_files():
    for name in ("index.html", "app.js", "style.css", "theme.css"):
        assert (STATIC_DIR / name).exists(), f"missing {name}"


# ---------------------------------------------------------------------------
# bonus: theme.css never silently drifts from src/ui/theme.py
# ---------------------------------------------------------------------------


def test_theme_css_matches_theme_py_constants():
    py_text = THEME_PY.read_text(encoding="utf-8")
    css_text = THEME_CSS.read_text(encoding="utf-8")

    py_values = dict(re.findall(r'^([A-Z_]+)\s*=\s*"(#[0-9A-Fa-f]{6})"', py_text, re.MULTILINE))
    css_values = dict(re.findall(r"--([a-z-]+):\s*(#[0-9A-Fa-f]{6});", css_text))

    mapping = {
        "CABIN": "cabin",
        "CHALK": "chalk",
        "BRASS": "brass",
        "SIGNAL_RED": "signal-red",
        "SIGNAL_AMBER": "signal-amber",
        "SIGNAL_GREEN": "signal-green",
        "TELEGRAPH_CYAN": "telegraph-cyan",
        "SLATE_DIM": "slate-dim",
    }
    assert py_values, "no colour constants found in theme.py — regex may be stale"
    assert css_values, "no CSS custom properties found in theme.css — regex may be stale"
    for py_name, css_name in mapping.items():
        assert py_name in py_values, f"{py_name} missing from theme.py"
        assert css_name in css_values, f"--{css_name} missing from theme.css"
        assert py_values[py_name].lower() == css_values[css_name].lower(), (
            f"{py_name}={py_values[py_name]} but --{css_name}={css_values[css_name]}"
        )
