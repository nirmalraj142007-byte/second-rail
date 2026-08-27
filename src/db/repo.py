"""Thin, typed SQLite access. No business logic.

Every function takes a sqlite3.Connection as its first argument — there is
no module-level global connection, because the eval harness and the
webhook server run in different processes and each opens its own
connection via migrate.get_connection().

On a UNIQUE-constraint IntegrityError, the specific expected-control-flow
error from src/errors.py is raised instead — DuplicateEventError for a
replayed episode/webhook event, IdempotencyCollision for a repeated
execution. A bare sqlite3.IntegrityError never escapes this module for
those two paths.
"""

from __future__ import annotations

import sqlite3

from src.errors import DuplicateEventError, IdempotencyCollision


def insert_customer_if_absent(
    conn: sqlite3.Connection,
    *,
    customer_id: str,
    synthetic_name: str | None,
    contact_hash: str,
    email_hash: str | None,
    segment: str | None,
    opted_out: bool,
    opt_out_ts: str | None,
    created_at: str,
) -> None:
    """INSERT OR IGNORE — customers are loaded fresh from data/customers.jsonl
    on every `make gate-run`, and a re-run against an un-reset database must
    not crash on the customer_id PRIMARY KEY."""
    conn.execute(
        """
        INSERT OR IGNORE INTO customer (
            customer_id, synthetic_name, contact_hash, email_hash, segment,
            opted_out, opt_out_ts, created_at
        ) VALUES (?,?,?,?,?,?,?,?)
        """,
        (
            customer_id, synthetic_name, contact_hash, email_hash, segment,
            int(opted_out), opt_out_ts, created_at,
        ),
    )
    conn.commit()


def get_opted_out_customer_ids(conn: sqlite3.Connection) -> frozenset[str]:
    rows = conn.execute("SELECT customer_id FROM customer WHERE opted_out = 1").fetchall()
    return frozenset(row["customer_id"] for row in rows)


def insert_episode(
    conn: sqlite3.Connection,
    *,
    episode_id: str,
    payment_id: str,
    order_id: str | None,
    customer_id: str | None,
    amount_paise: int,
    currency: str,
    instrument: str | None,
    issuer_family: str | None,
    error_code: str | None,
    error_description: str | None,
    error_source: str | None,
    error_step: str | None,
    error_reason: str | None,
    failed_at: str,
    received_at: str,
    split: str | None,
    is_synthetic: bool,
    harvested_from: str | None,
) -> None:
    try:
        conn.execute(
            """
            INSERT INTO episode (
                episode_id, payment_id, order_id, customer_id, amount_paise,
                currency, instrument, issuer_family, error_code,
                error_description, error_source, error_step, error_reason,
                failed_at, received_at, split, is_synthetic, harvested_from
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                episode_id, payment_id, order_id, customer_id, amount_paise,
                currency, instrument, issuer_family, error_code,
                error_description, error_source, error_step, error_reason,
                failed_at, received_at, split, int(is_synthetic), harvested_from,
            ),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise DuplicateEventError(
            f"episode with payment_id={payment_id!r} already exists",
            remediation="expected control flow for a replayed webhook — not a bug",
            code="DUPLICATE_PAYMENT_ID",
        ) from exc


def get_episode_by_payment_id(conn: sqlite3.Connection, payment_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM episode WHERE payment_id = ?", (payment_id,)
    ).fetchone()


def count_contacts_in_window(conn: sqlite3.Connection, customer_id: str, since_iso: str) -> int:
    """How many contact attempts (created Payment Links) this customer has
    had since since_iso. The frequency-cap threshold itself lives in
    src/gate/, not here — this just answers "how many"."""
    row = conn.execute(
        """
        SELECT COUNT(*) AS n
        FROM execution
        JOIN episode ON episode.episode_id = execution.episode_id
        WHERE episode.customer_id = ?
          AND execution.status = 'created'
          AND execution.created_at >= ?
        """,
        (customer_id, since_iso),
    ).fetchone()
    return int(row["n"])


def insert_execution(
    conn: sqlite3.Connection,
    *,
    execution_id: str,
    episode_id: str,
    idempotency_key: str,
    reference_id: str | None,
    api: str | None,
    plink_id: str | None,
    short_url: str | None,
    request_body_hash: str | None,
    response_code: int | None,
    attempt: int | None,
    delay_ms: int | None,
    status: str,
    run_id: str | None,
    created_at: str | None,
    cancelled_at: str | None = None,
) -> None:
    try:
        conn.execute(
            """
            INSERT INTO execution (
                execution_id, episode_id, idempotency_key, reference_id, api,
                plink_id, short_url, request_body_hash, response_code,
                attempt, delay_ms, status, run_id, created_at, cancelled_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                execution_id, episode_id, idempotency_key, reference_id, api,
                plink_id, short_url, request_body_hash, response_code,
                attempt, delay_ms, status, run_id, created_at, cancelled_at,
            ),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise IdempotencyCollision(
            f"execution with idempotency_key={idempotency_key!r} already exists",
            remediation="this is the success path — it proves duplicate-link avoidance",
            code="DUPLICATE_IDEMPOTENCY_KEY",
        ) from exc


def get_execution_by_idempotency_key(
    conn: sqlite3.Connection, idempotency_key: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM execution WHERE idempotency_key = ?", (idempotency_key,)
    ).fetchone()


def insert_webhook_event(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    event_type: str | None,
    payment_id: str | None,
    plink_id: str | None,
    raw_body_hash: str | None,
    signature_valid: bool,
    received_at: str,
    processed: bool,
    dedup_result: str,
) -> None:
    try:
        conn.execute(
            """
            INSERT INTO webhook_event (
                event_id, event_type, payment_id, plink_id, raw_body_hash,
                signature_valid, received_at, processed, dedup_result
            ) VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                event_id, event_type, payment_id, plink_id, raw_body_hash,
                int(signature_valid), received_at, int(processed), dedup_result,
            ),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise DuplicateEventError(
            f"webhook_event with event_id={event_id!r} already exists",
            remediation="expected control flow for a replayed webhook — not a bug",
            code="DUPLICATE_WEBHOOK_EVENT",
        ) from exc


def get_webhook_event(conn: sqlite3.Connection, event_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM webhook_event WHERE event_id = ?", (event_id,)
    ).fetchone()


def insert_gate_check(
    conn: sqlite3.Connection,
    *,
    check_id: str,
    episode_id: str,
    check_name: str,
    result: str,
    reason: str | None,
    evaluated_at: str,
    order_index: int,
) -> None:
    conn.execute(
        """
        INSERT INTO gate_check (
            check_id, episode_id, check_name, result, reason, evaluated_at, order_index
        ) VALUES (?,?,?,?,?,?,?)
        """,
        (check_id, episode_id, check_name, result, reason, evaluated_at, order_index),
    )
    conn.commit()


def insert_exception_entry(
    conn: sqlite3.Connection,
    *,
    exception_id: str,
    run_id: str | None,
    episode_id: str | None,
    stage: str,
    reason_code: str,
    reason_text: str | None,
    excluded_from_recovery: bool = True,
) -> None:
    conn.execute(
        """
        INSERT INTO exception_entry (
            exception_id, run_id, episode_id, stage, reason_code, reason_text,
            excluded_from_recovery
        ) VALUES (?,?,?,?,?,?,?)
        """,
        (
            exception_id, run_id, episode_id, stage, reason_code, reason_text,
            int(excluded_from_recovery),
        ),
    )
    conn.commit()


def start_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    started_at: str,
    mode: str,
    git_sha: str | None = None,
    config_hash: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO run (run_id, started_at, mode, git_sha, config_hash)
        VALUES (?,?,?,?,?)
        """,
        (run_id, started_at, mode, git_sha, config_hash),
    )
    conn.commit()


def end_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    ended_at: str,
    episode_count: int | None = None,
    stopped_reason: str | None = None,
    llm_cost_paise: int | None = None,
    throughput_epm: float | None = None,
) -> None:
    conn.execute(
        """
        UPDATE run
        SET ended_at = ?, episode_count = ?, stopped_reason = ?,
            llm_cost_paise = ?, throughput_epm = ?
        WHERE run_id = ?
        """,
        (ended_at, episode_count, stopped_reason, llm_cost_paise, throughput_epm, run_id),
    )
    conn.commit()


def insert_audit_record(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    run_id: str | None,
    episode_id: str | None,
    actor: str,
    stage: str,
    inputs_hash: str | None,
    prev_hash: str,
    audit_hash: str,
    seq: int,
) -> None:
    """Mirror one row into audit_record. The JSONL file is the source of
    truth (src/audit/writer.py); this is a queryable index over it, not a
    second copy of record content — only the chain-verification fields and
    enough context to look a record up are stored here."""
    conn.execute(
        """
        INSERT INTO audit_record (
            event_id, run_id, episode_id, actor, stage, inputs_hash,
            prev_hash, hash, seq
        ) VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (event_id, run_id, episode_id, actor, stage, inputs_hash, prev_hash, audit_hash, seq),
    )
    conn.commit()
