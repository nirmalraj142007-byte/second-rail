"""Two ways to learn a recovery link's outcome — webhooks (fast, but this
demo's tunnel is not durable, see LIMITATIONS.md) and polling (the demo's
insurance policy: works even if the webhook server was never started).
Both converge on the same `attribute()` call from src/attribute/rules.py,
so a real scenario fed through either path must produce an identical
Attribution — see tests/test_attribution.py.

NO LLM. Everything here is DB reads, one Razorpay GET per link, and a pure
function call.
"""

from __future__ import annotations

import time
from datetime import datetime

from ulid import ULID

from src.attribute.rules import Attribution, ExecutionRecord, OutcomeEvent, attribute
from src.db.repo import (
    get_executions_for_run,
    get_terminal_webhook_events,
    insert_attribution,
)
from src.logging_setup import get_logger

logger = get_logger("attribute.watcher")


def _execution_record_from_row(row) -> ExecutionRecord:
    return ExecutionRecord(
        execution_id=row["execution_id"],
        episode_id=row["episode_id"],
        payment_id=row["payment_id"],
        order_id=row["order_id"],
        plink_id=row["plink_id"],
        amount_paise=row["amount_paise"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _webhook_row_to_event(row) -> OutcomeEvent:
    return OutcomeEvent(
        event_type=row["event_type"],
        payment_id=row["payment_id"],
        order_id=row["order_id"],
        plink_id=row["plink_id"],
        amount_paise=row["amount_paise"],
        occurred_at=datetime.fromisoformat(row["received_at"]),
    )


def _first_payment_id(link_response: dict) -> str | None:
    payments = link_response.get("payments") or []
    if payments and isinstance(payments[0], dict):
        return payments[0].get("payment_id") or payments[0].get("id")
    return None


def _payment_link_outcome_event(link_response: dict, created_at: datetime) -> OutcomeEvent | None:
    """Translate one `GET /payment_links/{id}` response into an OutcomeEvent,
    or None if the link is still in a non-terminal state ('created')."""
    status = link_response.get("status")
    plink_id = link_response.get("id")
    order_id = link_response.get("order_id")

    if status == "paid":
        payments = link_response.get("payments") or []
        amount = link_response.get("amount_paid")
        occurred_at = created_at
        if payments and isinstance(payments[0], dict):
            amount = amount if amount is not None else payments[0].get("amount")
            paid_epoch = payments[0].get("created_at")
            if paid_epoch is not None:
                occurred_at = datetime.fromtimestamp(int(paid_epoch), tz=created_at.tzinfo)
        return OutcomeEvent(
            event_type="payment_link.paid",
            payment_id=_first_payment_id(link_response),
            order_id=order_id,
            plink_id=plink_id,
            amount_paise=int(amount) if amount is not None else None,
            occurred_at=occurred_at,
        )

    if status == "expired":
        expired_epoch = link_response.get("expired_at")
        occurred_at = (
            datetime.fromtimestamp(int(expired_epoch), tz=created_at.tzinfo)
            if expired_epoch is not None
            else created_at
        )
        return OutcomeEvent(
            event_type="payment_link.expired",
            payment_id=None,
            order_id=order_id,
            plink_id=plink_id,
            amount_paise=None,
            occurred_at=occurred_at,
        )

    return None  # "created" or "cancelled" — no terminal outcome yet


class OutcomeWatcher:
    """`window_hours` is fixed at construction (config/guardrails.yaml's
    attribution_window_hours) so both from_webhooks() and by_polling() apply
    the exact same AR-01 window without either call site having to pass it
    through by hand."""

    def __init__(self, window_hours: int) -> None:
        self._window_hours = window_hours

    def _resolve_and_persist(
        self, conn, execution: ExecutionRecord, outcome_event: OutcomeEvent | None
    ) -> Attribution:
        attribution = attribute(execution, outcome_event, self._window_hours)
        insert_attribution(
            conn,
            attribution_id=str(ULID()),
            episode_id=attribution.episode_id,
            execution_id=attribution.execution_id,
            outcome=attribution.outcome,
            reason_code=attribution.reason_code,
            recovered_amount_paise=attribution.recovered_amount_paise,
            window_hours=attribution.window_hours,
            attributed_at=attribution.attributed_at.isoformat(timespec="seconds"),
            attribution_rule_id=attribution.attribution_rule_id,
        )
        return attribution

    def from_webhooks(self, conn, run_id: str) -> list[Attribution]:
        """Match each execution this run created against whatever terminal
        webhook_event rows (payment.captured / payment_link.paid /
        payment_link.expired) have already arrived, preferring the
        earliest-received candidate — Razorpay's dedup boundary
        (src/ingest/service.py) means at most one such row exists per real
        state transition, so "earliest" only matters if more than one
        transition (paid, then a later duplicate delivery) is on file."""
        results: list[Attribution] = []
        for row in get_executions_for_run(conn, run_id):
            execution = _execution_record_from_row(row)
            candidates = get_terminal_webhook_events(
                conn,
                payment_id=execution.payment_id,
                order_id=execution.order_id,
                plink_id=execution.plink_id,
            )
            outcome_event = _webhook_row_to_event(candidates[0]) if candidates else None
            results.append(self._resolve_and_persist(conn, execution, outcome_event))
        return results

    def by_polling(
        self,
        conn,
        run_id: str,
        client,
        interval_s: int = 20,
        timeout_s: int = 300,
    ) -> list[Attribution]:
        """THE POLL PATH IS THE DEMO'S INSURANCE POLICY: it never touches
        the webhook server at all — only `GET /v1/payment_links/{id}` — so
        it works identically whether or not that server (or its cloudflared
        tunnel) is even running. Polls every still-pending execution's link
        every `interval_s` seconds until every one resolves or `timeout_s`
        elapses, then attributes whatever remains as `attribute()` itself
        decides (pending vs. window-elapsed) based on real wall-clock time,
        independent of this call's own timeout."""
        executions = [_execution_record_from_row(r) for r in get_executions_for_run(conn, run_id)]
        pending = [e for e in executions if e.plink_id is not None]
        resolved: dict[str, Attribution] = {}
        deadline = time.monotonic() + timeout_s
        first_pass = True

        while pending and (first_pass or time.monotonic() < deadline):
            if not first_pass:
                time.sleep(interval_s)
            first_pass = False

            still_pending: list[ExecutionRecord] = []
            for execution in pending:
                response = client.fetch_payment_link(execution.plink_id)
                outcome_event = _payment_link_outcome_event(response, execution.created_at)
                if outcome_event is None:
                    still_pending.append(execution)
                    continue
                resolved[execution.episode_id] = self._resolve_and_persist(
                    conn, execution, outcome_event
                )
            pending = still_pending

        for execution in pending:
            resolved[execution.episode_id] = self._resolve_and_persist(conn, execution, None)

        # Executions with no plink_id at all (should not happen for a real
        # 'created' status row, but defensively attributed as not_recovered
        # rather than silently dropped from the returned list).
        for execution in executions:
            if execution.episode_id not in resolved:
                resolved[execution.episode_id] = self._resolve_and_persist(conn, execution, None)

        return [resolved[e.episode_id] for e in executions]
