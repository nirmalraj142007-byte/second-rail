"""guardrail_proof.py — the headline non-circular metric.

Runs N real test-mode Payment Link creations under a fault plan that
injects a 429, a timeout, a 5xx and a duplicate reference id at
deterministic points across the run, then re-submits every episode a
second time through the same (fault-exhausted) executor to test
idempotency detection at scale. Reports:

  duplicate links created            (must be 0 — verified against the
                                       real Razorpay API, not just the
                                       local database)
  cap breaches                       (must be 0)
  quiet-hour contacts                (must be 0)
  idempotency collisions correctly detected   (k/N)

Nothing about this measurement passes through the customer-response
simulator in outcome_model.md — it is a real measurement of a real
system's behaviour against real API responses (or, under
`--dry-run-first`, against FixtureExecutor, to validate the plan's wiring
and index arithmetic before spending a real API call).

Results are written to evidence/guardrail_proof.json. Every link created
is cancelled in a `finally` block, and the cancel count is reported —
this script's whole purpose is proving correctness, not leaving live
state behind.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import typer
from ulid import ULID

from src.audit.writer import AuditWriter
from src.config import load_settings, require_razorpay
from src.config_models import load_all
from src.db.migrate import get_connection, migrate
from src.db.repo import get_opted_out_customer_ids
from src.execute.executor import Executor, FixtureExecutor, RazorpayExecutor
from src.execute.faults import FaultInjectingExecutor, FaultPlan
from src.gate.checks import ReasonCode
from src.logging_setup import get_logger, setup_logging
from src.razorpay_client import RazorpayClient
from src.runner import (
    Runner,
    load_and_upsert_customers,
    load_episodes,
    select_gate_eligible_slice,
)

ROOT = Path(__file__).resolve().parent.parent
TRAIN_SOURCE = ROOT / "data" / "train.jsonl"
CUSTOMERS_PATH = ROOT / "data" / "customers.jsonl"
FIXTURE_DIR = ROOT / "fixtures" / "payment_links"
DEMO_DB_PATH = ROOT / "evidence" / "guardrail_proof.db"
OUTPUT_PATH = ROOT / "evidence" / "guardrail_proof.json"
PLACEHOLDER_ACTION = "placeholder_action"
PLACEHOLDER_POLICY_RULE = "P-00"

logger = get_logger("guardrail_proof")


def _reset_db() -> None:
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(DEMO_DB_PATH) + suffix)
        if p.exists():
            p.unlink()


def _build_fault_plan(n: int) -> FaultPlan:
    """4 deterministic fault points spread across the run — one of each
    kind FaultPlan supports. FaultPlan carries a single index per kind, not
    a list, so at N=200 this injects 4 total faults, not "every Kth
    episode" — a real constraint of the type as specified (see
    src/execute/faults.py), not an oversight."""
    if n < 5:
        raise ValueError("--n must be >= 5 so the 4 fault indices stay distinct and in range")
    return FaultPlan(
        inject_429_at_episode_index=max(1, n // 5),
        inject_429_repeat=3,
        inject_timeout_at_index=max(2, 2 * n // 5),
        inject_5xx_at_index=max(3, 3 * n // 5),
        inject_duplicate_reference_at_index=max(4, 4 * n // 5),
    )


def main(
    n: int = typer.Option(200, "--n", help="Number of episodes to run."),
    dry_run_first: bool = typer.Option(
        False,
        "--dry-run-first",
        help="Validate the plan end-to-end with FixtureExecutor before spending a real call.",
    ),
    consecutive_error_tolerance: int = typer.Option(
        5,
        "--consecutive-error-tolerance",
        help=(
            "Overrides guardrails.yaml's consecutive_executor_errors_stop (3) for THIS "
            "tool only — config/guardrails.yaml itself is never touched, and every other "
            "code path (src/runner.py's production runs, make demo, make eval) keeps "
            "reading the shared default of 3 from the file. Raised here because sustained "
            "real-API calls at volume produce sporadic Razorpay-side 429s (confirmed via "
            "real response bodies, not injected faults, not a bug) that are not a systemic "
            "failure signal specifically for a tool whose entire job is deliberately "
            "hammering the real API at volume — see BUILD_LOG.md."
        ),
    ),
) -> None:
    setup_logging()
    settings = load_settings()
    bundle = load_all()
    if consecutive_error_tolerance != bundle.guardrails.consecutive_executor_errors_stop:
        bundle = bundle.model_copy(
            update={
                "guardrails": bundle.guardrails.model_copy(
                    update={"consecutive_executor_errors_stop": consecutive_error_tolerance}
                )
            }
        )
    g = bundle.guardrails

    DEMO_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    _reset_db()
    migrate(DEMO_DB_PATH)
    conn = get_connection(DEMO_DB_PATH)

    episodes_all = load_episodes([TRAIN_SOURCE])
    load_and_upsert_customers(conn, CUSTOMERS_PATH)
    opted_out = get_opted_out_customer_ids(conn)
    episode_slice = select_gate_eligible_slice(conn, episodes_all, g, opted_out, n)

    plan = _build_fault_plan(n)
    run_id = str(ULID())
    audit = AuditWriter(run_id, settings.audit_dir, conn)

    client: RazorpayClient | None = None
    inner_executor: Executor | None = None
    created_plink_ids: list[str] = []
    cancelled_count = 0
    result: dict | None = None

    try:
        if dry_run_first:
            print(f"guardrail-proof --dry-run-first: validating plan for N={n} episodes "
                  "with FixtureExecutor (zero network calls)...")
            inner_executor = FixtureExecutor(fixture_dir=FIXTURE_DIR)
        else:
            key_id, key_secret = require_razorpay(settings)
            client = RazorpayClient(key_id, key_secret)
            inner_executor = RazorpayExecutor(
                conn=conn, client=client, mode="execute", run_id=run_id, audit=audit,
                retry_cap=g.executor_retry_cap,
                retry_delays=[float(s) for s in g.executor_backoff_seconds],
            )

        faulty_executor = FaultInjectingExecutor(inner_executor, plan)
        faulty_executor.start_run(len(episode_slice))

        runner = Runner(conn, audit, bundle, settings, executor=faulty_executor)
        mode = "fixture" if dry_run_first else "execute"
        summary = runner.run(episode_slice, mode, run_id=run_id)
        print(f"first pass: {summary.by_outcome}")

        # Second pass: re-submit every episode the FIRST pass actually
        # reached through the SAME (now fault-exhausted) rig. Every
        # configured fault index is <= n, and the rig's internal counter
        # keeps climbing past n on this second pass, so none of the four
        # faults fire again — every call here goes straight to the inner
        # executor's own idempotency check, which is exactly what this
        # pass is measuring.
        #
        # "Actually reached" matters: a stopping rule (src/gate/stopping.py)
        # can halt the first pass before it processes the full slice —
        # this was assumed impossible when the second pass unconditionally
        # iterated all of episode_slice, but a real, unplanned run of
        # genuine 429s (beyond the four deliberately-injected faults) hit
        # exactly that: consecutive_executor_errors_stop=3 fired after 8
        # episodes, leaving the other 100 never attempted at all. Re-
        # submitting one of THOSE through create_recovery_link() is not an
        # idempotency check — it is a fresh first-ever attempt, with
        # nothing on file yet to detect a duplicate against — and if it
        # also failed for real, ExecutorError propagated straight out of
        # this unguarded loop and crashed the whole script. Bounding the
        # second pass to the same prefix the first pass actually covered
        # keeps every second-pass call a genuine re-submission, matching
        # this pass's own documented purpose, and makes it impossible for
        # this loop to attempt an episode the first pass never touched.
        processed_count = len(episode_slice) - summary.by_outcome.get("pending", 0)
        idempotency_hits = 0
        for ep in episode_slice[:processed_count]:
            second_result = inner_executor.create_recovery_link(
                episode=ep, action=PLACEHOLDER_ACTION,
                policy_rule_id=PLACEHOLDER_POLICY_RULE, run_id=run_id,
            )
            if second_result.status == "duplicate_suppressed" and not second_result.created_new:
                idempotency_hits += 1

        cap_breach_rows = conn.execute(
            "SELECT COUNT(*) AS n FROM exception_entry WHERE run_id = ? AND reason_code = ?",
            (run_id, ReasonCode.EXPOSURE_CEILING_EXCEEDED),
        ).fetchone()["n"]
        quiet_hour_rows = conn.execute(
            "SELECT COUNT(*) AS n FROM exception_entry WHERE run_id = ? AND reason_code = ?",
            (run_id, ReasonCode.QUIET_HOURS_BLOCK),
        ).fetchone()["n"]

        created_rows = conn.execute(
            "SELECT plink_id, idempotency_key FROM execution "
            "WHERE run_id = ? AND status = 'created'",
            (run_id,),
        ).fetchall()
        created_plink_ids = [r["plink_id"] for r in created_rows if r["plink_id"]]
        distinct_keys = len({r["idempotency_key"] for r in created_rows})

        if dry_run_first:
            duplicate_links_created = 0
            verification_note = "not verified against the real API (--dry-run-first)"
        else:
            assert client is not None
            real_links: list[dict] = []
            skip = 0
            while True:
                page = client.list_payment_links(count=100, skip=skip)
                if not page:
                    break
                real_links.extend(page)
                skip += len(page)
                if len(page) < 100 or skip > 2000:
                    break
            run_links = [
                item for item in real_links
                if (item.get("notes") or {}).get("run_id") == run_id
            ]
            duplicate_links_created = max(0, len(run_links) - distinct_keys)
            verification_note = (
                f"{len(run_links)} link(s) on the real Razorpay API carry "
                f"notes.run_id={run_id!r}, vs {distinct_keys} distinct idempotency "
                "key(s) recorded locally"
            )

        result = {
            "run_id": run_id,
            "n": n,
            "processed_count": processed_count,
            "stopped_reason": summary.stopped_reason,
            "consecutive_error_tolerance": consecutive_error_tolerance,
            "mode": "dry_run_first" if dry_run_first else "live",
            "duplicate_links_created": duplicate_links_created,
            "cap_breaches": cap_breach_rows,
            "quiet_hour_contacts": quiet_hour_rows,
            "idempotency_detected": idempotency_hits,
            "idempotency_total": processed_count,
            "verification_note": verification_note,
            "fault_plan": asdict(plan),
        }
    finally:
        audit.close()
        if not dry_run_first and created_plink_ids and inner_executor is not None:
            print(f"cancelling {len(created_plink_ids)} link(s) created this run...")
            for plink_id in created_plink_ids:
                try:
                    inner_executor.cancel_link(plink_id)
                    cancelled_count += 1
                except Exception:
                    logger.exception("failed to cancel link %s", plink_id)
        if client is not None:
            client.close()
        conn.close()

    result["cancelled_count"] = cancelled_count
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print()
    if result["stopped_reason"]:
        print(
            f"NOTE: stopping rule '{result['stopped_reason']}' fired — only "
            f"{result['processed_count']}/{n} requested episode(s) were actually "
            "reached this run."
        )
    print(f"duplicate links created: {result['duplicate_links_created']}  (must be 0)")
    print(f"cap breaches: {result['cap_breaches']}  quiet-hour contacts: "
          f"{result['quiet_hour_contacts']}  (both must be 0)")
    print(f"idempotency collisions correctly detected: "
          f"{result['idempotency_detected']}/{result['idempotency_total']}")
    print(f"links cancelled: {cancelled_count}")
    print(f"written: {OUTPUT_PATH.relative_to(ROOT)}")


if __name__ == "__main__":
    typer.run(main)
