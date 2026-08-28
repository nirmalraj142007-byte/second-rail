"""Tests for the fault-injection rig and both failure demonstrations.

  1. FaultPlan fires at exactly index 7, twice in a row (rig resets)
  2. Targeting an out-of-range index raises at start
  3. After a 429-exhausted episode, RunSummary.execution_failed == 1 and
     the recovery total excludes that episode's amount (exact number)
  4. consecutive_executor_errors counter resets on a success
  5. Three consecutive failures DO trigger the stopping rule and write a
     stage="stop" audit record
  6. failure_demo_backup produces exactly one episode and one suppressed
     audit record
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest

from scripts.failure_demo_backup import _CapturingAuditWriter, run_backup_demo
from src.audit.writer import AuditWriter
from src.config import Settings
from src.config_models import Guardrails, QuietHours, load_all
from src.db.migrate import get_connection, migrate
from src.db.repo import insert_customer_if_absent
from src.execute.executor import RazorpayExecutor
from src.execute.faults import FaultInjectingExecutor, FaultPlan, recovered_amount_paise
from src.gate.checks import Episode
from src.runner import Runner

IST = ZoneInfo("Asia/Kolkata")


# ---------------------------------------------------------------------------
# shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_db() -> tuple[Path, sqlite3.Connection]:
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        migrate(db_path)
        conn = get_connection(db_path)
        yield db_path, conn
        conn.close()


def _guardrails(**overrides: Any) -> Guardrails:
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


def _episode(i: int, amount_paise: int = 50000) -> Episode:
    return Episode(
        episode_id=f"epi_{i:03d}",
        payment_id=f"pay_TEST{i:03d}",
        order_id=f"order_{i:03d}",
        customer_id=f"cust_{i:03d}",  # distinct customer per episode -> no frequency-cap noise
        amount_paise=amount_paise,
        currency="INR",
        instrument="card",
        issuer_family="HDFC",
        error_code="GATEWAY_ERROR",
        error_description="Gateway timeout",
        error_source="razorpay",
        error_step="authorization",
        error_reason="payment_failed",
        failed_at=datetime(2026, 8, 20, 12, 0, 0, tzinfo=IST),
        received_at=datetime(2026, 8, 20, 12, 5, 0, tzinfo=IST),
        split="train",
        is_synthetic=True,
        harvested_from=None,
    )


def _seed_customers(conn: sqlite3.Connection, episodes: list[Episode]) -> None:
    """Only customers — Runner.run() itself inserts the episode row for
    each episode it gates (src/runner.py `_ensure_episode_row`), so
    pre-inserting episodes here would make `check_duplicate` treat every
    one of them as already-seen and suppress the whole batch."""
    for ep in episodes:
        insert_customer_if_absent(
            conn,
            customer_id=ep.customer_id,
            synthetic_name=None,
            contact_hash="testhash",
            email_hash=None,
            segment="repeat",
            opted_out=False,
            opt_out_ts=None,
            created_at=datetime.now(IST).isoformat(timespec="seconds"),
        )


def _mock_client() -> Mock:
    client = Mock()
    client.create_payment_link_once.return_value = (
        200,
        {"id": "plink_ok", "short_url": "https://rzp.io/l/ok"},
    )
    return client


# ---------------------------------------------------------------------------
# 1. fires at exactly index 7, twice in a row (rig resets)
# ---------------------------------------------------------------------------


class _CountingInner:
    """A minimal Executor with no HTTP layer at all (no `_client`
    attribute) — this isolates the test from RazorpayExecutor's own
    globally-unique idempotency_key semantics (which would make a second
    "take" over the same episodes return duplicate_suppressed rather than
    re-exercising the rig) and tests only what this test claims: the rig's
    own index counter fires on the configured episode every take, and
    resets when start_run() is called again."""

    def create_recovery_link(self, episode, action, policy_rule_id, run_id):
        from src.execute.executor import ExecutionResult
        return ExecutionResult(status="created", idempotency_key="k", created_new=True)

    def cancel_link(self, plink_id):
        return {}


def test_fault_plan_fires_at_exactly_index_7_every_take() -> None:
    inner = _CountingInner()
    plan = FaultPlan(inject_429_at_episode_index=7, inject_429_repeat=3)
    faulty = FaultInjectingExecutor(inner, plan)
    episodes = [_episode(i) for i in range(1, 13)]  # 12 episodes, matches the real demo slice

    for take in (1, 2):
        faulty.start_run(len(episodes))  # reset for this take

        statuses = []
        for ep in episodes:
            try:
                result = faulty.create_recovery_link(ep, "action", "P-00", f"run_{take}")
                statuses.append(result.status)
            except Exception:
                statuses.append("failed")

        # Episode 7 (index 7, 1-based) is the only failure, every take.
        assert statuses[6] == "failed"
        assert all(s == "created" for i, s in enumerate(statuses) if i != 6)


# ---------------------------------------------------------------------------
# 2. out-of-range index raises at start
# ---------------------------------------------------------------------------


def test_out_of_range_index_raises_at_start() -> None:
    wrapped = Mock()
    plan = FaultPlan(inject_429_at_episode_index=15)
    faulty = FaultInjectingExecutor(wrapped, plan)

    with pytest.raises(ValueError, match="episode index"):
        faulty.start_run(12)


def test_ambiguous_plan_raises_immediately() -> None:
    wrapped = Mock()
    plan = FaultPlan(inject_429_at_episode_index=5, inject_timeout_at_index=5)
    with pytest.raises(ValueError, match="ambiguous"):
        FaultInjectingExecutor(wrapped, plan)


def test_using_rig_before_start_run_raises() -> None:
    wrapped = Mock()
    plan = FaultPlan(inject_429_at_episode_index=1)
    faulty = FaultInjectingExecutor(wrapped, plan)
    with pytest.raises(RuntimeError, match="start_run"):
        faulty.create_recovery_link(_episode(1), "action", "P-00", "run_x")


# ---------------------------------------------------------------------------
# 3. execution_failed == 1, recovery total excludes that episode's amount
# ---------------------------------------------------------------------------


def test_429_exhausted_episode_excluded_from_recovery_total(
    temp_db: tuple[Path, sqlite3.Connection], monkeypatch
) -> None:
    _, conn = temp_db
    monkeypatch.setattr("src.execute.executor.time.sleep", lambda _s: None)

    episodes = [_episode(1, amount_paise=10000), _episode(2, amount_paise=20000),
                _episode(3, amount_paise=30000)]
    _seed_customers(conn, episodes)

    g = _guardrails()
    bundle = load_all().model_copy(update={"guardrails": g})
    run_id = "run_recovery_test"
    audit = AuditWriter(run_id, Path(tempfile.mkdtemp()) / "audit", conn)

    client = _mock_client()
    wrapped = RazorpayExecutor(
        conn=conn, client=client, mode="execute", run_id=run_id, audit=audit,
        retry_cap=g.executor_retry_cap, retry_delays=[0.0, 0.0, 0.0],
    )
    plan = FaultPlan(inject_429_at_episode_index=2, inject_429_repeat=3)
    faulty = FaultInjectingExecutor(wrapped, plan)
    faulty.start_run(len(episodes))

    runner = Runner(conn, audit, bundle, Settings(), executor=faulty)
    summary = runner.run(episodes, "execute", run_id=run_id)
    audit.close()

    assert summary.by_outcome.get("execution_failed", 0) == 1
    assert summary.by_outcome.get("actioned", 0) == 2
    # Episode 2 (Rs 200) excluded; episodes 1 and 3 (Rs 100 + Rs 300) included.
    assert recovered_amount_paise(conn, run_id) == 10000 + 30000


# ---------------------------------------------------------------------------
# 4. consecutive_executor_errors counter resets on a success
# ---------------------------------------------------------------------------


def test_consecutive_errors_counter_resets_on_success(
    temp_db: tuple[Path, sqlite3.Connection], monkeypatch
) -> None:
    _, conn = temp_db
    monkeypatch.setattr("src.execute.executor.time.sleep", lambda _s: None)

    # 5 episodes; failures at index 2 and 4, threshold 2. If the counter did
    # NOT reset after episode 3's success, episode 4's failure would bring
    # the naive cumulative count to 2 and the run would stop before episode
    # 5. Because it resets, episode 4 is only the 1st consecutive failure
    # again, and the whole batch completes.
    episodes = [_episode(i) for i in range(1, 6)]
    _seed_customers(conn, episodes)

    g = _guardrails(consecutive_executor_errors_stop=2)
    bundle = load_all().model_copy(update={"guardrails": g})
    run_id = "run_reset_test"
    audit = AuditWriter(run_id, Path(tempfile.mkdtemp()) / "audit", conn)

    client = _mock_client()
    wrapped = RazorpayExecutor(
        conn=conn, client=client, mode="execute", run_id=run_id, audit=audit,
        retry_cap=g.executor_retry_cap, retry_delays=[0.0, 0.0, 0.0],
    )
    plan = FaultPlan(inject_5xx_at_index=2, inject_timeout_at_index=4)
    faulty = FaultInjectingExecutor(wrapped, plan)
    faulty.start_run(len(episodes))

    runner = Runner(conn, audit, bundle, Settings(), executor=faulty)
    summary = runner.run(episodes, "execute", run_id=run_id)
    audit.close()

    assert summary.stopped_reason is None
    assert summary.by_outcome.get("execution_failed", 0) == 2
    assert summary.by_outcome.get("actioned", 0) == 3
    assert summary.episode_count == 5


# ---------------------------------------------------------------------------
# 5. three consecutive failures DO trigger the stopping rule
# ---------------------------------------------------------------------------


def test_three_consecutive_failures_trigger_stopping_rule(
    temp_db: tuple[Path, sqlite3.Connection], monkeypatch
) -> None:
    _, conn = temp_db
    monkeypatch.setattr("src.execute.executor.time.sleep", lambda _s: None)

    episodes = [_episode(i) for i in range(1, 6)]
    _seed_customers(conn, episodes)

    g = _guardrails(consecutive_executor_errors_stop=3)
    bundle = load_all().model_copy(update={"guardrails": g})
    run_id = "run_stop_test"
    audit_dir = Path(tempfile.mkdtemp()) / "audit"
    audit = AuditWriter(run_id, audit_dir, conn)

    client = _mock_client()
    wrapped = RazorpayExecutor(
        conn=conn, client=client, mode="execute", run_id=run_id, audit=audit,
        retry_cap=g.executor_retry_cap, retry_delays=[0.0, 0.0, 0.0],
    )
    # Three consecutive failing indices (1, 2, 3), one of each kind so the
    # plan stays unambiguous.
    plan = FaultPlan(inject_429_at_episode_index=1, inject_429_repeat=3,
                      inject_timeout_at_index=2, inject_5xx_at_index=3)
    faulty = FaultInjectingExecutor(wrapped, plan)
    faulty.start_run(len(episodes))

    runner = Runner(conn, audit, bundle, Settings(), executor=faulty)
    summary = runner.run(episodes, "execute", run_id=run_id)
    audit.close()

    assert summary.stopped_reason == "consecutive_executor_errors"
    # The run breaks immediately after the 3rd consecutive failure —
    # episodes 4 and 5 are never reached.
    assert summary.by_outcome.get("execution_failed", 0) == 3
    assert summary.by_outcome.get("actioned", 0) == 0

    records = [json.loads(line) for line in (audit_dir / f"{run_id}.jsonl").read_text(
        encoding="utf-8").splitlines() if line.strip()]
    stop_records = [r for r in records if r["stage"] == "stop"]
    assert len(stop_records) == 1
    assert "consecutive_executor_errors" in stop_records[0]["rationale"]


# ---------------------------------------------------------------------------
# 6. failure_demo_backup: exactly one episode, one suppressed audit record
# ---------------------------------------------------------------------------


def test_backup_demo_produces_one_episode_and_one_suppressed_record(
    temp_db: tuple[Path, sqlite3.Connection]
) -> None:
    _, conn = temp_db
    audit = _CapturingAuditWriter(None, Path(tempfile.mkdtemp()) / "audit", conn)

    first, second = run_backup_demo(conn, audit)
    audit.close()

    assert first.dedup_result == "new"
    assert second.dedup_result == "duplicate"
    assert first.episode_id is not None

    episode_count = conn.execute("SELECT COUNT(*) AS n FROM episode").fetchone()["n"]
    assert episode_count == 1

    suppressed = [r for r in audit.records if r.get("outcome") == "suppressed"]
    assert len(suppressed) == 1
    assert "payment_id" in suppressed[0]["rationale"] or "dedup" in suppressed[0]["rationale"]
