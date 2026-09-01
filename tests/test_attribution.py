"""Tests for src/attribute/ — AR-01, the two outcome watchers, and the
recovery ledger.

Covers:
  1. paid inside window -> recovered + gross ledger entry with the exact amount
  2. paid at window_hours + 1 minute -> not_recovered, outside_attribution_window
  3. webhook path and polling path produce identical Attribution objects
  4. polling works with the webhook server not running at all
  5. fp cost: 3 seeded FP contacts -> exact rupee figure, and net == gross - fp
  6. gate-disabled counterfactual reports a higher FP count than gate-enabled
  7. attribution_window_hours drives behaviour (changing it changes the outcome)

Plus a few extra pure-function cases for the edge codes rules.py commits to
(partial payment, unattributable recovery, link expired) — not in the
phase's required list, but cheap given the fixtures already exist here.
"""

from __future__ import annotations

import sqlite3
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest

from src.attribute.ledger import (
    OutcomeAssumptions,
    compute_fp_cost,
    compute_gate_disabled_counterfactual,
    parse_outcome_assumptions,
    post_gross,
    post_net,
)
from src.attribute.rules import (
    REASON_LINK_EXPIRED,
    REASON_OUTSIDE_WINDOW,
    REASON_PARTIAL_PAYMENT,
    REASON_RECOVERED,
    REASON_UNATTRIBUTABLE,
    ExecutionRecord,
    OutcomeEvent,
    attribute,
)
from src.attribute.watcher import OutcomeWatcher
from src.config_models import Guardrails, QuietHours
from src.db.migrate import get_connection, migrate
from src.db.repo import (
    get_ledger_total,
    insert_customer_if_absent,
    insert_episode,
    insert_webhook_event,
    start_run,
)
from src.gate.checks import Episode

IST = ZoneInfo("Asia/Kolkata")
ROOT = Path(__file__).resolve().parent.parent


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


@pytest.fixture
def temp_db() -> tuple[Path, sqlite3.Connection]:
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        migrate(db_path)
        conn = get_connection(db_path)
        yield db_path, conn
        conn.close()


def _seed_run(conn: sqlite3.Connection, run_id: str = "run_attr") -> None:
    start_run(conn, run_id=run_id, started_at=_iso(datetime.now(IST)), mode="execute")


def _seed_customer(
    conn: sqlite3.Connection,
    customer_id: str,
    *,
    opted_out: bool = False,
    opt_out_ts: str | None = None,
) -> None:
    insert_customer_if_absent(
        conn,
        customer_id=customer_id,
        synthetic_name=None,
        contact_hash="hash",
        email_hash=None,
        segment="repeat",
        opted_out=opted_out,
        opt_out_ts=opt_out_ts,
        created_at=_iso(datetime.now(IST)),
    )


def _seed_episode(
    conn: sqlite3.Connection,
    episode_id: str,
    payment_id: str,
    *,
    customer_id: str | None = None,
    amount_paise: int = 50000,
    order_id: str | None = None,
    failed_at: datetime | None = None,
) -> None:
    failed_at = failed_at or datetime.now(IST)
    insert_episode(
        conn,
        episode_id=episode_id,
        payment_id=payment_id,
        order_id=order_id,
        customer_id=customer_id,
        amount_paise=amount_paise,
        currency="INR",
        instrument="upi",
        issuer_family="BANK_A",
        error_code="BAD_REQUEST_ERROR",
        error_description="x",
        error_source="business",
        error_step="payment_authentication",
        error_reason="insufficient_funds",
        failed_at=_iso(failed_at),
        received_at=_iso(failed_at),
        split="train",
        is_synthetic=True,
        harvested_from=None,
    )


def _seed_execution(
    conn: sqlite3.Connection,
    execution_id: str,
    episode_id: str,
    *,
    plink_id: str | None,
    created_at: datetime,
    run_id: str = "run_attr",
    status: str = "created",
) -> None:
    conn.execute(
        """
        INSERT INTO execution (
            execution_id, episode_id, idempotency_key, reference_id, api,
            plink_id, short_url, status, run_id, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (
            execution_id, episode_id, f"key_{execution_id}", f"sr-key_{execution_id}",
            "payment_links", plink_id, f"https://rzp.io/l/{execution_id}", status,
            run_id, _iso(created_at),
        ),
    )
    conn.commit()


def _seed_terminal_webhook(
    conn: sqlite3.Connection,
    event_id: str,
    *,
    event_type: str,
    received_at: datetime,
    payment_id: str | None = None,
    order_id: str | None = None,
    plink_id: str | None = None,
    amount_paise: int | None = None,
) -> None:
    insert_webhook_event(
        conn,
        event_id=event_id,
        event_type=event_type,
        payment_id=payment_id,
        plink_id=plink_id,
        order_id=order_id,
        amount_paise=amount_paise,
        raw_body_hash="hash",
        signature_valid=True,
        received_at=_iso(received_at),
        processed=True,
        dedup_result="new",
    )


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
        amount_paise=50000,
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


# ---------------------------------------------------------------------------
# 1. paid inside window -> recovered + gross ledger entry with exact amount
# ---------------------------------------------------------------------------


def test_paid_inside_window_is_recovered_and_posts_exact_gross(
    temp_db: tuple[Path, sqlite3.Connection],
) -> None:
    _, conn = temp_db
    _seed_run(conn)
    _seed_episode(conn, "epi_1", "pay_1", amount_paise=50000)
    created_at = datetime(2026, 9, 1, 10, 0, tzinfo=IST)
    execution = ExecutionRecord(
        execution_id="exec_1", episode_id="epi_1", payment_id="pay_1",
        order_id=None, plink_id="plink_ABC", amount_paise=50000, created_at=created_at,
    )
    event = OutcomeEvent(
        event_type="payment_link.paid", payment_id="pay_recovered_1", order_id=None,
        plink_id="plink_ABC", amount_paise=50000, occurred_at=created_at + timedelta(hours=10),
    )

    result = attribute(execution, event, window_hours=48)

    assert result.outcome == "recovered"
    assert result.reason_code == REASON_RECOVERED
    assert result.recovered_amount_paise == 50000

    post_gross(conn, "run_attr", result)
    assert get_ledger_total(conn, "run_attr", "gross_recovery") == 50000


# ---------------------------------------------------------------------------
# 2. paid at window_hours + 1 minute -> not_recovered, outside_attribution_window
# ---------------------------------------------------------------------------


def test_paid_one_minute_past_window_is_not_recovered() -> None:
    created_at = datetime(2026, 9, 1, 10, 0, tzinfo=IST)
    execution = ExecutionRecord(
        execution_id="exec_2", episode_id="epi_2", payment_id="pay_2",
        order_id=None, plink_id="plink_XYZ", amount_paise=20000, created_at=created_at,
    )
    event = OutcomeEvent(
        event_type="payment_link.paid", payment_id="pay_recovered_2", order_id=None,
        plink_id="plink_XYZ", amount_paise=20000,
        occurred_at=created_at + timedelta(hours=48, minutes=1),
    )

    result = attribute(execution, event, window_hours=48)

    assert result.outcome == "not_recovered"
    assert result.reason_code == REASON_OUTSIDE_WINDOW


# ---------------------------------------------------------------------------
# 3. webhook path and polling path produce identical Attribution objects
# ---------------------------------------------------------------------------


def test_webhook_and_polling_paths_agree(temp_db: tuple[Path, sqlite3.Connection]) -> None:
    _, conn = temp_db
    _seed_run(conn)
    _seed_episode(conn, "epi_3", "pay_3", amount_paise=30000)
    created_at = datetime(2026, 9, 1, 8, 0, tzinfo=IST)
    _seed_execution(conn, "exec_3", "epi_3", plink_id="plink_PARITY", created_at=created_at)

    paid_at = created_at + timedelta(hours=5)
    _seed_terminal_webhook(
        conn, "evt_paid_3", event_type="payment_link.paid", received_at=paid_at,
        payment_id="pay_recovered_3", plink_id="plink_PARITY", amount_paise=30000,
    )

    watcher = OutcomeWatcher(window_hours=48)
    webhook_results = watcher.from_webhooks(conn, "run_attr")
    assert len(webhook_results) == 1
    via_webhook = webhook_results[0]

    mock_client = Mock()
    mock_client.fetch_payment_link.return_value = {
        "id": "plink_PARITY",
        "status": "paid",
        "order_id": None,
        "amount_paid": 30000,
        "payments": [
            {"id": "pay_recovered_3", "amount": 30000, "created_at": int(paid_at.timestamp())}
        ],
    }
    via_polling = watcher.by_polling(conn, "run_attr", mock_client, interval_s=0, timeout_s=5)[0]

    assert via_webhook.episode_id == via_polling.episode_id
    assert via_webhook.outcome == via_polling.outcome == "recovered"
    assert via_webhook.reason_code == via_polling.reason_code
    assert via_webhook.recovered_amount_paise == via_polling.recovered_amount_paise == 30000
    assert via_webhook.window_hours == via_polling.window_hours
    assert via_webhook.attribution_rule_id == via_polling.attribution_rule_id
    assert _iso(via_webhook.attributed_at) == _iso(via_polling.attributed_at)


# ---------------------------------------------------------------------------
# 4. polling works with the webhook server not running at all
# ---------------------------------------------------------------------------


def test_polling_requires_no_webhook_server(temp_db: tuple[Path, sqlite3.Connection]) -> None:
    _, conn = temp_db
    _seed_run(conn)
    _seed_episode(conn, "epi_4", "pay_4", amount_paise=15000)
    created_at = datetime(2026, 9, 1, 9, 0, tzinfo=IST)
    _seed_execution(conn, "exec_4", "epi_4", plink_id="plink_NOSERVER", created_at=created_at)
    # Deliberately: zero rows in webhook_event anywhere in this DB — there
    # is no ingest server, no tunnel, nothing. by_polling must still work,
    # because it only ever calls the passed-in client directly.
    assert conn.execute("SELECT COUNT(*) AS n FROM webhook_event").fetchone()["n"] == 0

    mock_client = Mock()
    mock_client.fetch_payment_link.return_value = {
        "id": "plink_NOSERVER",
        "status": "paid",
        "order_id": None,
        "amount_paid": 15000,
        "payments": [{"id": "pay_recovered_4", "amount": 15000,
                      "created_at": int((created_at + timedelta(hours=1)).timestamp())}],
    }

    watcher = OutcomeWatcher(window_hours=48)
    results = watcher.by_polling(conn, "run_attr", mock_client, interval_s=0, timeout_s=5)

    assert len(results) == 1
    assert results[0].outcome == "recovered"
    mock_client.fetch_payment_link.assert_called_once_with("plink_NOSERVER")


# ---------------------------------------------------------------------------
# 5. fp cost: 3 seeded FP contacts -> exact rupee figure, net == gross - fp
# ---------------------------------------------------------------------------


def test_fp_cost_exact_figure_and_net_nets_against_gross(
    temp_db: tuple[Path, sqlite3.Connection],
) -> None:
    _, conn = temp_db
    _seed_run(conn)
    model = OutcomeAssumptions(sms_cost_paise=20, goodwill_cost_paise=1500)

    # FP #1: contacted after a terminal event already existed for that payment.
    _seed_customer(conn, "cust_fp1")
    _seed_episode(conn, "epi_fp1", "pay_fp1", customer_id="cust_fp1")
    t0 = datetime(2026, 9, 1, 6, 0, tzinfo=IST)
    _seed_terminal_webhook(
        conn, "evt_fp1", event_type="payment.captured", received_at=t0,
        payment_id="pay_fp1",
    )
    _seed_execution(
        conn, "exec_fp1", "epi_fp1", plink_id="plink_fp1", created_at=t0 + timedelta(hours=1)
    )

    # FP #2: contacted after the customer had already opted out.
    _seed_customer(conn, "cust_fp2", opted_out=True, opt_out_ts=_iso(t0))
    _seed_episode(conn, "epi_fp2", "pay_fp2", customer_id="cust_fp2")
    _seed_execution(
        conn, "exec_fp2", "epi_fp2", plink_id="plink_fp2", created_at=t0 + timedelta(hours=1)
    )

    # FP #3: contacted a third time inside the 7-day/2-contact frequency window.
    _seed_customer(conn, "cust_fp3")
    _seed_episode(conn, "epi_fp3a", "pay_fp3a", customer_id="cust_fp3")
    _seed_episode(conn, "epi_fp3b", "pay_fp3b", customer_id="cust_fp3")
    _seed_episode(conn, "epi_fp3c", "pay_fp3c", customer_id="cust_fp3")
    _seed_execution(conn, "exec_fp3a", "epi_fp3a", plink_id="plink_fp3a", created_at=t0)
    _seed_execution(
        conn, "exec_fp3b", "epi_fp3b", plink_id="plink_fp3b", created_at=t0 + timedelta(hours=2)
    )
    _seed_execution(
        conn, "exec_fp3c", "epi_fp3c", plink_id="plink_fp3c", created_at=t0 + timedelta(hours=4)
    )

    # One clean, genuinely recovered contact — proves net = gross - fp, not
    # just fp_cost computed in isolation.
    _seed_episode(conn, "epi_clean", "pay_clean", amount_paise=50000)
    _seed_execution(conn, "exec_clean", "epi_clean", plink_id="plink_clean", created_at=t0)
    clean_execution = ExecutionRecord(
        execution_id="exec_clean", episode_id="epi_clean", payment_id="pay_clean",
        order_id=None, plink_id="plink_clean", amount_paise=50000, created_at=t0,
    )
    clean_event = OutcomeEvent(
        event_type="payment_link.paid", payment_id="pay_recovered_clean", order_id=None,
        plink_id="plink_clean", amount_paise=50000, occurred_at=t0 + timedelta(hours=1),
    )
    post_gross(conn, "run_attr", attribute(clean_execution, clean_event, window_hours=48))

    fp = compute_fp_cost(conn, "run_attr", model)

    # exec_fp3a and exec_fp3b are the two contacts that seed the frequency
    # window; exec_fp3c is the one that actually breaches it (>= 2 prior).
    assert fp.fp_count == 3
    assert fp.cost_paise == 3 * (20 + 1500)

    net_paise = post_net(conn, "run_attr")
    gross_paise = get_ledger_total(conn, "run_attr", "gross_recovery")
    assert gross_paise == 50000
    assert net_paise == gross_paise - fp.cost_paise


def test_parse_outcome_assumptions_reads_the_committed_file() -> None:
    assumptions = parse_outcome_assumptions(ROOT / "outcome_model.md")
    assert assumptions.sms_cost_paise == 20
    assert assumptions.goodwill_cost_paise == 1500


# ---------------------------------------------------------------------------
# 6. gate-disabled counterfactual reports a higher FP count than gate-enabled
# ---------------------------------------------------------------------------


def test_gate_disabled_counterfactual_exceeds_gate_enabled_fp_count(
    temp_db: tuple[Path, sqlite3.Connection],
) -> None:
    _, conn = temp_db
    _seed_run(conn)
    model = OutcomeAssumptions(sms_cost_paise=20, goodwill_cost_paise=1500)
    g = _guardrails()

    episodes = [
        _episode(episode_id="e1", payment_id="p1", customer_id="c1", already_paid_elsewhere=True),
        _episode(episode_id="e2", payment_id="p2", customer_id="c2", already_paid_elsewhere=True),
        _episode(episode_id="e3", payment_id="p3", customer_id="c3"),  # opted out (via param below)
        _episode(episode_id="e4", payment_id="p4", customer_id="c4"),  # clean
    ]
    opted_out = frozenset({"c3"})

    # Gate-enabled reality: the real gate suppressed every risky episode
    # above, so nothing was ever contacted for them — zero executions,
    # hence compute_fp_cost() over the (empty) actual run finds nothing.
    gate_enabled = compute_fp_cost(conn, "run_attr", model)
    assert gate_enabled.fp_count == 0

    counterfactual = compute_gate_disabled_counterfactual(conn, episodes, opted_out, g)

    assert counterfactual.fp_count == 3  # e1, e2 (terminal_seen), e3 (opted out)
    assert counterfactual.fp_count > gate_enabled.fp_count


# ---------------------------------------------------------------------------
# 7. attribution_window_hours drives behaviour
# ---------------------------------------------------------------------------


def test_window_hours_changes_the_attributed_outcome() -> None:
    created_at = datetime(2026, 9, 1, 0, 0, tzinfo=IST)
    execution = ExecutionRecord(
        execution_id="exec_7", episode_id="epi_7", payment_id="pay_7",
        order_id=None, plink_id="plink_WIN", amount_paise=10000, created_at=created_at,
    )
    event = OutcomeEvent(
        event_type="payment_link.paid", payment_id="pay_recovered_7", order_id=None,
        plink_id="plink_WIN", amount_paise=10000, occurred_at=created_at + timedelta(hours=30),
    )

    with_48h_window = attribute(execution, event, window_hours=48)
    with_24h_window = attribute(execution, event, window_hours=24)

    assert with_48h_window.outcome == "recovered"
    assert with_24h_window.outcome == "not_recovered"
    assert with_24h_window.reason_code == REASON_OUTSIDE_WINDOW


# ---------------------------------------------------------------------------
# Bonus: the remaining edge-case reason codes rules.py commits to.
# ---------------------------------------------------------------------------


def test_partial_payment_is_not_attributed() -> None:
    created_at = datetime(2026, 9, 1, 0, 0, tzinfo=IST)
    execution = ExecutionRecord(
        execution_id="exec_8", episode_id="epi_8", payment_id="pay_8",
        order_id=None, plink_id="plink_PARTIAL", amount_paise=10000, created_at=created_at,
    )
    event = OutcomeEvent(
        event_type="payment_link.paid", payment_id="pay_recovered_8", order_id=None,
        plink_id="plink_PARTIAL", amount_paise=6000,  # less than the 10000 asked for
        occurred_at=created_at + timedelta(hours=1),
    )

    result = attribute(execution, event, window_hours=48)

    assert result.outcome == "not_recovered"
    assert result.reason_code == REASON_PARTIAL_PAYMENT


def test_recovery_via_a_channel_we_did_not_create_is_not_claimed() -> None:
    created_at = datetime(2026, 9, 1, 0, 0, tzinfo=IST)
    execution = ExecutionRecord(
        execution_id="exec_9", episode_id="epi_9", payment_id="pay_9",
        order_id=None, plink_id="plink_OURS", amount_paise=10000, created_at=created_at,
    )
    event = OutcomeEvent(
        event_type="payment.captured", payment_id="pay_9", order_id=None,
        plink_id="plink_SOMEONE_ELSES", amount_paise=10000,
        occurred_at=created_at + timedelta(hours=1),
    )

    result = attribute(execution, event, window_hours=48)

    assert result.outcome == "not_recovered"
    assert result.reason_code == REASON_UNATTRIBUTABLE


def test_link_expired_is_not_recovered() -> None:
    created_at = datetime(2026, 9, 1, 0, 0, tzinfo=IST)
    execution = ExecutionRecord(
        execution_id="exec_10", episode_id="epi_10", payment_id="pay_10",
        order_id=None, plink_id="plink_EXPIRE", amount_paise=10000, created_at=created_at,
    )
    event = OutcomeEvent(
        event_type="payment_link.expired", payment_id=None, order_id=None,
        plink_id="plink_EXPIRE", amount_paise=None,
        occurred_at=created_at + timedelta(days=7),
    )

    result = attribute(execution, event, window_hours=48)

    assert result.outcome == "not_recovered"
    assert result.reason_code == REASON_LINK_EXPIRED
