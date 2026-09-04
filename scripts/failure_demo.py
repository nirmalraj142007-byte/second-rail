"""Primary failure demonstration — video beat 2:20-2:42.

`make failure-demo` runs a fixed 12-episode slice against the real
Razorpay test-mode API, with a 429 injected on episode 7 for 3 consecutive
attempts (matching `executor_retry_cap` in `config/guardrails.yaml`). It
prints, in order: episodes 1-6 streaming normally, episode 7's real
backoff (1s, 2s, 4s), the retry-cap-reached line, the stopping-rule check
(not triggered — only 1 consecutive failure against a threshold of 3), the
remaining episodes, the batch summary with the recovery total excluding
episode 7's amount, and finally an automatic re-run of episode 7 proving
the idempotency key blocks a second link.

Uses its own throwaway SQLite database (`evidence/failure_demo.db`,
deleted and re-migrated at the start of every run) rather than the shared
`second_rail.db` — this is what makes "DETERMINISTIC ON EPISODE INDEX ...
every take" actually true: the same 12 episodes must gate-pass identically
on take one and take three, and `check_duplicate` (src/gate/checks.py)
looks episodes up by `payment_id` in whatever database is passed in, so a
persistent shared DB would silently gate-suppress all 12 on the second
run. Audit records still land in the normal `evidence/audit/` directory —
only the bookkeeping database is throwaway, not the audit trail.

Every link this run creates is cancelled (`src/execute/rollback.py`,
reused, not duplicated) before the process exits. Resetting the local DB
is not enough on its own to make three consecutive real runs identical:
`reference_id` is derived from `payment_id` + a *fixed* placeholder policy
rule (both deterministic on purpose, for the reason above), so it is the
same string on every invocation, and Razorpay's own test-mode account
remembers a `reference_id` across runs, not just within one — a second
real run against an account that still holds the first run's links gets
HTTP 400 `BAD_REQUEST_ERROR` ("... already exists") on nearly every
episode instead of the intended 429 fault-injection flow, reproduced live
while rehearsing this script (see BUILD_LOG.md). Cancelling a link frees
its `reference_id` for reuse (confirmed against the real API, same BUILD_LOG
entry) — cancelling here, every run, is what makes three consecutive real
takes actually behave identically rather than only the first one.

The primary scenario is recorded on camera; scripts/failure_demo_backup.py
exists because a stateful fault injector interacting with a live API is
exactly the thing that works on take one and not on take three.
"""

from __future__ import annotations

from pathlib import Path

from ulid import ULID

from src.audit.writer import AuditWriter
from src.config import load_settings, require_razorpay
from src.config_models import load_all
from src.db.migrate import get_connection, migrate
from src.db.repo import get_opted_out_customer_ids
from src.execute.executor import RazorpayExecutor
from src.execute.faults import FaultInjectingExecutor, FaultPlan, recovered_amount_paise
from src.execute.idempotency import idempotency_key
from src.execute.rollback import rollback_run
from src.gate.checks import Episode
from src.logging_setup import get_logger, setup_logging
from src.razorpay_client import RazorpayClient
from src.runner import (
    Runner,
    load_and_upsert_customers,
    load_episodes,
    select_gate_eligible_slice,
)

ROOT = Path(__file__).resolve().parent.parent
DEMO_DB_PATH = ROOT / "evidence" / "failure_demo.db"
TRAIN_SOURCE = ROOT / "data" / "train.jsonl"
CUSTOMERS_PATH = ROOT / "data" / "customers.jsonl"
SLICE_SIZE = 12
FAULT_EPISODE_INDEX = 7
PLACEHOLDER_ACTION = "placeholder_action"
PLACEHOLDER_POLICY_RULE = "P-00"

logger = get_logger("failure_demo")


class _PrintingAuditWriter(AuditWriter):
    """Same hash chain as AuditWriter — every record still goes through
    `super().append()` — plus a live, camera-legible print of each
    execute-stage record as it happens, so the backoff, the exhaustion, and
    each episode's outcome scroll in real time instead of only existing in
    the JSONL file after the fact."""

    def __init__(
        self,
        run_id: str | None,
        audit_dir: Path,
        conn,
        *,
        labels: dict[str, int],
        payment_ids: dict[str, str],
        retry_cap: int,
    ) -> None:
        super().__init__(run_id, audit_dir, conn)
        self._labels = labels
        self._payment_ids = payment_ids
        self._retry_cap = retry_cap

    def append(self, *, stage: str, actor: str, episode_id: str | None = None, **fields):
        event_id = super().append(stage=stage, actor=actor, episode_id=episode_id, **fields)
        if stage == "execute" and episode_id in self._labels:
            self._print_execute_record(episode_id, fields)
        return event_id

    def _print_execute_record(self, episode_id: str, fields: dict) -> None:
        idx = self._labels[episode_id]
        pay = self._payment_ids.get(episode_id, episode_id)
        execution = fields.get("execution") or {}
        outcome = fields.get("outcome")

        if outcome is None and "attempt" in execution:
            attempt = execution["attempt"]
            delay_s = execution["delay_ms"] / 1000
            print(f"episode {idx} ({pay}): attempt {attempt} -> HTTP 429 -> backoff {delay_s:g}s")
            return

        if outcome == "execution_failed":
            print(f"retry cap {self._retry_cap} reached - not retrying further")
            print(f"episode {idx} ({pay}) -> execution_failed -> exception list "
                  f"(reason: executor_retry_exhausted)")
            return

        if outcome is not None:
            plink = execution.get("plink_id")
            print(f"episode {idx} ({pay}) -> {outcome} (plink_id={plink})")


def _reset_demo_db() -> None:
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(DEMO_DB_PATH) + suffix)
        if p.exists():
            p.unlink()


def main() -> int:
    setup_logging()
    settings = load_settings()
    bundle = load_all()
    g = bundle.guardrails

    # Fails loudly here (ConfigError) if keys are absent — no silent
    # dry-run substitution for a script whose entire point is a real 429.
    key_id, key_secret = require_razorpay(settings)

    DEMO_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    _reset_demo_db()
    migrate(DEMO_DB_PATH)
    conn = get_connection(DEMO_DB_PATH)

    all_train_episodes = load_episodes([TRAIN_SOURCE])
    load_and_upsert_customers(conn, CUSTOMERS_PATH)
    opted_out = get_opted_out_customer_ids(conn)

    episode_slice: list[Episode] = select_gate_eligible_slice(
        conn, all_train_episodes, g, opted_out, SLICE_SIZE
    )
    labels = {ep.episode_id: i + 1 for i, ep in enumerate(episode_slice)}
    payment_ids = {ep.episode_id: ep.payment_id for ep in episode_slice}

    run_id = str(ULID())
    client = RazorpayClient(key_id, key_secret)
    audit = _PrintingAuditWriter(
        run_id, settings.audit_dir, conn,
        labels=labels, payment_ids=payment_ids, retry_cap=g.executor_retry_cap,
    )

    print("=" * 72)
    print(f"FAILURE DEMO - run_id={run_id}")
    print(f"{len(episode_slice)} episodes, --execute (real Razorpay test-mode calls)")
    print(f"fault plan: inject 429 at episode {FAULT_EPISODE_INDEX}, "
          f"{g.executor_retry_cap} consecutive attempts")
    print("=" * 72)

    # `audit` stays open across BOTH the runner's batch loop and the manual
    # idempotency re-run below — inner_executor.create_recovery_link() for
    # that re-run writes through the same audit writer, and closing it
    # right after runner.run() (as this used to) crashes with "I/O
    # operation on closed file" the moment the run's OWN stopping rule
    # fires early (reproduced live: real, unrelated Razorpay-side rate
    # limiting during rehearsal pushed consecutive_executor_errors to
    # threshold at episode 3, well before the deliberately-faulted episode
    # 7 — see BUILD_LOG.md). That is a real, if less common, path through
    # this script, not just the deliberate-fault-only one.
    try:
        inner_executor = RazorpayExecutor(
            conn=conn,
            client=client,
            mode="execute",
            run_id=run_id,
            audit=audit,
            retry_cap=g.executor_retry_cap,
            retry_delays=[float(s) for s in g.executor_backoff_seconds],
        )
        plan = FaultPlan(
            inject_429_at_episode_index=FAULT_EPISODE_INDEX,
            inject_429_repeat=g.executor_retry_cap,
        )
        faulty_executor = FaultInjectingExecutor(inner_executor, plan)
        faulty_executor.start_run(len(episode_slice))

        runner = Runner(conn, audit, bundle, settings, executor=faulty_executor)
        summary = runner.run(episode_slice, mode="execute", run_id=run_id)

        exit_code = _finish_failure_demo(
            conn, client, inner_executor, run_id, episode_slice, summary, g
        )
    finally:
        audit.close()
        client.close()
        conn.close()
    return exit_code


def _finish_failure_demo(conn, client, inner_executor, run_id, episode_slice, summary, g) -> int:
    execution_failed = summary.by_outcome.get("execution_failed", 0)
    actioned = summary.by_outcome.get("actioned", 0)
    threshold = g.consecutive_executor_errors_stop
    # With a single fault index in this plan, the peak consecutive-error
    # count the run ever reaches equals the total execution_failed count —
    # there is nothing else in the 12-episode slice to make it climb higher
    # or to make it non-monotonic before resetting on the next success.
    triggered = execution_failed >= threshold
    print()
    print(f"stopping rule check: consecutive_executor_errors {execution_failed} of "
          f"{threshold} - {'TRIGGERED' if triggered else 'NOT triggered'}")

    print()
    print(f"batch complete: {actioned} actioned, {execution_failed} execution_failed")
    total_paise = recovered_amount_paise(conn, run_id)
    print(f"recovery computed over {actioned} episodes - episode {FAULT_EPISODE_INDEX} EXCLUDED "
          f"(Rs {total_paise / 100:.2f} total)")

    # Automatic re-run of the failed episode, proving the idempotency key
    # blocks a second link even for an episode whose only prior attempt
    # failed — a fresh call still finds the earlier row and refuses. Only
    # meaningful if episode 7 was actually reached this run: the stopping
    # rule (consecutive_executor_errors) can fire earlier than episode 7 —
    # not from the deliberate fault plan, but from real, unrelated
    # Razorpay-side rate limiting on episodes 1-6, reproduced live during
    # rehearsal (see BUILD_LOG.md) — and re-running an episode with no
    # prior attempt at all would create a fresh link, not prove idempotency.
    failed_episode = episode_slice[FAULT_EPISODE_INDEX - 1]
    key = idempotency_key(failed_episode.payment_id, PLACEHOLDER_POLICY_RULE)
    # The runner processes episode_slice strictly in order and writes a
    # gate_check row for every episode it reaches before stopping — this is
    # true regardless of *why* a run stopped early, so it is a more direct
    # check than re-deriving "did we get to index 7" from stopped_reason.
    episode_7_reached = (
        conn.execute(
            "SELECT 1 FROM gate_check WHERE episode_id = ? LIMIT 1", (failed_episode.episode_id,)
        ).fetchone()
        is not None
    )

    if not episode_7_reached:
        print()
        print(
            f"run stopped early (stopped_reason={summary.stopped_reason!r}) before reaching "
            f"episode {FAULT_EPISODE_INDEX} ({failed_episode.payment_id}) - skipping the "
            "idempotency re-run proof, nothing to deduplicate against yet"
        )
    else:
        links_before = conn.execute(
            "SELECT COUNT(*) AS n FROM execution WHERE run_id = ? AND status = 'created'", (run_id,)
        ).fetchone()["n"]

        print()
        print(f"re-running episode {FAULT_EPISODE_INDEX} ({failed_episode.payment_id}) "
              "to prove idempotency...")
        rerun_result = inner_executor.create_recovery_link(
            episode=failed_episode,
            action=PLACEHOLDER_ACTION,
            policy_rule_id=PLACEHOLDER_POLICY_RULE,
            run_id=run_id,
        )
        links_after = conn.execute(
            "SELECT COUNT(*) AS n FROM execution WHERE run_id = ? AND status = 'created'", (run_id,)
        ).fetchone()["n"]
        new_links = links_after - links_before

        print(f"re-run {failed_episode.payment_id} -> idempotency key {key} matches -> "
              f"{rerun_result.status} -> {new_links} new links created")

    print()
    print("cancelling every link this run created (frees their reference_id for the next take)...")
    successes, failures = rollback_run(conn, client, run_id)
    print(f"rollback: cancelled {len(successes)}/{len(successes) + len(failures)} link(s)")
    if failures:
        print(f"rollback FAILED for: {failures}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
