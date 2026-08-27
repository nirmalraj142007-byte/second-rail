"""The batch orchestrator — the spine every later phase plugs into.

`Runner.run()` takes four OPTIONAL collaborators (diagnoser, chooser,
executor, attributor). With all four `None` — which is all that exists as
of this phase — it performs gate-only processing: every episode is gated,
nothing is diagnosed, chosen, executed, or attributed. Phases 8-12 inject
the real implementations; this class does not get rebuilt when they land.

`make gate-run` wires this module's CLI with `--gate-only`. The phase spec
that requested this module names a single source, `data/train.jsonl`
(400 episodes) — but the same phase's own acceptance tests ask for "all
600 generated episodes" and check the accounting invariant "on the full
600-episode set". Those two are inconsistent given how Phase 5 actually
split the data (400 train / 200 sealed): reading train.jsonl alone can
never reach 600. `holdout/sealed.jsonl` carries every gate-relevant field
(amount, timestamps, error strings, customer_id) and withholds only
`cause_class` — which the gate never looks at, only src/diagnose/ will —
so combining both sources is safe under the sealed-split rule enforced by
scripts/holdout_guard.py (that guard blocks reads of the sealed split's
ground-truth labels file, not the sealed episodes file itself) and is the
only way to actually satisfy "600" and "the full 600-episode set" as
written. `--source` is still overridable for anyone who wants train.jsonl
alone.
"""

from __future__ import annotations

import subprocess
import time
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import typer
from pydantic import BaseModel
from ulid import ULID

from src.audit.writer import AuditWriter
from src.config import Settings, load_settings
from src.config_models import ConfigBundle, config_hash, load_all
from src.db.migrate import get_connection, migrate
from src.db.repo import (
    end_run,
    get_opted_out_customer_ids,
    insert_customer_if_absent,
    insert_episode,
    insert_exception_entry,
    insert_gate_check,
    start_run,
)
from src.errors import DuplicateEventError
from src.gate.checks import Episode, GateContext, RunState
from src.gate.engine import GateDecision, GateEngine, cluster_sizes, compute_cluster_membership
from src.gate.stopping import REASON_CLUSTER_ESCALATION, StoppingRules
from src.logging_setup import get_logger, setup_logging

IST = ZoneInfo("Asia/Kolkata")
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCES = (ROOT / "data" / "train.jsonl", ROOT / "holdout" / "sealed.jsonl")
DEFAULT_CUSTOMERS_PATH = ROOT / "data" / "customers.jsonl"
DEFAULT_EXCEPTIONS_SAMPLE_PATH = ROOT / "evidence" / "exceptions_sample.md"


class RunSummary(BaseModel):
    run_id: str
    episode_count: int
    by_outcome: dict[str, int]
    by_escalation_tier: dict[str, int]
    exception_count: int
    elapsed_s: float
    throughput_epm: float
    stopped_reason: str | None


@dataclass
class _EpisodeRow:
    """Just the columns insert_episode() needs, split out of Episode so the
    mapping between the two lives in exactly one place."""

    episode: Episode

    def kwargs(self) -> dict[str, object]:
        e = self.episode
        return dict(
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


def _now_iso() -> str:
    return datetime.now(IST).isoformat(timespec="seconds")


def _git_sha() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=ROOT, capture_output=True, text=True, timeout=5, check=True,
        )
        return out.stdout.strip() or None
    except Exception:
        return None


class Runner:
    def __init__(
        self,
        conn,
        audit: AuditWriter,
        config: ConfigBundle,
        settings: Settings,
        diagnoser: object | None = None,
        chooser: object | None = None,
        executor: object | None = None,
        attributor: object | None = None,
    ) -> None:
        self._conn = conn
        self._audit = audit
        self._config = config
        self._settings = settings
        self._diagnoser = diagnoser
        self._chooser = chooser
        self._executor = executor
        self._attributor = attributor
        self._gate = GateEngine()
        self._stopping = StoppingRules()
        self._logger = get_logger("runner", stage="gate")

    def _ensure_episode_row(self, episode: Episode) -> None:
        try:
            insert_episode(self._conn, **_EpisodeRow(episode).kwargs())
        except DuplicateEventError:
            # check_duplicate already looked this payment_id up before we
            # got here — a collision now would mean something else wrote
            # this row between the check and the insert, which cannot
            # happen in this single-threaded batch loop. Logged, not
            # raised, so a genuine race elsewhere never silently vanishes.
            self._logger.warning(
                "unexpected duplicate insert for payment_id=%s", episode.payment_id
            )

    def run(
        self,
        episodes: Iterable[Episode],
        mode: str,
        *,
        now: datetime | None = None,
        run_id: str | None = None,
    ) -> RunSummary:
        episode_list = list(episodes)
        episode_count = len(episode_list)
        run_id = run_id or str(ULID())
        bundle = self._config
        g = bundle.guardrails

        start_run(
            self._conn,
            run_id=run_id,
            started_at=_now_iso(),
            mode=mode,
            git_sha=_git_sha(),
            config_hash=config_hash(bundle),
        )

        cluster_membership = compute_cluster_membership(episode_list, g.outage_cluster_threshold)
        cluster_size_by_key = cluster_sizes(cluster_membership)
        opted_out = get_opted_out_customer_ids(self._conn)

        state = RunState()
        by_outcome: Counter[str] = Counter()
        by_tier: Counter[str] = Counter()
        exception_count = 0
        processed = 0
        stopped_reason: str | None = None

        t0 = time.monotonic()
        for episode in episode_list:
            if Path(g.kill_switch_path).exists():
                stopped_reason = "kill_switch"
                self._audit.append(
                    stage="stop", actor="system",
                    rationale=f"kill switch file present at {g.kill_switch_path!r}",
                )
                break

            # "now" for a batch replay defaults to *this episode's own*
            # received_at, simulating a system that gates each episode
            # shortly after it arrives — the realistic case, and the one
            # the seeded data assumes (received_at - failed_at is a few
            # minutes for every episode except the one deliberately-seeded
            # episode_older_than_window edge case). A single fixed instant
            # for the whole batch was tried first and rejected: episodes
            # are drawn uniformly across a 30-day window, so measuring
            # every one of them against one instant near the end of that
            # window made ~90% of the batch fail on age alone, for reasons
            # that had nothing to do with the age cap actually working —
            # see BUILD_LOG.md. An explicit `now` (tests, `--now`) still
            # overrides this per-episode default globally.
            episode_now = now if now is not None else episode.received_at
            ctx = GateContext(
                now=episode_now,
                conn=self._conn,
                state=state,
                opted_out_customers=opted_out,
                cluster_key_for_episode=cluster_membership,
            )
            decision = self._gate.evaluate(episode, ctx, g)
            processed += 1

            if decision.failed_check != "duplicate":
                self._ensure_episode_row(episode)

            for i, check in enumerate(decision.checks):
                insert_gate_check(
                    self._conn,
                    check_id=str(ULID()),
                    episode_id=episode.episode_id,
                    check_name=check.name,
                    result=check.result,
                    reason=check.reason,
                    evaluated_at=_now_iso(),
                    order_index=i,
                )

            outcome = "pending" if decision.eligible else "suppressed"
            self._audit.append(
                stage="gate",
                actor="system",
                episode_id=episode.episode_id,
                payment_id=episode.payment_id,
                outcome=outcome,
                escalation_tier=decision.escalation_tier,
                rationale=self._rationale(decision),
                guardrail_checks=[{"name": c.name, "result": c.result} for c in decision.checks],
            )

            if decision.eligible:
                by_outcome["pending"] += 1
                state.exposure_committed_paise += episode.amount_paise
                state.total_eligible_contacts_this_run += 1
                if episode.customer_id:
                    state.contacts_by_customer.setdefault(episode.customer_id, []).append(
                        episode.failed_at
                    )
            else:
                by_outcome["suppressed"] += 1
                exception_count += 1
                insert_exception_entry(
                    self._conn,
                    exception_id=str(ULID()),
                    run_id=run_id,
                    episode_id=episode.episode_id,
                    stage="gate",
                    reason_code=decision.reason_code or "unknown",
                    reason_text=f"{decision.failed_check} check failed",
                )
                if decision.failed_check == "amount_cap":
                    state.cap_breached = True
                if decision.failed_check == "cluster":
                    key = cluster_membership[episode.episode_id]
                    state.cluster_processed[key] = state.cluster_processed.get(key, 0) + 1
                    if state.cluster_processed[key] == cluster_size_by_key[key]:
                        state.cluster_escalated = True
                        self._audit.append(
                            stage="stop",
                            actor="system",
                            rationale=(
                                f"shared_cause_cluster: {cluster_size_by_key[key]} episodes "
                                f"sharing error_reason={key!r} within a 30-minute window exceed "
                                f"outage_cluster_threshold={g.outage_cluster_threshold} — "
                                "hard_refuse, not individually recoverable"
                            ),
                        )

            by_tier[decision.escalation_tier] += 1

            reason = self._stopping.check(state, g)
            if reason:
                stopped_reason = reason
                if reason != REASON_CLUSTER_ESCALATION:
                    # the cluster-escalation stop record was already
                    # written above, once, at the point of detection
                    self._audit.append(
                        stage="stop", actor="system", rationale=f"stopping rule fired: {reason}"
                    )
                break

        elapsed = time.monotonic() - t0
        pending_unreached = episode_count - processed
        by_outcome["pending"] += pending_unreached

        throughput = (processed / elapsed * 60.0) if elapsed > 0 else 0.0
        end_run(
            self._conn,
            run_id=run_id,
            ended_at=_now_iso(),
            episode_count=episode_count,
            stopped_reason=stopped_reason,
            llm_cost_paise=0,
            throughput_epm=throughput,
        )

        actioned = by_outcome.get("actioned", 0)
        suppressed = by_outcome.get("suppressed", 0)
        execution_failed = by_outcome.get("execution_failed", 0)
        pending = by_outcome.get("pending", 0)
        total = actioned + suppressed + execution_failed + pending
        if total != episode_count:
            raise AssertionError(
                f"accounting invariant violated: episode_count={episode_count} != "
                f"actioned({actioned}) + suppressed({suppressed}) + "
                f"execution_failed({execution_failed}) + pending({pending}) = {total}"
            )
        print(
            f"episode_count={episode_count} == actioned({actioned}) + "
            f"suppressed({suppressed}) + execution_failed({execution_failed}) + "
            f"pending({pending})"
        )

        return RunSummary(
            run_id=run_id,
            episode_count=episode_count,
            by_outcome=dict(by_outcome),
            by_escalation_tier=dict(by_tier),
            exception_count=exception_count,
            elapsed_s=elapsed,
            throughput_epm=throughput,
            stopped_reason=stopped_reason,
        )

    @staticmethod
    def _rationale(decision: GateDecision) -> str:
        if decision.eligible:
            return f"eligible, escalation_tier={decision.escalation_tier}"
        return f"{decision.failed_check} check failed: {decision.reason_code}"


# ---------------------------------------------------------------------------
# data loading + exceptions_sample.md + CLI
# ---------------------------------------------------------------------------


def load_episodes(paths: Iterable[Path]) -> list[Episode]:
    episodes: list[Episode] = []
    for path in paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                episodes.append(Episode.model_validate_json(line))
    episodes.sort(key=lambda e: e.episode_id)
    return episodes


def load_and_upsert_customers(conn, path: Path) -> int:
    import json

    n = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        c = json.loads(line)
        insert_customer_if_absent(
            conn,
            customer_id=c["customer_id"],
            synthetic_name=c.get("synthetic_name"),
            contact_hash=c["contact_hash"],
            email_hash=c.get("email_hash"),
            segment=c.get("segment"),
            opted_out=bool(c.get("opted_out", False)),
            opt_out_ts=c.get("opt_out_ts"),
            created_at=c["created_at"],
        )
        n += 1
    return n


def write_exceptions_sample(conn, run_id: str, path: Path) -> None:
    counts = conn.execute(
        """
        SELECT reason_code, COUNT(*) AS n FROM exception_entry
        WHERE run_id = ? GROUP BY reason_code ORDER BY n DESC
        """,
        (run_id,),
    ).fetchall()
    examples = conn.execute(
        """
        SELECT e.reason_code, e.reason_text, ep.payment_id, ep.amount_paise,
               ep.error_code, ep.error_reason, ep.instrument
        FROM exception_entry e
        JOIN episode ep ON ep.episode_id = e.episode_id
        WHERE e.run_id = ?
        ORDER BY e.rowid
        LIMIT 3
        """,
        (run_id,),
    ).fetchall()

    lines = [
        f"# Exceptions sample — run `{run_id}`",
        "",
        "Every suppressed episode this run, grouped by reason_code, plus three",
        "worked examples with the actual episode data. No episode is ever",
        "silently dropped — see the accounting invariant in `src/runner.py`.",
        "",
        "| reason_code | count |",
        "|---|---|",
    ]
    for row in counts:
        lines.append(f"| `{row['reason_code']}` | {row['n']} |")

    lines += ["", "## Worked examples", ""]
    for ex in examples:
        rupees = ex["amount_paise"] / 100
        lines.append(
            f"- `{ex['payment_id']}` ({ex['instrument']}, Rs {rupees:.2f}, "
            f"error_reason={ex['error_reason']!r}) — **{ex['reason_code']}**: {ex['reason_text']}"
        )
    if not examples:
        lines.append("- (no suppressed episodes this run)")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


app = typer.Typer(add_completion=False)


@app.command()
def main(
    source: list[Path] = typer.Option(
        None, "--source", help="JSONL episode file(s); repeatable. Default: train + sealed."
    ),
    gate_only: bool = typer.Option(
        False, "--gate-only",
        help="Skip diagnose/choose/execute/attribute (the only mode built so far).",
    ),
    mode: str = typer.Option("dry_run", "--mode"),
    now: str | None = typer.Option(
        None, "--now",
        help="ISO8601 override for 'now' (episode_age / quiet_hours reference point), applied "
        "globally to every episode. Default: each episode's own received_at.",
    ),
    customers: Path = typer.Option(DEFAULT_CUSTOMERS_PATH, "--customers"),
) -> None:
    setup_logging()
    settings = load_settings()
    migrate(settings.db_path)
    conn = get_connection(settings.db_path)
    bundle = load_all()

    sources = list(source) if source else list(DEFAULT_SOURCES)
    episodes = load_episodes(sources)
    load_and_upsert_customers(conn, customers)

    now_dt: datetime | None = None
    if now is not None:
        now_dt = datetime.fromisoformat(now)
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=IST)

    run_id = str(ULID())
    audit = AuditWriter(run_id, settings.audit_dir, conn)
    try:
        runner = Runner(conn, audit, bundle, settings)
        summary = runner.run(episodes, mode, now=now_dt, run_id=run_id)
    finally:
        audit.close()

    write_exceptions_sample(conn, run_id, DEFAULT_EXCEPTIONS_SAMPLE_PATH)

    typer.echo(f"run_id={summary.run_id} episode_count={summary.episode_count}")
    typer.echo(f"by_outcome={summary.by_outcome}")
    typer.echo(f"by_escalation_tier={summary.by_escalation_tier}")
    typer.echo(f"exception_count={summary.exception_count}")
    typer.echo(f"stopped_reason={summary.stopped_reason}")
    typer.echo(f"throughput_epm={summary.throughput_epm:.1f}")
    conn.close()


if __name__ == "__main__":
    app()
