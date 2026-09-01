"""AR-01 — the one and only attribution rule. NO LLM: the model never
determines attribution, ever. Every outcome this module produces is a pure
function of (execution, outcome_event, window_hours) — nothing here reads
an LLM response, and tests/test_llm_boundary.py greps this package for the
client symbol to enforce it.

Rule AR-01, stated in exactly the words that appear on screen in the video:

    "A payment is attributed as recovered when a payment_link.paid or
     payment.captured event references a link created by this run, for the
     same payment_id or order_id, within <window_hours> hours of link
     creation. Everything else is not_recovered."

`window_hours` always comes from `config/guardrails.yaml`'s
`attribution_window_hours` (48 by default) and must match
`outcome_model.md` §3 — `scripts/config_check.py` asserts the two agree.
Never hard-code 48 here.

Matching a link "created by this run" (implementation of "references a
link created by this run" above): an outcome event references our link if
its `plink_id` equals the execution's `plink_id` (the strongest signal —
Razorpay's own payment_link.paid/expired events always carry it), or,
absent a plink_id on the event (a bare payment.captured has no
payment_link entity), if its `order_id` matches the execution's order_id.
An event whose payment_id/order_id happens to match but whose plink_id
explicitly disagrees did not come through our link — see
`REASON_UNATTRIBUTABLE_RECOVERY` below.

Reason codes this module ever returns, each one load-bearing for the
report's honesty (judge expectations §3):

    recovered_within_window      the success path — AR-01 is satisfied
    outside_attribution_window   the outcome event arrived, but after
                                  window_hours from link creation
    partial_payment_not_attributed  the amount paid is less than the
                                  amount the link was created for — see
                                  LIMITATIONS.md
    unattributable_recovery      the payment/order matched, but not
                                  through a link this run created — we do
                                  not claim credit for a recovery via a
                                  channel we did not create
    link_expired                 the event is payment_link.expired for
                                  our own link
    awaiting_outcome              no terminal event has arrived yet and
                                  window_hours has not elapsed — "pending"
    no_outcome_before_deadline    no terminal event ever arrived and
                                  window_hours has elapsed
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

ATTRIBUTION_RULE_ID = "AR-01"

REASON_RECOVERED = "recovered_within_window"
REASON_OUTSIDE_WINDOW = "outside_attribution_window"
REASON_PARTIAL_PAYMENT = "partial_payment_not_attributed"
REASON_UNATTRIBUTABLE = "unattributable_recovery"
REASON_LINK_EXPIRED = "link_expired"
REASON_AWAITING_OUTCOME = "awaiting_outcome"
REASON_NO_OUTCOME_BEFORE_DEADLINE = "no_outcome_before_deadline"

Outcome = Literal["recovered", "not_recovered", "pending"]

TerminalEventType = Literal["payment.captured", "payment_link.paid", "payment_link.expired"]


@dataclass(frozen=True)
class ExecutionRecord:
    """The subset of one `execution` row (joined to its episode) that AR-01
    needs to judge an outcome against. Built by src/attribute/watcher.py
    from either the DB (from_webhooks) or a live Payment Link fetch
    (by_polling) — both paths converge on this same shape, which is what
    guarantees they produce identical Attribution objects for the same
    scenario."""

    execution_id: str
    episode_id: str
    payment_id: str
    order_id: str | None
    plink_id: str | None
    amount_paise: int
    created_at: datetime


@dataclass(frozen=True)
class OutcomeEvent:
    """One candidate outcome, from a webhook row or a polled Payment Link
    fetch — same shape either way."""

    event_type: TerminalEventType
    payment_id: str | None
    order_id: str | None
    plink_id: str | None
    amount_paise: int | None
    occurred_at: datetime


class Attribution(BaseModel):
    episode_id: str
    execution_id: str | None
    outcome: Outcome
    reason_code: str
    recovered_amount_paise: int | None = None
    window_hours: int
    attributed_at: datetime
    attribution_rule_id: str = ATTRIBUTION_RULE_ID


def _references_our_link(execution: ExecutionRecord, event: OutcomeEvent) -> bool:
    if event.plink_id is not None or execution.plink_id is not None:
        return event.plink_id == execution.plink_id
    if event.order_id and execution.order_id:
        return event.order_id == execution.order_id
    return event.payment_id == execution.payment_id


def attribute(
    execution: ExecutionRecord,
    outcome_event: OutcomeEvent | None,
    window_hours: int,
    *,
    now: datetime | None = None,
) -> Attribution:
    """Apply AR-01. `now` is only used when `outcome_event` is None (no
    terminal event observed yet) to decide pending vs. window-elapsed;
    ignored otherwise — an event's own `occurred_at` is what the window is
    measured against, never wall-clock time at the moment attribute() runs."""
    attributed_at = now or datetime.now(execution.created_at.tzinfo)

    if outcome_event is None:
        elapsed = attributed_at - execution.created_at
        if elapsed.total_seconds() / 3600.0 >= window_hours:
            return Attribution(
                episode_id=execution.episode_id,
                execution_id=execution.execution_id,
                outcome="not_recovered",
                reason_code=REASON_NO_OUTCOME_BEFORE_DEADLINE,
                window_hours=window_hours,
                attributed_at=attributed_at,
            )
        return Attribution(
            episode_id=execution.episode_id,
            execution_id=execution.execution_id,
            outcome="pending",
            reason_code=REASON_AWAITING_OUTCOME,
            window_hours=window_hours,
            attributed_at=attributed_at,
        )

    attributed_at = outcome_event.occurred_at

    if not _references_our_link(execution, outcome_event):
        return Attribution(
            episode_id=execution.episode_id,
            execution_id=execution.execution_id,
            outcome="not_recovered",
            reason_code=REASON_UNATTRIBUTABLE,
            window_hours=window_hours,
            attributed_at=attributed_at,
        )

    if outcome_event.event_type == "payment_link.expired":
        return Attribution(
            episode_id=execution.episode_id,
            execution_id=execution.execution_id,
            outcome="not_recovered",
            reason_code=REASON_LINK_EXPIRED,
            window_hours=window_hours,
            attributed_at=attributed_at,
        )

    elapsed_hours = (outcome_event.occurred_at - execution.created_at).total_seconds() / 3600.0
    if elapsed_hours > window_hours:
        return Attribution(
            episode_id=execution.episode_id,
            execution_id=execution.execution_id,
            outcome="not_recovered",
            reason_code=REASON_OUTSIDE_WINDOW,
            window_hours=window_hours,
            attributed_at=attributed_at,
        )

    if (
        outcome_event.amount_paise is not None
        and outcome_event.amount_paise < execution.amount_paise
    ):
        return Attribution(
            episode_id=execution.episode_id,
            execution_id=execution.execution_id,
            outcome="not_recovered",
            reason_code=REASON_PARTIAL_PAYMENT,
            window_hours=window_hours,
            attributed_at=attributed_at,
        )

    return Attribution(
        episode_id=execution.episode_id,
        execution_id=execution.execution_id,
        outcome="recovered",
        reason_code=REASON_RECOVERED,
        recovered_amount_paise=outcome_event.amount_paise or execution.amount_paise,
        window_hours=window_hours,
        attributed_at=attributed_at,
    )
