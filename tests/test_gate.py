from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from freezegun import freeze_time

from src.audit.writer import AuditWriter
from src.config import Settings
from src.config_models import Guardrails, QuietHours, load_all
from src.db.migrate import get_connection, migrate
from src.db.repo import insert_customer_if_absent
from src.gate.checks import (
    Episode,
    GateContext,
    ReasonCode,
    RunState,
    check_frequency_cap,
    check_quiet_hours,
)
from src.gate.engine import GateEngine
from src.runner import (
    DEFAULT_CUSTOMERS_PATH,
    DEFAULT_SOURCES,
    Runner,
    load_and_upsert_customers,
    load_episodes,
)

IST = ZoneInfo("Asia/Kolkata")


def _guardrails(**overrides) -> Guardrails:
    base = dict(
        max_actions_per_payment=1,
        max_contacts_per_customer_7d=2,
        quiet_hours=QuietHours(start="21:00", end="09:00", tz="Asia/Kolkata"),
        max_episode_age_hours=72,
        auto_approve_ceiling_paise=500000,
        batch_contact_ceiling=50,
        per_run_exposure_ceiling_paise=20000000,
        outage_cluster_threshold=15,
        executor_retry_cap=3,
        executor_backoff_seconds=[1, 2, 4],
        consecutive_executor_errors_stop=3,
        kill_switch_path="KILL_test_should_not_exist",
        default_mode="dry_run",
        attribution_window_hours=48,
    )
    base.update(overrides)
    return Guardrails(**base)


def _episode(**overrides) -> Episode:
    base = dict(
        episode_id="epi_00001",
        payment_id="pay_00001",
        customer_id="cust_0001",
        amount_paise=85000,
        instrument="upi",
        issuer_family="BANK_A",
        error_code="BAD_REQUEST_ERROR",
        error_reason="insufficient_fund",
        failed_at="2026-08-20T12:00:00+05:30",
        received_at="2026-08-20T12:01:00+05:30",
        split="train",
    )
    base.update(overrides)
    return Episode.model_validate(base)


def _conn(tmp_path):
    db_path = tmp_path / "test.db"
    migrate(db_path)
    return get_connection(db_path)


# ---------------------------------------------------------------------------
# 1. quiet hours at 22:30 IST -> fail; at 10:00 IST -> pass
# ---------------------------------------------------------------------------


def test_quiet_hours_2230_fails_and_1000_passes(tmp_path):
    g = _guardrails()
    conn = _conn(tmp_path)
    ep = _episode()

    with freeze_time("2026-08-20T22:30:00+05:30"):
        ctx = GateContext(now=datetime.now(IST), conn=conn, state=RunState())
        assert check_quiet_hours(ep, ctx, g).result == "fail"

    with freeze_time("2026-08-20T10:00:00+05:30"):
        ctx = GateContext(now=datetime.now(IST), conn=conn, state=RunState())
        assert check_quiet_hours(ep, ctx, g).result == "pass"


# ---------------------------------------------------------------------------
# 2. quiet-hours boundary at exactly 21:00:00 and 09:00:00 — start
#    inclusive, end exclusive (see src/gate/checks.py:check_quiet_hours)
# ---------------------------------------------------------------------------


def test_quiet_hours_boundary_start_inclusive_end_exclusive(tmp_path):
    g = _guardrails()
    conn = _conn(tmp_path)
    ep = _episode()

    with freeze_time("2026-08-20T21:00:00+05:30"):
        ctx = GateContext(now=datetime.now(IST), conn=conn, state=RunState())
        assert check_quiet_hours(ep, ctx, g).result == "fail"

    with freeze_time("2026-08-20T09:00:00+05:30"):
        ctx = GateContext(now=datetime.now(IST), conn=conn, state=RunState())
        assert check_quiet_hours(ep, ctx, g).result == "pass"


# ---------------------------------------------------------------------------
# 3. third contact inside 7 days -> frequency cap fail
# ---------------------------------------------------------------------------


def test_third_contact_within_7_days_trips_frequency_cap(tmp_path):
    g = _guardrails(max_contacts_per_customer_7d=2)
    conn = _conn(tmp_path)
    state = RunState()
    customer_id = "cust_freq"
    base = datetime(2026, 8, 20, 12, 0, 0, tzinfo=IST)
    state.contacts_by_customer[customer_id] = [base, base + timedelta(hours=8)]
    third = _episode(customer_id=customer_id, failed_at=(base + timedelta(hours=16)).isoformat())
    ctx = GateContext(now=base + timedelta(hours=16), conn=conn, state=state)

    result = check_frequency_cap(third, ctx, g)

    assert result.result == "fail"
    assert result.reason == ReasonCode.FREQUENCY_CAP_EXCEEDED


def test_second_contact_within_7_days_still_passes(tmp_path):
    g = _guardrails(max_contacts_per_customer_7d=2)
    conn = _conn(tmp_path)
    state = RunState()
    customer_id = "cust_freq2"
    base = datetime(2026, 8, 20, 12, 0, 0, tzinfo=IST)
    state.contacts_by_customer[customer_id] = [base]
    second = _episode(customer_id=customer_id, failed_at=(base + timedelta(hours=8)).isoformat())
    ctx = GateContext(now=base + timedelta(hours=8), conn=conn, state=state)

    assert check_frequency_cap(second, ctx, g).result == "pass"


# ---------------------------------------------------------------------------
# 4. terminal_seen payment -> hard_refuse with "already_paid_elsewhere"
# ---------------------------------------------------------------------------


def test_terminal_seen_payment_hard_refuses(tmp_path):
    g = _guardrails()
    conn = _conn(tmp_path)
    ep = _episode(already_paid_elsewhere=True)
    ctx = GateContext(now=datetime(2026, 8, 20, 12, 0, 0, tzinfo=IST), conn=conn, state=RunState())

    decision = GateEngine().evaluate(ep, ctx, g)

    assert decision.eligible is False
    assert decision.escalation_tier == "hard_refuse"
    assert decision.reason_code == ReasonCode.ALREADY_PAID_ELSEWHERE
    assert decision.failed_check == "terminal_seen"


# ---------------------------------------------------------------------------
# 5. episode aged 73h -> hard_refuse; 71h -> eligible
# ---------------------------------------------------------------------------


def test_episode_age_73h_refuses_71h_eligible(tmp_path):
    g = _guardrails()
    conn = _conn(tmp_path)
    now = datetime(2026, 8, 20, 12, 0, 0, tzinfo=IST)
    old = _episode(
        episode_id="epi_old", payment_id="pay_old",
        failed_at=(now - timedelta(hours=73)).isoformat(),
    )
    fresh = _episode(
        episode_id="epi_fresh", payment_id="pay_fresh",
        failed_at=(now - timedelta(hours=71)).isoformat(),
    )
    ctx = GateContext(now=now, conn=conn, state=RunState())

    d_old = GateEngine().evaluate(old, ctx, g)
    d_fresh = GateEngine().evaluate(fresh, ctx, g)

    assert d_old.eligible is False
    assert d_old.escalation_tier == "hard_refuse"
    assert d_old.reason_code == ReasonCode.EPISODE_AGE_EXCEEDS_CAP
    assert d_fresh.eligible is True


# ---------------------------------------------------------------------------
# 6. amount above auto_approve_ceiling -> human_keystroke
# ---------------------------------------------------------------------------


def test_amount_above_ceiling_is_human_keystroke(tmp_path):
    g = _guardrails(auto_approve_ceiling_paise=500000)
    conn = _conn(tmp_path)
    ep = _episode(amount_paise=750000)
    ctx = GateContext(now=datetime(2026, 8, 20, 12, 0, 0, tzinfo=IST), conn=conn, state=RunState())

    decision = GateEngine().evaluate(ep, ctx, g)

    assert decision.eligible is True
    assert decision.escalation_tier == "human_keystroke"


def test_amount_below_ceiling_is_auto(tmp_path):
    g = _guardrails(auto_approve_ceiling_paise=500000)
    conn = _conn(tmp_path)
    ep = _episode(amount_paise=85000)
    ctx = GateContext(now=datetime(2026, 8, 20, 12, 0, 0, tzinfo=IST), conn=conn, state=RunState())

    decision = GateEngine().evaluate(ep, ctx, g)

    assert decision.eligible is True
    assert decision.escalation_tier == "auto"


# ---------------------------------------------------------------------------
# 7. 40-episode cluster -> all hard_refuse, exactly one stage="stop" audit
#    record
# ---------------------------------------------------------------------------


def test_40_episode_cluster_collapses_to_one_stop_record(tmp_path):
    g = _guardrails(outage_cluster_threshold=15)
    conn = _conn(tmp_path)
    bundle = load_all().model_copy(update={"guardrails": g})
    base = datetime(2026, 8, 20, 12, 0, 0, tzinfo=IST)

    episodes = []
    for i in range(40):
        customer_id = f"cust_c{i:03d}"
        insert_customer_if_absent(
            conn, customer_id=customer_id, synthetic_name=None, contact_hash="x",
            email_hash=None, segment="repeat", opted_out=False, opt_out_ts=None,
            created_at=base.isoformat(),
        )
        episodes.append(_episode(
            episode_id=f"epi_c{i:03d}", payment_id=f"pay_c{i:03d}", customer_id=customer_id,
            error_reason="gateway_technical_error",
            failed_at=(base + timedelta(seconds=30 * i)).isoformat(),
        ))

    audit = AuditWriter("run_cluster_test", tmp_path / "audit", conn)
    runner = Runner(conn, audit, bundle, Settings())
    summary = runner.run(episodes, "dry_run", now=base)
    audit.close()

    audit_text = (tmp_path / "audit" / "run_cluster_test.jsonl").read_text(encoding="utf-8")
    records = [json.loads(line) for line in audit_text.splitlines() if line.strip()]
    stop_records = [r for r in records if r["stage"] == "stop"]

    assert summary.by_escalation_tier.get("hard_refuse", 0) == 40
    assert len(stop_records) == 1
    assert "shared_cause_cluster" in stop_records[0]["rationale"]
    assert summary.stopped_reason == "cluster_escalation"


# ---------------------------------------------------------------------------
# 8. KILL file present -> run stops within one episode, stopped_reason set
# ---------------------------------------------------------------------------


def test_kill_switch_stops_within_one_episode(tmp_path):
    kill_path = tmp_path / "KILL"
    kill_path.write_text("", encoding="utf-8")
    g = _guardrails(kill_switch_path=str(kill_path))
    conn = _conn(tmp_path)
    bundle = load_all().model_copy(update={"guardrails": g})
    audit = AuditWriter("run_kill_test", tmp_path / "audit", conn)
    episodes = [_episode(episode_id=f"epi_k{i}", payment_id=f"pay_k{i}") for i in range(5)]

    runner = Runner(conn, audit, bundle, Settings())
    summary = runner.run(episodes, "dry_run", now=datetime(2026, 8, 20, 12, 0, 0, tzinfo=IST))
    audit.close()

    assert summary.stopped_reason == "kill_switch"
    assert summary.episode_count == 5
    assert summary.by_outcome.get("pending", 0) == 5


# ---------------------------------------------------------------------------
# 9. accounting invariant holds on the full 600-episode set
# ---------------------------------------------------------------------------


def test_full_600_episode_set_accounting_invariant_holds(tmp_path):
    from data.generator import REFERENCE_NOW

    conn = _conn(tmp_path)
    bundle = load_all()
    episodes = load_episodes(DEFAULT_SOURCES)
    assert len(episodes) == 600
    load_and_upsert_customers(conn, DEFAULT_CUSTOMERS_PATH)

    audit = AuditWriter("run_full_test", tmp_path / "audit", conn)
    runner = Runner(conn, audit, bundle, Settings())
    summary = runner.run(episodes, "dry_run", now=REFERENCE_NOW)
    audit.close()

    outcome_keys = ("actioned", "suppressed", "execution_failed", "pending")
    total = sum(summary.by_outcome.get(k, 0) for k in outcome_keys)
    assert total == summary.episode_count == 600


# ---------------------------------------------------------------------------
# 10. zero network: monkeypatch httpx.Client.request to raise, run the full
#     gate pass, assert it completes
# ---------------------------------------------------------------------------


def test_gate_run_makes_zero_network_calls(tmp_path, monkeypatch):
    import httpx

    from data.generator import REFERENCE_NOW

    def _boom(*args, **kwargs):
        raise AssertionError("gate stage must never touch the network")

    monkeypatch.setattr(httpx.Client, "request", _boom)

    conn = _conn(tmp_path)
    bundle = load_all()
    episodes = load_episodes(DEFAULT_SOURCES)
    load_and_upsert_customers(conn, DEFAULT_CUSTOMERS_PATH)

    audit = AuditWriter("run_network_test", tmp_path / "audit", conn)
    runner = Runner(conn, audit, bundle, Settings())
    summary = runner.run(episodes, "dry_run", now=REFERENCE_NOW)
    audit.close()

    assert summary.episode_count == 600
