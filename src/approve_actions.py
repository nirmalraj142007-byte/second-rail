"""The one place a human's approve/reject verdict on a queued
human_keystroke episode is ever persisted.

Two callers, one code path: `src/ui/approve.py`'s CLI (`make approve
ID=...`) and `src/webui/app.py`'s `POST /api/approvals/{episode_id}/decide`
both call `decide_approval()` and nothing else — neither re-implements the
`approval` table write or the audit record. `tests/test_webui.py` asserts
the two entry points produce byte-identical audit record shapes (minus
event_id/timestamp) for the same input, which is only true because there
is exactly one implementation to diverge from.

`decide_approval()` mutates the passed-in queue-item dict in place
(status/approved_by/resolved_at/rejected_reason/audit_event_id) rather than
taking those fields as flat parameters — deliberately, so the "what changes
on a queue item when it's decided" logic also lives in exactly one place,
not duplicated between the CLI's console-rendering path and the web
endpoint's JSON-response path. The caller still owns persisting the queue
file afterward (`src.ui.approve.save_queue`) since that's a queue-storage
concern, not a decision concern.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Literal

from ulid import ULID

from src.audit.writer import AuditWriter
from src.db.repo import insert_approval
from src.logging_setup import IST

Action = Literal["approve", "reject"]


def decide_approval(
    conn: sqlite3.Connection,
    audit: AuditWriter,
    item: dict,
    *,
    action: Action,
    reason: str | None,
    actor: str,
    tier: str = "human_keystroke",
) -> str:
    """Persist one human decision on `item` (a queue item dict shaped like
    `src/ui/approve.py`'s `QueueItem`: episode_id, chosen_action,
    gate_reason, expires_at, run_id) and return the resulting audit
    event_id. Writes the `approval` table row (src/db/repo.py's
    insert_approval) and one audit record (stage="approve"), then updates
    `item` in place — the caller passes an `AuditWriter` already
    constructed against `item`'s own run_id (or None), and closes it
    afterward; this function never opens or closes one itself, since both
    callers already need their own connection/writer lifecycle around
    other work in the same request/command.
    """
    decision: Literal["approved", "rejected"] = "approved" if action == "approve" else "rejected"
    now_iso = datetime.now(IST).isoformat(timespec="seconds")
    rejected_reason = None if decision == "approved" else (reason or "operator_rejected")

    insert_approval(
        conn,
        approval_id=str(ULID()),
        episode_id=item["episode_id"],
        required=True,
        tier=tier,
        approved_by=actor if decision == "approved" else None,
        approved_at=now_iso if decision == "approved" else None,
        rejected_reason=rejected_reason,
        expires_at=item.get("expires_at"),
    )

    # No CLI- or web-specific prefix in the rationale text — this is the
    # exact string both callers must produce for the byte-identical-shape
    # test to mean anything. A prior version of the CLI path prefixed this
    # with "make approve: "; removed when this was extracted, since that
    # prefix would have made a web-originated approval's audit record lie
    # about where the decision came from.
    event_id = audit.append(
        stage="approve",
        actor=actor,
        episode_id=item["episode_id"],
        outcome=decision,
        rationale=(
            f"{decision} chosen_action={item['chosen_action']!r}"
            + (f" reason={reason!r}" if reason else "")
        ),
        approval={
            "chosen_action": item["chosen_action"],
            "gate_reason": item["gate_reason"],
            "decision": decision,
        },
    )

    # Queue-item "approved_by" tracks who resolved it either way (matching
    # the CLI's original, pre-extraction behavior) — distinct from the
    # `approval` table's approved_by column above, which is genuinely
    # None on a rejection. Different fields, deliberately different
    # semantics: one says "who acted," the other says "who approved."
    item["status"] = decision
    item["approved_by"] = actor
    item["resolved_at"] = now_iso
    if decision == "rejected":
        item["rejected_reason"] = rejected_reason
    item["audit_event_id"] = event_id

    return event_id
