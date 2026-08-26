from __future__ import annotations

import sqlite3

import pytest

from src.db.migrate import db_check, get_connection, migrate
from src.db.repo import insert_episode, insert_execution
from src.errors import DuplicateEventError, IdempotencyCollision

# The Phase 2 prompt's own header says "Tables (14)" but its detailed spec
# (matching second-rail-build-blueprint.md §6 exactly) defines 16 named
# tables, including harvested_error and exception_entry — both load-bearing
# for other phases (M-08, C-05). Treating that as a miscount in the prompt,
# not a signal to drop two blueprint-mandated tables; see BUILD_LOG.md.
EXPECTED_TABLE_COUNT = 16


def _episode_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = dict(
        episode_id="ep_1",
        payment_id="pay_dup",
        order_id=None,
        customer_id=None,
        amount_paise=100,
        currency="INR",
        instrument="card",
        issuer_family=None,
        error_code="BAD_REQUEST_ERROR",
        error_description="Payment failed",
        error_source="gateway",
        error_step="payment_authorization",
        error_reason="payment_failed",
        failed_at="2026-08-26T10:00:00+05:30",
        received_at="2026-08-26T10:00:01+05:30",
        split="train",
        is_synthetic=True,
        harvested_from=None,
    )
    base.update(overrides)
    return base


def _execution_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = dict(
        execution_id="exec_1",
        episode_id="ep_1",
        idempotency_key="idem_dup",
        reference_id="ref_1",
        api="paymentLink.create",
        plink_id=None,
        short_url=None,
        request_body_hash=None,
        response_code=200,
        attempt=1,
        delay_ms=0,
        status="created",
        run_id=None,
        created_at="2026-08-26T10:00:02+05:30",
    )
    base.update(overrides)
    return base


def test_migrate_is_idempotent_and_creates_every_table(tmp_path):
    db_path = tmp_path / "second_rail.db"

    first = migrate(db_path)
    second = migrate(db_path)

    assert len(first) == EXPECTED_TABLE_COUNT
    assert first == second
    assert db_check(db_path) == {name: 0 for name in first}


def test_duplicate_idempotency_key_raises_idempotency_collision(tmp_path):
    db_path = tmp_path / "second_rail.db"
    migrate(db_path)
    conn = get_connection(db_path)
    insert_episode(conn, **_episode_kwargs())
    insert_execution(conn, **_execution_kwargs())

    with pytest.raises(IdempotencyCollision):
        insert_execution(conn, **_execution_kwargs(execution_id="exec_2"))

    conn.close()


def test_duplicate_payment_id_raises_duplicate_event_error(tmp_path):
    db_path = tmp_path / "second_rail.db"
    migrate(db_path)
    conn = get_connection(db_path)
    insert_episode(conn, **_episode_kwargs())

    with pytest.raises(DuplicateEventError):
        insert_episode(conn, **_episode_kwargs(episode_id="ep_2"))

    conn.close()


def test_duplicate_policy_rule_key_raises(tmp_path):
    db_path = tmp_path / "second_rail.db"
    migrate(db_path)
    conn = get_connection(db_path)
    rule = (
        "P-01", "issuer_technical_decline", "low", "repeat", "upi",
        '["link_upi_alt"]', "auto", "test fixture",
    )
    conn.execute(
        """
        INSERT INTO policy_rule (
            policy_rule_id, cause_class, amount_band, segment, instrument,
            admissible_actions, escalation_tier, justification
        ) VALUES (?,?,?,?,?,?,?,?)
        """,
        rule,
    )
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO policy_rule (
                policy_rule_id, cause_class, amount_band, segment, instrument,
                admissible_actions, escalation_tier, justification
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            ("P-02",) + rule[1:],
        )

    conn.close()
