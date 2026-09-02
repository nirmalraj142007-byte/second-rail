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

import json
import sqlite3
from typing import TYPE_CHECKING

from src.errors import DuplicateEventError, IdempotencyCollision

if TYPE_CHECKING:
    from src.config_models import PolicyTable


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
    order_id: str | None = None,
    amount_paise: int | None = None,
) -> None:
    try:
        conn.execute(
            """
            INSERT INTO webhook_event (
                event_id, event_type, payment_id, plink_id, order_id,
                amount_paise, raw_body_hash, signature_valid, received_at,
                processed, dedup_result
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                event_id, event_type, payment_id, plink_id, order_id,
                amount_paise, raw_body_hash, int(signature_valid), received_at,
                int(processed), dedup_result,
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


def upsert_policy_rules(conn: sqlite3.Connection, policy: PolicyTable) -> None:
    """Mirror config/policy_table.yaml's rules (plus a synthetic
    'default_rule' row for the catch-all) into the policy_rule table, so
    decision.policy_rule_id's FK has something to point at. Config is the
    source of truth — this is a queryable mirror, INSERT OR REPLACE'd fresh
    on every run in case the config changed since the last one, the same
    pattern load_and_upsert_customers() uses for data/customers.jsonl."""
    for r in policy.rules:
        conn.execute(
            """
            INSERT OR REPLACE INTO policy_rule (
                policy_rule_id, cause_class, amount_band, segment, instrument,
                admissible_actions, escalation_tier, justification
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                r.policy_rule_id, r.cause_class, r.amount_band, r.segment, r.instrument,
                json.dumps(r.admissible_actions), r.escalation_tier, r.justification,
            ),
        )
    d = policy.default_rule
    conn.execute(
        """
        INSERT OR REPLACE INTO policy_rule (
            policy_rule_id, cause_class, amount_band, segment, instrument,
            admissible_actions, escalation_tier, justification
        ) VALUES ('default_rule', NULL, NULL, NULL, NULL, ?, ?, ?)
        """,
        (json.dumps(d.admissible_actions), d.escalation_tier, d.justification),
    )
    conn.commit()


def insert_approval(
    conn: sqlite3.Connection,
    *,
    approval_id: str,
    episode_id: str,
    required: bool,
    tier: str,
    approved_by: str | None,
    approved_at: str | None,
    rejected_reason: str | None,
    expires_at: str | None,
) -> None:
    """One row per human_keystroke-tier episode that actually reached a
    human decision — via the interactive prompt (src/ui/live.py) or the
    JSON-queue fallback (src/ui/approve.py). Both paths write through this
    same function, so `approval` stays the single system of record either
    way."""
    conn.execute(
        """
        INSERT INTO approval (
            approval_id, episode_id, required, tier, approved_by, approved_at,
            rejected_reason, expires_at
        ) VALUES (?,?,?,?,?,?,?,?)
        """,
        (
            approval_id, episode_id, int(required), tier, approved_by, approved_at,
            rejected_reason, expires_at,
        ),
    )
    conn.commit()


def insert_decision(
    conn: sqlite3.Connection,
    *,
    decision_id: str,
    episode_id: str,
    policy_rule_id: str,
    candidate_actions: list[str],
    chosen_action: str,
    features_used: list[str],
    inside_admissible_set: bool,
    escalation_tier: str,
    decided_at: str,
) -> None:
    """UNIQUE(episode_id) — one decision per episode, ever, mirroring
    "one recovery attempt per payment_id" (config/guardrails.yaml:
    max_actions_per_payment)."""
    conn.execute(
        """
        INSERT INTO decision (
            decision_id, episode_id, policy_rule_id, candidate_actions,
            chosen_action, features_used, inside_admissible_set,
            escalation_tier, decided_at
        ) VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (
            decision_id, episode_id, policy_rule_id, json.dumps(candidate_actions),
            chosen_action, json.dumps(features_used), int(inside_admissible_set),
            escalation_tier, decided_at,
        ),
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


def get_terminal_webhook_events(
    conn: sqlite3.Connection,
    *,
    payment_id: str | None,
    order_id: str | None,
    plink_id: str | None,
) -> list[sqlite3.Row]:
    """Every terminal-event webhook_event row (payment.captured,
    payment_link.paid, payment_link.expired) that could plausibly be the
    outcome of a given execution: matched by plink_id, order_id, or
    payment_id, in that preference order at the call site (this just
    returns every candidate, sorted earliest-first — src/attribute/rules.py
    decides which one actually counts and why)."""
    clauses = []
    params: list[str] = []
    if plink_id:
        clauses.append("plink_id = ?")
        params.append(plink_id)
    if order_id:
        clauses.append("order_id = ?")
        params.append(order_id)
    if payment_id:
        clauses.append("payment_id = ?")
        params.append(payment_id)
    if not clauses:
        return []
    where = " OR ".join(clauses)
    rows = conn.execute(
        f"""
        SELECT * FROM webhook_event
        WHERE event_type IN ('payment.captured','payment_link.paid','payment_link.expired')
          AND ({where})
        ORDER BY received_at ASC
        """,
        params,
    ).fetchall()
    return rows


def get_executions_for_run(conn: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    """Every execution this run actually created, joined to the episode data
    attribution needs (payment_id, order_id, customer_id, the original
    amount) — one row per link created, oldest first."""
    return conn.execute(
        """
        SELECT ex.execution_id, ex.episode_id, ex.plink_id, ex.created_at,
               ep.payment_id, ep.order_id, ep.customer_id, ep.amount_paise
        FROM execution ex
        JOIN episode ep ON ep.episode_id = ex.episode_id
        WHERE ex.run_id = ? AND ex.status = 'created'
        ORDER BY ex.created_at ASC
        """,
        (run_id,),
    ).fetchall()


def insert_attribution(
    conn: sqlite3.Connection,
    *,
    attribution_id: str,
    episode_id: str,
    execution_id: str | None,
    outcome: str,
    reason_code: str,
    recovered_amount_paise: int | None,
    window_hours: int,
    attributed_at: str,
    attribution_rule_id: str,
) -> None:
    """INSERT OR REPLACE keyed on the UNIQUE(episode_id) constraint — a
    re-run of the watcher for the same run replaces the prior verdict for
    an episode rather than accumulating a second row."""
    conn.execute(
        """
        INSERT INTO attribution (
            attribution_id, episode_id, execution_id, outcome, reason_code,
            recovered_amount_paise, window_hours, attributed_at, attribution_rule_id
        ) VALUES (?,?,?,?,?,?,?,?,?)
        ON CONFLICT(episode_id) DO UPDATE SET
            execution_id = excluded.execution_id,
            outcome = excluded.outcome,
            reason_code = excluded.reason_code,
            recovered_amount_paise = excluded.recovered_amount_paise,
            window_hours = excluded.window_hours,
            attributed_at = excluded.attributed_at,
            attribution_rule_id = excluded.attribution_rule_id
        """,
        (
            attribution_id, episode_id, execution_id, outcome, reason_code,
            recovered_amount_paise, window_hours, attributed_at, attribution_rule_id,
        ),
    )
    conn.commit()


def get_attributions_for_run(conn: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT a.* FROM attribution a
        JOIN execution ex ON ex.execution_id = a.execution_id
        WHERE ex.run_id = ?
        """,
        (run_id,),
    ).fetchall()


def insert_ledger_entry(
    conn: sqlite3.Connection,
    *,
    entry_id: str,
    run_id: str,
    episode_id: str | None,
    kind: str,
    amount_paise: int,
    basis: str | None,
) -> None:
    conn.execute(
        """
        INSERT INTO ledger_entry (entry_id, run_id, episode_id, kind, amount_paise, basis)
        VALUES (?,?,?,?,?,?)
        """,
        (entry_id, run_id, episode_id, kind, amount_paise, basis),
    )
    conn.commit()


def get_ledger_total(conn: sqlite3.Connection, run_id: str, kind: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(amount_paise), 0) AS total FROM ledger_entry "
        "WHERE run_id = ? AND kind = ?",
        (run_id, kind),
    ).fetchone()
    return int(row["total"])


def get_customer(conn: sqlite3.Connection, customer_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM customer WHERE customer_id = ?", (customer_id,)
    ).fetchone()


def count_prior_created_contacts(
    conn: sqlite3.Connection, customer_id: str, *, before_iso: str, since_iso: str
) -> int:
    """How many 'created' executions this customer already had in
    [since_iso, before_iso) — used by the false-positive frequency-cap
    check, which must count contacts strictly before the one being judged
    so a contact never counts against itself."""
    row = conn.execute(
        """
        SELECT COUNT(*) AS n
        FROM execution ex
        JOIN episode ep ON ep.episode_id = ex.episode_id
        WHERE ep.customer_id = ? AND ex.status = 'created'
          AND ex.created_at >= ? AND ex.created_at < ?
        """,
        (customer_id, since_iso, before_iso),
    ).fetchone()
    return int(row["n"])


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
