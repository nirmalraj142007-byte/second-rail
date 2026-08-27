"""The seven ordered eligibility checks plus the state they share.

Every check has the signature `check_<name>(episode, ctx, g) -> CheckResult`
and is evaluated in the fixed order `CHECK_ORDER` below — order_index 0..6,
matching CLAUDE.md's module boundary and the phase spec verbatim. A check
reads `ctx` (including `ctx.state`, the run's running counters) but never
mutates it; `src/gate/engine.py`'s `GateEngine` is the only thing that
updates `RunState` between episodes, so replaying the same episode stream
against the same starting state always produces the same sequence of
decisions.

`check_terminal_seen` and `check_frequency_cap` do touch `ctx.conn` /
`ctx.state` to answer "has something already happened", which is not pure
in the strict sense — but it is deterministic given the state at call time,
and there's no way to answer "is this a duplicate contact" without looking
at what's already happened this run.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict

from src.config_models import Guardrails

ReasonCodeStr = str


class ReasonCode:
    """Closed, snake_case enum of every reason a gate check can fail with.
    A judge groups the exception list by this field — every string here is
    stable and never composed ad hoc at a call site."""

    DUPLICATE_EPISODE_THIS_RUN = "duplicate_episode_this_run"
    ALREADY_PAID_ELSEWHERE = "already_paid_elsewhere"
    CUSTOMER_OPTED_OUT = "customer_opted_out"
    EPISODE_AGE_EXCEEDS_CAP = "episode_age_exceeds_cap"
    EXPOSURE_CEILING_EXCEEDED = "exposure_ceiling_exceeded"
    FREQUENCY_CAP_EXCEEDED = "frequency_cap_exceeded"
    QUIET_HOURS_BLOCK = "quiet_hours_block"
    SHARED_CAUSE_CLUSTER = "shared_cause_cluster"


ALL_REASON_CODES: frozenset[str] = frozenset(
    v for k, v in vars(ReasonCode).items() if not k.startswith("_") and isinstance(v, str)
)

_TERMINAL_EVENT_TYPES = ("payment.captured", "payment_link.paid", "payment_link.expired")


class Episode(BaseModel):
    """A gate-relevant view of one episode record, whether it came from
    `data/train.jsonl`, `holdout/sealed.jsonl`, or a live webhook. Built with
    pydantic so a malformed source row fails loudly and specifically rather
    than crashing deep inside a check function."""

    episode_id: str
    payment_id: str
    order_id: str | None = None
    customer_id: str | None = None
    amount_paise: int
    currency: str = "INR"
    instrument: str | None = None
    issuer_family: str | None = None
    error_code: str | None = None
    error_description: str | None = None
    error_source: str | None = None
    error_step: str | None = None
    error_reason: str | None = None
    failed_at: datetime
    received_at: datetime
    split: str | None = None
    is_synthetic: bool = True
    harvested_from: str | None = None
    segment: str | None = None
    edge_case: str | None = None
    edge_case_note: str | None = None
    already_paid_elsewhere: bool = False
    refund_already_issued: bool = False
    order_already_fulfilled: bool = False
    outage_cluster_id: str | None = None
    frequency_cap_group: str | None = None

    model_config = ConfigDict(extra="ignore")


@dataclass(frozen=True)
class CheckResult:
    name: str
    result: Literal["pass", "fail"]
    reason: str | None = None


@dataclass
class RunState:
    """Running counters mutated by GateEngine between episodes — never by a
    check function itself. One instance per `Runner.run()` call."""

    exposure_committed_paise: int = 0
    total_eligible_contacts_this_run: int = 0
    contacts_by_customer: dict[str, list[datetime]] = field(default_factory=dict)
    cap_breached: bool = False
    cluster_escalated: bool = False
    cluster_processed: dict[str, int] = field(default_factory=dict)
    consecutive_executor_errors: int = 0


@dataclass
class GateContext:
    now: datetime
    conn: sqlite3.Connection
    state: RunState
    opted_out_customers: frozenset[str] = frozenset()
    cluster_key_for_episode: dict[str, str] = field(default_factory=dict)

    def prior_contacts_7d(self, customer_id: str, *, before: datetime) -> int:
        since = before - timedelta(days=7)
        timestamps = self.state.contacts_by_customer.get(customer_id, [])
        return sum(1 for ts in timestamps if since <= ts < before)


def _parse_hhmm(value: str) -> time:
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


# ---------------------------------------------------------------------------
# the seven ordered checks
# ---------------------------------------------------------------------------


def check_duplicate(episode: Episode, ctx: GateContext, g: Guardrails) -> CheckResult:
    """"Already processed" is answered against the DB, not an in-memory set —
    that makes re-running `make gate-run` against an un-reset database an
    idempotent no-op on the second pass instead of a crash on the first
    UNIQUE(payment_id) collision, which is exactly the behaviour this
    project is supposed to model everywhere else."""
    row = ctx.conn.execute(
        "SELECT 1 FROM episode WHERE payment_id = ? LIMIT 1", (episode.payment_id,)
    ).fetchone()
    if row is not None:
        return CheckResult("duplicate", "fail", ReasonCode.DUPLICATE_EPISODE_THIS_RUN)
    return CheckResult("duplicate", "pass", None)


def check_terminal_seen(episode: Episode, ctx: GateContext, g: Guardrails) -> CheckResult:
    terminal_flags = (
        episode.already_paid_elsewhere,
        episode.refund_already_issued,
        episode.order_already_fulfilled,
    )
    if any(terminal_flags):
        return CheckResult("terminal_seen", "fail", ReasonCode.ALREADY_PAID_ELSEWHERE)
    placeholders = ",".join("?" * len(_TERMINAL_EVENT_TYPES))
    row = ctx.conn.execute(
        "SELECT 1 FROM webhook_event WHERE payment_id = ? "
        f"AND event_type IN ({placeholders}) LIMIT 1",
        (episode.payment_id, *_TERMINAL_EVENT_TYPES),
    ).fetchone()
    if row is not None:
        return CheckResult("terminal_seen", "fail", ReasonCode.ALREADY_PAID_ELSEWHERE)
    return CheckResult("terminal_seen", "pass", None)


def check_opt_out(episode: Episode, ctx: GateContext, g: Guardrails) -> CheckResult:
    if episode.customer_id and episode.customer_id in ctx.opted_out_customers:
        return CheckResult("opt_out", "fail", ReasonCode.CUSTOMER_OPTED_OUT)
    return CheckResult("opt_out", "pass", None)


def check_episode_age(episode: Episode, ctx: GateContext, g: Guardrails) -> CheckResult:
    age = ctx.now - episode.failed_at
    if age > timedelta(hours=g.max_episode_age_hours):
        return CheckResult("episode_age", "fail", ReasonCode.EPISODE_AGE_EXCEEDS_CAP)
    return CheckResult("episode_age", "pass", None)


def check_amount_cap(episode: Episode, ctx: GateContext, g: Guardrails) -> CheckResult:
    """Headroom is reserved the moment this check passes, not only once the
    episode clears every later check (see check_frequency_cap /
    check_quiet_hours) — a conservative pre-flight reservation, so a run
    never promises more notional exposure than guardrails.yaml allows even
    if a later check would have blocked the actual contact anyway."""
    projected = ctx.state.exposure_committed_paise + episode.amount_paise
    if projected > g.per_run_exposure_ceiling_paise:
        return CheckResult("amount_cap", "fail", ReasonCode.EXPOSURE_CEILING_EXCEEDED)
    return CheckResult("amount_cap", "pass", None)


def check_frequency_cap(episode: Episode, ctx: GateContext, g: Guardrails) -> CheckResult:
    if not episode.customer_id:
        return CheckResult("frequency_cap", "pass", None)
    count = ctx.prior_contacts_7d(episode.customer_id, before=episode.failed_at)
    if count >= g.max_contacts_per_customer_7d:
        return CheckResult("frequency_cap", "fail", ReasonCode.FREQUENCY_CAP_EXCEEDED)
    return CheckResult("frequency_cap", "pass", None)


def check_quiet_hours(episode: Episode, ctx: GateContext, g: Guardrails) -> CheckResult:
    """Quiet hours block on `ctx.now` — the moment a contact would actually
    go out — not on `episode.failed_at`, the historical moment the payment
    failed. See docs/where-the-llm-is-not.md.

    Boundary convention: start inclusive, end exclusive — `[21:00, 09:00)`
    wrapping midnight. A message may go out at exactly 09:00:00 (the window
    has just ended); it may not go out at exactly 21:00:00 (the window has
    just begun). Chosen, not incidental — see tests/test_gate.py."""
    tz_now = ctx.now.astimezone(ZoneInfo(g.quiet_hours.tz))
    start = _parse_hhmm(g.quiet_hours.start)
    end = _parse_hhmm(g.quiet_hours.end)
    t = tz_now.time()
    in_quiet_window = (t >= start or t < end) if start > end else (start <= t < end)
    if in_quiet_window:
        return CheckResult("quiet_hours", "fail", ReasonCode.QUIET_HOURS_BLOCK)
    return CheckResult("quiet_hours", "pass", None)


CHECK_ORDER: tuple[tuple[str, object], ...] = (
    ("duplicate", check_duplicate),
    ("terminal_seen", check_terminal_seen),
    ("opt_out", check_opt_out),
    ("episode_age", check_episode_age),
    ("amount_cap", check_amount_cap),
    ("frequency_cap", check_frequency_cap),
    ("quiet_hours", check_quiet_hours),
)

HARD_REFUSE_CHECKS: frozenset[str] = frozenset({"terminal_seen", "opt_out", "episode_age"})
