from __future__ import annotations

import hashlib
import hmac
import re
from pathlib import Path

from fastapi.testclient import TestClient

from src.db.migrate import get_connection
from src.ingest.app import app, drain

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "webhooks"
SECRET = "test_webhook_secret"


def _sign(body: bytes) -> str:
    return hmac.new(SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _setup_env(monkeypatch, tmp_path) -> Path:
    db_path = tmp_path / "second_rail.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", SECRET)
    return db_path


def _post(client: TestClient, filename: str, event_id: str, signature: str | None = None):
    body = (FIXTURES / filename).read_bytes()
    sig = signature if signature is not None else _sign(body)
    headers = {
        "Content-Type": "application/json",
        "X-Razorpay-Signature": sig,
        "X-Razorpay-Event-Id": event_id,
    }
    response = client.post("/webhooks/razorpay", content=body, headers=headers)
    drain()
    return response


def test_valid_signature_creates_one_episode_dedup_new(monkeypatch, tmp_path):
    db_path = _setup_env(monkeypatch, tmp_path)
    with TestClient(app) as client:
        response = _post(client, "payment_failed.json", "evt_001")
        assert response.status_code == 200

        conn = get_connection(db_path)
        episodes = conn.execute("SELECT * FROM episode").fetchall()
        assert len(episodes) == 1
        assert episodes[0]["payment_id"] == "pay_TU6NMPiyJVkobn"
        assert episodes[0]["is_synthetic"] == 0

        events = conn.execute("SELECT * FROM webhook_event").fetchall()
        assert len(events) == 1
        assert events[0]["dedup_result"] == "new"
        conn.close()


def test_replaying_same_event_id_creates_no_new_episode_or_row(monkeypatch, tmp_path):
    # Razorpay's own docs confirm a retried delivery of the same webhook
    # carries the SAME X-Razorpay-Event-Id — this is a literal PK collision
    # against webhook_event, not a fresh row with dedup_result="duplicate".
    # See BUILD_LOG.md.
    db_path = _setup_env(monkeypatch, tmp_path)
    with TestClient(app) as client:
        first = _post(client, "payment_failed.json", "evt_001")
        second = _post(client, "payment_failed.json", "evt_001")
        assert first.status_code == 200
        assert second.status_code == 200

        conn = get_connection(db_path)
        assert conn.execute("SELECT COUNT(*) AS n FROM episode").fetchone()["n"] == 1
        assert conn.execute("SELECT COUNT(*) AS n FROM webhook_event").fetchone()["n"] == 1
        conn.close()


def test_same_payment_id_different_event_id_is_duplicate(monkeypatch, tmp_path):
    db_path = _setup_env(monkeypatch, tmp_path)
    with TestClient(app) as client:
        first = _post(client, "payment_failed.json", "evt_001")
        second = _post(client, "payment_failed_duplicate.json", "evt_002")
        assert first.status_code == 200
        assert second.status_code == 200

        conn = get_connection(db_path)
        assert conn.execute("SELECT COUNT(*) AS n FROM episode").fetchone()["n"] == 1
        rows = conn.execute("SELECT dedup_result FROM webhook_event ORDER BY rowid").fetchall()
        assert [r["dedup_result"] for r in rows] == ["new", "duplicate"]
        conn.close()


def test_captured_with_no_prior_failed_is_out_of_order(monkeypatch, tmp_path):
    db_path = _setup_env(monkeypatch, tmp_path)
    with TestClient(app) as client:
        response = _post(client, "payment_captured_orphan.json", "evt_003")
        assert response.status_code == 200

        conn = get_connection(db_path)
        assert conn.execute("SELECT COUNT(*) AS n FROM episode").fetchone()["n"] == 0
        row = conn.execute("SELECT dedup_result FROM webhook_event").fetchone()
        assert row["dedup_result"] == "out_of_order"
        conn.close()


def test_bad_signature_returns_400_and_writes_nothing(monkeypatch, tmp_path):
    db_path = _setup_env(monkeypatch, tmp_path)
    with TestClient(app) as client:
        response = _post(client, "payment_failed.json", "evt_004", signature="0" * 64)
        assert response.status_code == 400
        assert response.json() == {"error": "signature_invalid"}

        conn = get_connection(db_path)
        assert conn.execute("SELECT COUNT(*) AS n FROM episode").fetchone()["n"] == 0
        assert (
            conn.execute(
                "SELECT COUNT(*) AS n FROM webhook_event WHERE signature_valid = 1"
            ).fetchone()["n"]
            == 0
        )
        conn.close()


def test_malformed_missing_error_code_routes_to_exception_entry_not_500(monkeypatch, tmp_path):
    db_path = _setup_env(monkeypatch, tmp_path)
    with TestClient(app) as client:
        response = _post(client, "malformed_missing_error_code.json", "evt_005")
        assert response.status_code == 200

        conn = get_connection(db_path)
        assert conn.execute("SELECT COUNT(*) AS n FROM episode").fetchone()["n"] == 0
        exceptions = conn.execute("SELECT * FROM exception_entry").fetchall()
        assert len(exceptions) == 1
        assert exceptions[0]["stage"] == "ingest"
        assert exceptions[0]["reason_code"] == "SCHEMA_DRIFT_FIELD_MISSING"
        conn.close()


def test_oversized_body_returns_413_and_writes_nothing(monkeypatch, tmp_path):
    db_path = _setup_env(monkeypatch, tmp_path)
    with TestClient(app) as client:
        oversized = b'{"pad": "' + b"a" * (256 * 1024 + 1) + b'"}'
        response = client.post(
            "/webhooks/razorpay",
            content=oversized,
            headers={
                "Content-Type": "application/json",
                "X-Razorpay-Signature": _sign(oversized),
                "X-Razorpay-Event-Id": "evt_big",
            },
        )
        drain()
        assert response.status_code == 413
        assert response.json() == {"error": "payload_too_large"}

        conn = get_connection(db_path)
        assert conn.execute("SELECT COUNT(*) AS n FROM episode").fetchone()["n"] == 0
        assert conn.execute("SELECT COUNT(*) AS n FROM webhook_event").fetchone()["n"] == 0
        conn.close()


def test_non_json_content_type_returns_400_and_writes_nothing(monkeypatch, tmp_path):
    db_path = _setup_env(monkeypatch, tmp_path)
    with TestClient(app) as client:
        body = (FIXTURES / "payment_failed.json").read_bytes()
        response = client.post(
            "/webhooks/razorpay",
            content=body,
            headers={
                "Content-Type": "text/plain",
                "X-Razorpay-Signature": _sign(body),
                "X-Razorpay-Event-Id": "evt_wrong_type",
            },
        )
        drain()
        assert response.status_code == 400
        assert response.json() == {"error": "unsupported_content_type"}

        conn = get_connection(db_path)
        assert conn.execute("SELECT COUNT(*) AS n FROM episode").fetchone()["n"] == 0
        assert conn.execute("SELECT COUNT(*) AS n FROM webhook_event").fetchone()["n"] == 0
        conn.close()


def test_signature_comparison_is_constant_time(monkeypatch, tmp_path):
    # Not a timing measurement (too flaky in CI) -- asserts the actual
    # comparison call site uses hmac.compare_digest rather than `==`, which
    # is the property that makes it constant-time. See
    # src/ingest/signature.py's module docstring for why this matters.
    import inspect

    from src.ingest import signature as signature_module

    source = inspect.getsource(signature_module.verify_signature)
    assert "hmac.compare_digest" in source
    assert re.search(r"expected\s*==\s*header_signature", source) is None


def test_health_ok(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    with TestClient(app) as client:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok", "run_id": None, "db": "ok"}
