"""Backup failure demonstration — no external API required.

Replays fixtures/webhooks/payment_failed.json twice through IngestService
directly, under two different event ids, showing the payment_id-keyed
dedup boundary (src/ingest/service.py) suppress the second delivery as a
no-op: zero new episodes, one audit record naming exactly why.

Exists because a stateful fault injector interacting with a live API is
exactly the kind of thing that works on take one and not on take three.
Needs no network, no Razorpay keys, and produces well under 20 lines of
output — `make failure-demo-backup` runs it directly against
IngestService rather than through the FastAPI layer, since neither the
dedup rule nor the audit trail depends on the HTTP boundary.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from src.audit.writer import AuditWriter
from src.config import load_settings
from src.db.migrate import get_connection, migrate
from src.ingest.service import IngestResult, IngestService

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "fixtures" / "webhooks" / "payment_failed.json"


class _CapturingAuditWriter(AuditWriter):
    """Same hash chain as AuditWriter — every record still goes through
    `super().append()` — plus an in-memory copy of exactly what was
    appended this run, so the script (and tests) can point at the real
    record that explains the dedup no-op instead of re-parsing a JSONL
    file that may hold records from earlier runs too."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.records: list[dict[str, Any]] = []

    def append(
        self, *, stage: str, actor: str, episode_id: str | None = None, **fields: Any
    ) -> str:
        event_id = super().append(stage=stage, actor=actor, episode_id=episode_id, **fields)
        self.records.append({
            "event_id": event_id, "stage": stage, "actor": actor,
            "episode_id": episode_id, **fields,
        })
        return event_id


def run_backup_demo(conn, audit: AuditWriter) -> tuple[IngestResult, IngestResult]:
    """Replay the fixture twice under distinct event ids. Returns
    (first_delivery_result, second_delivery_result)."""
    settings = load_settings()
    service = IngestService(conn, audit, settings)
    raw = FIXTURE.read_bytes()
    payload = json.loads(raw)
    raw_body_hash = hashlib.sha256(raw).hexdigest()

    first = service.handle_event(
        event_id="evt_backup_demo_1",
        event_type="payment.failed",
        payload=payload,
        raw_body_hash=raw_body_hash,
    )
    second = service.handle_event(
        event_id="evt_backup_demo_2",
        event_type="payment.failed",
        payload=payload,
        raw_body_hash=raw_body_hash,
    )
    return first, second


def main() -> int:
    settings = load_settings()
    migrate(settings.db_path)
    conn = get_connection(settings.db_path)
    audit = _CapturingAuditWriter(None, settings.audit_dir, conn)

    try:
        first, second = run_backup_demo(conn, audit)
    finally:
        audit.close()

    payment_id = conn.execute(
        "SELECT payment_id FROM episode WHERE episode_id = ?", (first.episode_id,)
    ).fetchone()["payment_id"]
    episode_count = conn.execute(
        "SELECT COUNT(*) AS n FROM episode WHERE payment_id = ?", (payment_id,)
    ).fetchone()["n"]

    # Plain ASCII only, matching scripts/demo.py's own convention — a
    # Windows console on the cp1252 codepage raises/garbles non-ASCII
    # punctuation like an em dash.
    print("BACKUP FAILURE DEMO - duplicate payment.failed webhook, no network")
    print(f"delivery 1 (evt_backup_demo_1): dedup_result={first.dedup_result} "
          f"episode_id={first.episode_id}")
    print(f"delivery 2 (evt_backup_demo_2): dedup_result={second.dedup_result} "
          f"episode_id={second.episode_id}")
    print(f"payment_id={payment_id} episodes_in_db={episode_count} "
          "(must be 1 - second delivery created no new episode)")

    suppressed = [r for r in audit.records if r.get("outcome") == "suppressed"]
    if suppressed:
        record = suppressed[-1]
        print("audit record explaining the no-op:")
        print(f"  stage={record['stage']} outcome={record['outcome']} "
              f"rationale={record['rationale']!r}")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
