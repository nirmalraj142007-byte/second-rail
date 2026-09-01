"""Shared plumbing for the guardrail-threshold sweep experiments in this
directory (Phase 14 — see CLAUDE.md's non-negotiable that every threshold
in `config/guardrails.yaml` traces to either an experiment or an explicit
"not experimentally derived" marker).

Not an experiment itself. `run_auto_approve.py` and `run_outage_cluster.py`
both need to evaluate the real `src.gate.engine.GateEngine` per episode
over `data/train.jsonl` and inspect the outcome (eligibility, escalation
tier, cluster membership) for that one episode — `src.runner.Runner`
doesn't expose that; it only returns run-level aggregates
(`RunSummary.by_outcome` / `by_escalation_tier`), which is enough for a
live run's own reporting but not enough to build a per-threshold-setting
trade-off table. So these two scripts drive `GateEngine` directly, the
same way `src/runner.py` itself does internally, rather than going through
`Runner.run()`.

`run_retry_cap.py` does not use this module — its scope is the executor's
retry/backoff code, not the gate, and it needs no episode/customer DB at
all.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from src.db.migrate import get_connection, migrate
from src.db.repo import insert_episode
from src.gate.checks import Episode
from src.runner import load_and_upsert_customers, load_episodes

ROOT = Path(__file__).resolve().parent.parent.parent
TRAIN_PATH = ROOT / "data" / "train.jsonl"
CUSTOMERS_PATH = ROOT / "data" / "customers.jsonl"


def load_train_episodes() -> list[Episode]:
    return load_episodes([TRAIN_PATH])


def fresh_conn():
    """A fresh, empty, on-disk sqlite DB — on disk, not `:memory:`,
    because `migrate()` opens and closes its own connection first, and two
    separate connections to `:memory:` are two separate, unrelated
    databases. Customers are loaded so `check_opt_out` (src/gate/checks.py)
    sees the real opt-out set; the `episode` table starts empty so
    `check_duplicate` only fires on a genuine duplicate `payment_id` within
    this sweep setting's own pass, never carried over from a previous one.
    """
    tmp_dir = tempfile.mkdtemp(prefix="second_rail_threshold_sweep_")
    db_path = Path(tmp_dir) / "sweep.db"
    migrate(db_path)
    conn = get_connection(db_path)
    load_and_upsert_customers(conn, CUSTOMERS_PATH)
    return conn


def record_episode(conn, episode: Episode) -> None:
    """Mirrors `src/runner.py`'s `_EpisodeRow.kwargs()` mapping, so a
    later literal duplicate `payment_id` in the same sweep pass is caught
    by `check_duplicate` exactly as it would be in a real `Runner.run()`."""
    e = episode
    insert_episode(
        conn,
        episode_id=e.episode_id,
        payment_id=e.payment_id,
        order_id=e.order_id,
        customer_id=e.customer_id,
        amount_paise=e.amount_paise,
        currency=e.currency,
        instrument=e.instrument,
        issuer_family=e.issuer_family,
        error_code=e.error_code,
        error_description=e.error_description,
        error_source=e.error_source,
        error_step=e.error_step,
        error_reason=e.error_reason,
        failed_at=e.failed_at.isoformat(timespec="seconds"),
        received_at=e.received_at.isoformat(timespec="seconds"),
        split=e.split,
        is_synthetic=e.is_synthetic,
        harvested_from=e.harvested_from,
    )
