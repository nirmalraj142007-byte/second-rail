"""Read-only web dashboard — a second window into the same data
src/ui/live.py's terminal renders during `make demo`, nothing more.

Deliberately a SEPARATE FastAPI app/process from src/ingest/app.py (the
public webhook receiver): that app is meant to be reachable through the
cloudflared tunnel from Razorpay's servers, on port 8000. This one has a
write action (approve/reject) and binds to 127.0.0.1 only, on a different
port (8001) — so it is structurally never reachable through the same
tunnel, not merely "not supposed to be." Two processes, two ports, two
trust boundaries.

Every read here opens its OWN short-lived, genuinely read-only SQLite
connection (`file:...?mode=ro` — SQLite refuses any write against a
connection opened this way, at the driver level, not by convention) rather
than sharing one connection on `app.state`: sqlite3 connections are not
safe to use across threads (`check_same_thread` defaults True, unchanged
here — see src/ingest/app.py's own comment on the identical constraint),
and FastAPI runs sync path-operation functions in a thread pool, so a
shared connection would eventually be used from the wrong thread. The one
write path (POST /api/approvals/{episode_id}/decide) opens its own
separate, ordinary connection, scoped to that single request, and calls
src/approve_actions.py's decide_approval() — the exact same function
src/ui/approve.py's CLI calls — so this file never re-implements what
"approving an episode" means.

Episode-stream and run-summary data come from evidence/audit/*.jsonl,
tailed directly (re-read and filtered by seq on every request) — no new
database, no new table, matching this phase's strict scope. The approval
queue comes from demo/approval_queue.json via src.ui.approve's own
load_queue()/mark_expired()/save_queue(), reused rather than re-read
independently, so there is exactly one parser for that file's shape too.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from src.approve_actions import decide_approval
from src.audit.writer import AuditWriter
from src.config import Settings, load_settings
from src.db.migrate import get_connection, migrate
from src.ui.approve import DEFAULT_QUEUE_PATH, load_queue, mark_expired, save_queue

STATIC_DIR = Path(__file__).parent / "static"

# The Makefile's `webui` target is the actual enforcement point for this —
# `--host 127.0.0.1` is passed there, not inferred from this module — but
# these constants exist so a `python -m src.webui.app` invocation (and
# tests/test_webui.py) have one place to check the intended default rather
# than a string only living in the Makefile.
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8001

app = FastAPI(title="Second Rail dashboard (read-only + approve)")


# ---------------------------------------------------------------------------
# connections
# ---------------------------------------------------------------------------


def _settings() -> Settings:
    return load_settings()


def _read_conn(settings: Settings) -> sqlite3.Connection:
    """A genuinely read-only connection: `mode=ro` in the URI makes SQLite
    itself refuse any write, so a bug in one of the read endpoints below
    can never mutate second_rail.db — this isn't enforced by code review
    discipline, it's enforced by the database driver."""
    db_path = Path(settings.db_path)
    uri = f"file:{db_path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _write_conn(settings: Settings) -> sqlite3.Connection:
    """The one connection in this file that can write — scoped to a single
    request in the approval-decision endpoint only."""
    migrate(settings.db_path)
    return get_connection(settings.db_path)


# ---------------------------------------------------------------------------
# audit-trail tailing (evidence/audit/{run_id}.jsonl)
# ---------------------------------------------------------------------------


def _audit_path(settings: Settings, run_id: str) -> Path:
    return Path(settings.audit_dir) / f"{run_id}.jsonl"


def _tail_audit_records(settings: Settings, run_id: str, since: int = -1) -> list[dict[str, Any]]:
    """Every audit record for `run_id` with seq > since, in file order.
    Re-reads the whole file each call rather than tracking a byte offset —
    simple and correct for this phase's polling-only scope (files here run
    to a few hundred/thousand lines, not millions); a truncated last line
    (a crash mid-write) is tolerated and skipped, same as
    src/audit/verify.py's own streaming reader."""
    path = _audit_path(settings, run_id)
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            seq = record.get("seq")
            if isinstance(seq, int) and seq > since:
                records.append(record)
    return records


# ---------------------------------------------------------------------------
# run summary — reconstructed from the audit trail + `run`/`ledger_entry`,
# since RunSummary itself is only ever an in-memory object (src/runner.py),
# never persisted anywhere queryable.
# ---------------------------------------------------------------------------

# (stage, outcome) -> RunSummary.by_outcome bucket, read directly off every
# outcome= value src/runner.py actually writes (see that module — every
# `self._audit.append(stage=..., outcome=...)` call site is represented
# here). ("approve", "approved") maps to "pending" only as a defensive
# fallback for the brief window between that record landing and the
# "execute" record that always follows it in the same Runner.run()
# iteration — in practice the later "execute" record supersedes it, since
# records are applied in seq order below.
_TERMINAL_BUCKET: dict[tuple[str | None, str | None], str] = {
    ("gate", "suppressed"): "suppressed",
    ("gate", "pending"): "pending",
    ("execute", "execution_failed"): "execution_failed",
    ("execute", "created"): "actioned",
    ("execute", "duplicate_suppressed"): "pending",
    ("execute", "cancelled"): "pending",
    ("execute", "suppressed"): "suppressed",
    ("execute", "pending"): "pending",
    ("approve", "approved"): "pending",
    ("approve", "rejected"): "suppressed",
    ("approve", "skip"): "suppressed",
    ("approve", "approval_timeout"): "suppressed",
}


def _reconstruct_by_outcome(
    records: list[dict[str, Any]], episode_count: int
) -> dict[str, int]:
    """The LAST applicable bucket per episode_id, in seq order (records
    are already file-ordered, and seq is monotonic) — an episode that
    reached "approve"/"approved" and then "execute"/"created" ends up
    counted "actioned", matching src/runner.py's real accounting.
    Episodes with no audit record at all (the run stopped before reaching
    them — a kill switch, cap breach, or consecutive-error stop) are
    counted "pending" via episode_count, exactly like
    src/runner.py's own `pending_unreached` line."""
    bucket_by_episode: dict[str, str] = {}
    for rec in records:
        episode_id = rec.get("episode_id")
        if not episode_id:
            continue
        bucket = _TERMINAL_BUCKET.get((rec.get("stage"), rec.get("outcome")))
        if bucket is not None:
            bucket_by_episode[episode_id] = bucket

    counts = Counter(bucket_by_episode.values())
    unreached = max(0, episode_count - len(bucket_by_episode))
    counts["pending"] = counts.get("pending", 0) + unreached
    return dict(counts)


def _admissibility_rate(records: list[dict[str, Any]]) -> float | None:
    """Always 1.0 when any decision was made, else None (attribution never
    ran) — not computed from a ratio, because src/choose/selector.py's own
    documented invariant is that a Selection is NEVER recorded unless it's
    inside the admissible set (an out-of-set response halts the run via
    AdmissibilityError instead of ever reaching a "choose" audit record).
    See that module's docstring."""
    decisions_total = sum(1 for r in records if r.get("stage") == "choose")
    return 1.0 if decisions_total > 0 else None


def _net_recovered_paise(conn: sqlite3.Connection, run_id: str) -> int | None:
    row = conn.execute(
        "SELECT COALESCE(SUM(amount_paise), 0) AS net, COUNT(*) AS n "
        "FROM ledger_entry WHERE run_id = ? AND kind = 'net'",
        (run_id,),
    ).fetchone()
    return int(row["net"]) if row["n"] > 0 else None


# ---------------------------------------------------------------------------
# API — reads
# ---------------------------------------------------------------------------


@app.get("/api/runs/latest")
def get_latest_run() -> dict[str, Any]:
    settings = _settings()
    if not Path(settings.db_path).exists():
        raise HTTPException(status_code=404, detail="no run exists yet")
    conn = _read_conn(settings)
    try:
        row = conn.execute(
            "SELECT run_id, mode, started_at, config_hash FROM run ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail="no run exists yet")
    return dict(row)


@app.get("/api/runs/{run_id}/episodes")
def get_run_episodes(run_id: str, since: int = -1) -> list[dict[str, Any]]:
    settings = _settings()
    return _tail_audit_records(settings, run_id, since=since)


@app.get("/api/runs/{run_id}/summary")
def get_run_summary(run_id: str) -> dict[str, Any]:
    settings = _settings()
    if not Path(settings.db_path).exists():
        raise HTTPException(status_code=404, detail="no run exists yet")
    conn = _read_conn(settings)
    try:
        run_row = conn.execute(
            "SELECT episode_count FROM run WHERE run_id = ?", (run_id,)
        ).fetchone()
        if run_row is None:
            raise HTTPException(status_code=404, detail=f"unknown run_id {run_id!r}")
        episode_count = run_row["episode_count"] or 0
        net_recovered_paise = _net_recovered_paise(conn, run_id)
    finally:
        conn.close()

    records = _tail_audit_records(settings, run_id)
    by_outcome = _reconstruct_by_outcome(records, episode_count)

    return {
        "run_id": run_id,
        "episode_count": episode_count,
        "actioned": by_outcome.get("actioned", 0),
        "suppressed": by_outcome.get("suppressed", 0),
        "pending": by_outcome.get("pending", 0),
        "execution_failed": by_outcome.get("execution_failed", 0),
        "admissibility_rate": _admissibility_rate(records),
        "net_recovered_paise": net_recovered_paise,
    }


@app.get("/api/approvals/pending")
def get_pending_approvals() -> list[dict[str, Any]]:
    items = load_queue(DEFAULT_QUEUE_PATH)
    if mark_expired(items):
        save_queue(items, DEFAULT_QUEUE_PATH)
    return [i for i in items if i["status"] == "pending"]


# ---------------------------------------------------------------------------
# API — the one write action
# ---------------------------------------------------------------------------


class DecideBody(BaseModel):
    action: Literal["approve", "reject"]
    reason: str | None = None


@app.post("/api/approvals/{episode_id}/decide")
def post_decide_approval(episode_id: str, body: DecideBody) -> dict[str, Any]:
    items = load_queue(DEFAULT_QUEUE_PATH)
    item = next(
        (i for i in items if i["episode_id"] == episode_id and i["status"] == "pending"), None
    )
    if item is None:
        raise HTTPException(
            status_code=404, detail=f"no pending item for episode_id={episode_id!r}"
        )

    settings = _settings()
    conn = _write_conn(settings)
    audit = AuditWriter(item.get("run_id"), settings.audit_dir, conn)
    try:
        event_id = decide_approval(
            conn, audit, item, action=body.action, reason=body.reason, actor="human"
        )
    finally:
        audit.close()
        conn.close()

    save_queue(items, DEFAULT_QUEUE_PATH)
    return {"audit_event_id": event_id, "status": item["status"]}


# ---------------------------------------------------------------------------
# static files — must be mounted LAST: Starlette matches routes in
# registration order, and a mount at "/" would otherwise shadow every
# /api/... route declared after it.
# ---------------------------------------------------------------------------

app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=DEFAULT_HOST, port=DEFAULT_PORT)
