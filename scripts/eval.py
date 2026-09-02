"""`make eval` — the sealed-split evaluation harness. Verifies the seal,
loads the sealed split, runs the Second Rail pipeline and the
FIXED_RETRY_AT_T30 baseline over it, computes every metric, and renders
`evidence/report.md` (plus `evidence/charts/*.png` and
`evidence/eval_metrics.json`).

`holdout/labels.jsonl` is opened exactly once in this whole codebase,
right here, via `scripts.holdout_guard.open_labels()` — nowhere under
`src/` is allowed to (enforced at runtime by that guard).

FIXTURE mode (default): FixtureExecutor, no network, no Razorpay key.
Every LLM call the diagnose/choose stages need goes through the same
content-addressed disk cache production uses — if every prompt this run
produces was already cached by an earlier evidence-generation pass, this
completes with `LLM_API_KEY` unset. `make eval LIVE=1` swaps in
RazorpayExecutor for real (still test-mode) Payment Link creation.

Two design choices load-bearing enough to state here rather than bury in
a comment:

1. **Recovery is computed as an expected value**, not a resampled boolean
   outcome — Sigma(response_probability * amount_paise) over episodes this
   run actually contacted, using each sealed episode's own
   response_probability field from `holdout/labels.jsonl` (itself derived
   from `outcome_model.md`'s formula). A single boolean draw per episode
   on a 200-episode batch would make the +/-30% sensitivity sweep mostly
   measure sampling noise, not the parameter being swept; expected value
   makes the sweep a pure, reproducible function of the declared
   probabilities. See src/report/sensitivity.py's module docstring.
2. **The baseline reuses Runner's existing gate-only fallback path**
   (`diagnoser`/`policy_engine`/`selector` all omitted) rather than a
   second, hand-written pipeline: with no diagnoser wired in, every
   gate-eligible episode gets `action="placeholder_action"`,
   `policy_rule_id="P-00"`, unconditionally executed — which is exactly
   FIXED_RETRY_AT_T30's own definition ("every eligible episode gets one
   retry offer... with no diagnosis and no policy table"). The literal
   string "placeholder_action" is Runner's pre-existing sentinel, not a
   name invented for this baseline; the report translates it for a reader.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import typer
from ulid import ULID

from scripts import seal as seal_module
from scripts.holdout_guard import open_labels
from src.attribute.ledger import (
    compute_fp_cost,
    parse_outcome_assumptions,
    post_expected_gross,
    post_net,
)
from src.audit.writer import AuditWriter
from src.choose.policy import PolicyEngine
from src.choose.selector import ActionSelector
from src.config import Settings, load_settings, require_razorpay
from src.config_models import ConfigBundle, config_hash, load_all
from src.db.migrate import get_connection, migrate
from src.diagnose.baseline import RegexBaseline
from src.diagnose.cache import DiskCache
from src.diagnose.classifier import Diagnoser, Diagnosis
from src.diagnose.llm_client import build_llm_client, compute_cost_paise, load_pricing
from src.errors import AttributionError
from src.execute.executor import Executor, FixtureExecutor, RazorpayExecutor
from src.gate.checks import Episode
from src.logging_setup import get_logger, setup_logging
from src.razorpay_client import RazorpayClient
from src.report import charts as report_charts
from src.report.render import (
    ClassMetric,
    ConfusionCost,
    ExceptionRow,
    ExternallyAnchoredRow,
    GuardrailProof,
    HeadToHeadRow,
    RecoveryFigure,
    ReportData,
    RunMeta,
    Section1,
    Section2,
    Section3,
    Section4,
    Section5,
    Section6,
    Section7,
    WorkedException,
    render_report,
)
from src.report.sensitivity import (
    METHODOLOGY_NOTE,
    PARAM_REASONING,
    SWEPT_PARAMS,
    WINDOW_NOTE,
    ContactedEpisode,
    SweepInputs,
    sweep_recovery,
)
from src.runner import Runner, RunSummary, load_and_upsert_customers, load_episodes

IST = ZoneInfo("Asia/Kolkata")
ROOT = Path(__file__).resolve().parent.parent
SEALED_PATH = ROOT / "holdout" / "sealed.jsonl"
SHIFT_PATH = ROOT / "holdout" / "SHIFT.md"
CUSTOMERS_PATH = ROOT / "data" / "customers.jsonl"
FIXTURE_DIR = ROOT / "fixtures" / "payment_links"
SR_DB_PATH = ROOT / "evidence" / "eval_second_rail.db"
BASELINE_DB_PATH = ROOT / "evidence" / "eval_baseline.db"
REPORT_PATH = ROOT / "evidence" / "report.md"
METRICS_PATH = ROOT / "evidence" / "eval_metrics.json"
GUARDRAIL_PROOF_PATH = ROOT / "evidence" / "guardrail_proof.json"
CLASSIFICATION_METRICS_PATH = ROOT / "evidence" / "classification_metrics.json"
HARVEST_PATH = ROOT / "evidence" / "harvested_errors.jsonl"

logger = get_logger("eval")


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


def _reset_db(path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(path) + suffix)
        if p.exists():
            p.unlink()


# ---------------------------------------------------------------------------
# recording wrappers — capture every Diagnosis/Selection Runner produces
# without duplicating a single LLM call (Runner still owns the one real
# diagnose()/select() call per episode; these just observe it)
# ---------------------------------------------------------------------------


@dataclass
class DiagnoseRecord:
    episode_id: str
    diagnosis: Diagnosis


class _RecordingDiagnoser:
    def __init__(self, inner: Diagnoser) -> None:
        self._inner = inner
        self.records: list[DiagnoseRecord] = []

    def diagnose(self, ep: Episode) -> Diagnosis:
        d = self._inner.diagnose(ep)
        self.records.append(DiagnoseRecord(ep.episode_id, d))
        return d


@dataclass
class SelectRecord:
    episode_id: str
    chosen_action: str
    cost_paise: int
    cache_hit: bool
    prompt_version: str | None
    llm_model: str | None
    input_tokens: int
    output_tokens: int


class _RecordingSelector:
    def __init__(self, inner: ActionSelector) -> None:
        self._inner = inner
        self.records: list[SelectRecord] = []

    def select(self, ep: Episode, diagnosis, match, *, ctx=None):
        s = self._inner.select(ep, diagnosis, match, ctx=ctx)
        self.records.append(
            SelectRecord(
                ep.episode_id, s.chosen_action, s.cost_paise, s.cache_hit, s.prompt_version,
                s.llm_model, s.input_tokens, s.output_tokens,
            )
        )
        return s


# ---------------------------------------------------------------------------
# pipeline runs
# ---------------------------------------------------------------------------


def _build_executor(
    live: bool, settings: Settings, conn, run_id: str, audit: AuditWriter, guardrails
) -> tuple[Executor, RazorpayClient | None]:
    if live:
        key_id, key_secret = require_razorpay(settings)
        client = RazorpayClient(key_id, key_secret)
        executor: Executor = RazorpayExecutor(
            conn=conn, client=client, mode="execute", run_id=run_id, audit=audit,
            retry_cap=guardrails.executor_retry_cap,
            retry_delays=[float(s) for s in guardrails.executor_backoff_seconds],
        )
        return executor, client
    return FixtureExecutor(fixture_dir=FIXTURE_DIR, conn=conn), None


def run_second_rail(
    episodes: list[Episode], settings: Settings, bundle: ConfigBundle, live: bool
) -> tuple[RunSummary, str, Any, _RecordingDiagnoser, _RecordingSelector]:
    _reset_db(SR_DB_PATH)
    migrate(SR_DB_PATH)
    conn = get_connection(SR_DB_PATH)
    load_and_upsert_customers(conn, CUSTOMERS_PATH)

    taxonomy = bundle.taxonomy
    baseline = RegexBaseline(taxonomy)
    cache = DiskCache(settings.cache_dir)
    llm = build_llm_client(settings)
    diagnoser = _RecordingDiagnoser(Diagnoser(baseline, llm, cache, taxonomy, settings))
    policy_engine = PolicyEngine(bundle.policy)
    selector = _RecordingSelector(ActionSelector(llm, cache, settings))

    run_id = str(ULID())
    audit = AuditWriter(run_id, settings.audit_dir, conn)
    executor, client = _build_executor(live, settings, conn, run_id, audit, bundle.guardrails)
    try:
        runner = Runner(
            conn, audit, bundle, settings,
            diagnoser=diagnoser, policy_engine=policy_engine, selector=selector, executor=executor,
        )
        summary = runner.run(episodes, "execute" if live else "fixture", run_id=run_id)
    finally:
        audit.close()
        if client is not None:
            client.close()
    return summary, run_id, conn, diagnoser, selector


def run_baseline(
    episodes: list[Episode], settings: Settings, bundle: ConfigBundle, live: bool
) -> tuple[RunSummary, str, Any]:
    _reset_db(BASELINE_DB_PATH)
    migrate(BASELINE_DB_PATH)
    conn = get_connection(BASELINE_DB_PATH)
    load_and_upsert_customers(conn, CUSTOMERS_PATH)

    run_id = str(ULID())
    audit = AuditWriter(run_id, settings.audit_dir, conn)
    executor, client = _build_executor(live, settings, conn, run_id, audit, bundle.guardrails)
    try:
        # No diagnoser/policy_engine/selector: Runner's pre-Phase-11
        # gate-only fallback fires for every eligible episode — see this
        # module's docstring for why that IS the FIXED_RETRY_AT_T30
        # baseline, not a stand-in for it.
        runner = Runner(conn, audit, bundle, settings, executor=executor)
        summary = runner.run(episodes, "execute" if live else "fixture", run_id=run_id)
    finally:
        audit.close()
        if client is not None:
            client.close()
    return summary, run_id, conn


# ---------------------------------------------------------------------------
# recovery figure — expected value over contacted episodes, using labels
# ---------------------------------------------------------------------------


def _contacted_episodes(conn, run_id: str) -> list[Any]:
    return conn.execute(
        "SELECT ex.episode_id, ep.amount_paise FROM execution ex "
        "JOIN episode ep ON ep.episode_id = ex.episode_id "
        "WHERE ex.run_id = ? AND ex.status = 'created'",
        (run_id,),
    ).fetchall()


def build_recovery_figure(
    *,
    label: str,
    conn,
    run_id: str,
    labels: dict[str, dict],
    gate_eligible_count: int,
    assumptions,
    batch_size: int,
    stopped_reason: str | None,
) -> tuple[RecoveryFigure, list[ContactedEpisode]]:
    rows = _contacted_episodes(conn, run_id)
    contacted = tuple(
        ContactedEpisode(
            episode_id=r["episode_id"],
            amount_paise=r["amount_paise"],
            response_probability=labels[r["episode_id"]]["response_probability"],
        )
        for r in rows
        if r["episode_id"] in labels
    )
    fp = compute_fp_cost(conn, run_id, assumptions)
    sweep = sweep_recovery(
        SweepInputs(
            contacted=contacted,
            fp_count=fp.fp_count,
            sms_cost_paise=assumptions.sms_cost_paise,
            goodwill_cost_paise=assumptions.goodwill_cost_paise,
        )
    )
    # The ledger, not the sweep, is the system of record for net. Post the
    # base case through the same post_net() every live run uses, then assert
    # the sweep agrees with it — if these two ever diverge, the report is
    # rendering a number the ledger cannot account for, which is exactly the
    # drift ledger.py's module docstring promises cannot happen.
    post_expected_gross(
        conn,
        run_id,
        amount_paise=sweep.gross_base_paise,
        basis=(
            "expected value, NOT realised recovery: "
            f"sum(response_probability x amount_paise) over the {len(contacted)} "
            "episode(s) this run contacted, using outcome_model.md's pre-registered "
            "per-episode response_probability. No customer paid a synthetic link; "
            "see evidence/report.md section 4."
        ),
    )
    net_paise = post_net(conn, run_id)
    if net_paise != sweep.net_base_paise:
        raise AttributionError(
            f"ledger net ({net_paise}p) disagrees with the sensitivity sweep's base-case "
            f"net ({sweep.net_base_paise}p) for run {run_id}",
            code="ATTRIBUTION_FAILURE",
            remediation=(
                "net must be computed only by post_net(); if the sweep now derives it "
                "differently, reconcile src/report/sensitivity.py with "
                "src/attribute/ledger.py rather than reporting either number"
            ),
        )

    figure = RecoveryFigure(
        label=label,
        gross_low_paise=sweep.gross_low_paise,
        gross_base_paise=sweep.gross_base_paise,
        gross_high_paise=sweep.gross_high_paise,
        fp_low_paise=sweep.fp_low_paise,
        fp_base_paise=sweep.fp_base_paise,
        fp_high_paise=sweep.fp_high_paise,
        net_low_paise=sweep.net_low_paise,
        net_base_paise=sweep.net_base_paise,
        net_high_paise=sweep.net_high_paise,
        contacted_count=len(contacted),
        gate_eligible_count=gate_eligible_count,
        fp_count=fp.fp_count,
        batch_size=batch_size,
        stopped_reason=stopped_reason,
    )
    return figure, list(contacted)


# ---------------------------------------------------------------------------
# section 6 — classifier detail (full 200 sealed episodes, gate-independent)
# ---------------------------------------------------------------------------


def _fake_diagnosis(episode_id: str, class_id: str) -> Diagnosis:
    return Diagnosis(
        episode_id=episode_id, method="synthetic", class_id=class_id, confidence=1.0,
        rationale="", llm_model=None, prompt_hash=None, prompt_version=None,
        cache_hit=False, latency_ms=0, cost_paise=0, input_tokens=0, output_tokens=0,
        llm_degraded=False, features_used=[],
    )


def _precision_recall_f1(pairs: list[tuple[str, str]], class_ids: list[str]) -> list[ClassMetric]:
    from collections import Counter

    tp: Counter[str] = Counter()
    fp: Counter[str] = Counter()
    fn: Counter[str] = Counter()
    support: Counter[str] = Counter()
    for pred, true in pairs:
        support[true] += 1
        if pred == true:
            tp[true] += 1
        else:
            fp[pred] += 1
            fn[true] += 1
    out = []
    for c in class_ids:
        if support[c] == 0:
            continue
        p = tp[c] / (tp[c] + fp[c]) if (tp[c] + fp[c]) else 0.0
        r = tp[c] / (tp[c] + fn[c]) if (tp[c] + fn[c]) else 0.0
        f1 = 2 * p * r / (p + r) if (p + r) else 0.0
        out.append(
            ClassMetric(
                class_id=c, precision=round(p, 4), recall=round(r, 4),
                f1=round(f1, 4), support=support[c],
            )
        )
    return out


def build_classification_section(
    bundle: ConfigBundle,
    settings: Settings,
    sealed_episodes: list[Episode],
    labels: dict[str, dict],
) -> Section6:
    taxonomy = bundle.taxonomy
    baseline = RegexBaseline(taxonomy)
    cache = DiskCache(settings.cache_dir)
    llm = build_llm_client(settings)
    diagnoser = Diagnoser(baseline, llm, cache, taxonomy, settings)
    policy_engine = PolicyEngine(bundle.policy)
    class_ids = taxonomy.class_ids()

    pairs: list[tuple[str, str]] = []
    confusions: list[tuple[str, str, Episode, dict]] = []
    for ep in sealed_episodes:
        lab = labels.get(ep.episode_id)
        if lab is None:
            continue
        d = diagnoser.diagnose(ep)
        true_class = lab["cause_class"]
        pairs.append((d.class_id, true_class))
        if d.class_id != true_class:
            confusions.append((true_class, d.class_id, ep, lab))

    n = len(pairs)
    accuracy = sum(1 for p, t in pairs if p == t) / n if n else 0.0
    per_class = _precision_recall_f1(pairs, class_ids)

    matrix: dict[str, dict[str, int]] = {t: {p: 0 for p in class_ids} for t in class_ids}
    for pred, true in pairs:
        if true in matrix:
            matrix[true][pred] = matrix[true].get(pred, 0) + 1

    def _first_admissible(match) -> str:
        for a in match.fallback_priority:
            if a in match.admissible_actions:
                return a
        return "no_action"

    by_cell: dict[tuple[str, str], list[float]] = {}
    for true_class, pred_class, ep, lab in confusions:
        true_match = policy_engine.resolve(ep, _fake_diagnosis(ep.episode_id, true_class))
        pred_match = policy_engine.resolve(ep, _fake_diagnosis(ep.episode_id, pred_class))
        true_action = _first_admissible(true_match)
        pred_action = _first_admissible(pred_match)
        p = lab["response_probability"]
        rupees = ep.amount_paise / 100
        true_expected = 0.0 if true_action == "no_action" else p * rupees
        pred_expected = 0.0 if pred_action == "no_action" else p * rupees
        by_cell.setdefault((true_class, pred_class), []).append(true_expected - pred_expected)

    confusion_costs = [
        ConfusionCost(
            true_class=t, pred_class=p, count=len(deltas),
            mean_delta_rupees=sum(deltas) / len(deltas),
        )
        for (t, p), deltas in sorted(by_cell.items(), key=lambda kv: -len(kv[1]))
    ]

    return Section6(
        n_episodes=n,
        accuracy=round(accuracy, 4),
        per_class=per_class,
        class_ids=class_ids,
        confusion_matrix=matrix,
        confusion_costs=confusion_costs,
        confusion_cost_methodology=(
            "for each confusion, the deterministic fallback_priority-first admissible action "
            "under the true class vs. the predicted class, valued at response_probability x "
            "amount for the episodes actually confused this way — not a live LLM re-selection, "
            "disclosed as a simplification"
        ),
    )


# ---------------------------------------------------------------------------
# section 5 — exceptions
# ---------------------------------------------------------------------------


def build_exceptions_section(conn, run_id: str, execution_failed_count: int) -> Section5:
    counts = conn.execute(
        "SELECT reason_code, COUNT(*) AS n FROM exception_entry WHERE run_id = ? "
        "GROUP BY reason_code ORDER BY n DESC",
        (run_id,),
    ).fetchall()
    examples = conn.execute(
        "SELECT e.reason_code, e.reason_text, ep.payment_id, ep.amount_paise, ep.error_reason, "
        "ep.instrument FROM exception_entry e JOIN episode ep ON ep.episode_id = e.episode_id "
        "WHERE e.run_id = ? ORDER BY e.rowid LIMIT 3",
        (run_id,),
    ).fetchall()
    total = conn.execute(
        "SELECT COUNT(*) AS n FROM exception_entry WHERE run_id = ?", (run_id,)
    ).fetchone()["n"]

    return Section5(
        rows=[ExceptionRow(reason_code=r["reason_code"], count=r["n"]) for r in counts],
        worked_examples=[
            WorkedException(
                payment_id=ex["payment_id"], instrument=ex["instrument"] or "(unknown)",
                amount_rupees=ex["amount_paise"] / 100, error_reason=ex["error_reason"],
                reason_code=ex["reason_code"], reason_text=ex["reason_text"],
            )
            for ex in examples
        ],
        execution_failed_count=execution_failed_count,
        total_exceptions=total,
    )


# ---------------------------------------------------------------------------
# section 7 — static disclosure
# ---------------------------------------------------------------------------


def build_section7() -> Section7:
    return Section7(
        items=[
            "Real customer behaviour — every response is a simulated draw from "
            "outcome_model.md's formula, not an actual person deciding whether to pay.",
            "Generalisation beyond the seeded distribution shift — BANK_E and the 11 "
            "reserved harvested error strings are the only shift this split carries; a real "
            "issuer's traffic could differ in ways this generator never modelled.",
            "Anything at production volume — 200 episodes in one batch, not a sustained "
            "10k/episode-a-day load; see LIMITATIONS.md for the three places this design "
            "is known to break first.",
            "Partial payments — a customer paying less than the link amount is recorded "
            "not_recovered by AR-01, even though the merchant did receive some money.",
            "Recoveries through channels this run did not create — a payment that later "
            "shows as paid through a different link or a different channel entirely is "
            "never claimed as this system's recovery.",
        ]
    )


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main(
    live: bool = typer.Option(
        False, "--live", help="Use RazorpayExecutor for real test-mode calls."
    ),
) -> None:
    setup_logging()
    t_start = time.monotonic()

    print("=== Second Rail eval: verifying the sealed split's seal ===")
    seal_ok = seal_module.verify()
    if seal_ok != 0:
        print(
            "ABORT: seal verification failed — a broken seal invalidates everything downstream.",
            file=sys.stderr,
        )
        raise typer.Exit(code=1)

    settings = load_settings()
    bundle = load_all()
    g = bundle.guardrails

    sealed_episodes = load_episodes([SEALED_PATH])
    print(f"loaded {len(sealed_episodes)} sealed episodes (holdout/labels.jsonl not opened yet)")

    print(f"=== running Second Rail pipeline ({'LIVE' if live else 'fixture, no network'}) ===")
    sr_summary, sr_run_id, sr_conn, sr_diagnoser, sr_selector = run_second_rail(
        sealed_episodes, settings, bundle, live
    )
    print(f"Second Rail: {sr_summary.by_outcome}, admissibility={sr_summary.admissibility_rate}")

    exec_mode = "LIVE" if live else "fixture, no network"
    print(f"=== running FIXED_RETRY_AT_T30 baseline ({exec_mode}) ===")
    baseline_summary, baseline_run_id, baseline_conn = run_baseline(
        sealed_episodes, settings, bundle, live
    )
    print(f"baseline: {baseline_summary.by_outcome}")

    # holdout/labels.jsonl opened exactly once in this codebase, here.
    labels = {row["episode_id"]: row for row in open_labels()}

    assumptions = parse_outcome_assumptions()

    sr_gate_eligible = _gate_eligible_from_db(sr_conn, sr_run_id, sr_summary)
    baseline_gate_eligible = _gate_eligible_from_db(
        baseline_conn, baseline_run_id, baseline_summary
    )

    sr_figure, sr_contacted = build_recovery_figure(
        label="Second Rail", conn=sr_conn, run_id=sr_run_id, labels=labels,
        gate_eligible_count=sr_gate_eligible, assumptions=assumptions,
        batch_size=sr_summary.episode_count, stopped_reason=sr_summary.stopped_reason,
    )
    baseline_figure, _ = build_recovery_figure(
        label="FIXED_RETRY_AT_T30 baseline (Runner's gate-only fallback: every gate-eligible "
        "episode gets `placeholder_action`/`P-00`, unconditionally)",
        conn=baseline_conn, run_id=baseline_run_id, labels=labels,
        gate_eligible_count=baseline_gate_eligible, assumptions=assumptions,
        batch_size=baseline_summary.episode_count, stopped_reason=baseline_summary.stopped_reason,
    )

    print("=== section 6: classifier detail over all 200 sealed episodes ===")
    section6 = build_classification_section(bundle, settings, sealed_episodes, labels)

    print("=== sections 2-3: reading evidence/classification_metrics.json ===")
    # Both sections reuse `make classify`'s already-computed, already-cached
    # numbers rather than re-deriving them: section 2's externally-anchored
    # rows and section 3's regex-vs-LLM head-to-head are general findings
    # about the classifier, not claims scoped to this specific batch, so
    # recomputing them here would only spend a second round of LLM calls on
    # data `make classify` already evaluated. If that file is missing, this
    # aborts loudly rather than fabricating either section.
    if not CLASSIFICATION_METRICS_PATH.exists():
        print(
            f"ABORT: {CLASSIFICATION_METRICS_PATH.relative_to(ROOT)} not found — "
            "run `make classify SPLIT=train` first.",
            file=sys.stderr,
        )
        raise typer.Exit(code=1)
    classify_metrics = json.loads(CLASSIFICATION_METRICS_PATH.read_text(encoding="utf-8"))
    harvested_row, doc_row = classify_metrics["externally_anchored"]
    head_to_head = classify_metrics["head_to_head"]
    classify_split = classify_metrics["split"]

    harvest_lines = HARVEST_PATH.read_text(encoding="utf-8").splitlines()
    harvest_records = [json.loads(line) for line in harvest_lines if line.strip()]
    captured_dates = sorted(
        {r["captured_at"][:10] for r in harvest_records if r.get("captured_at")}
    )
    harvest_note = (
        f"captured {captured_dates[0]}" if len(captured_dates) == 1
        else f"captured {captured_dates[0]} to {captured_dates[-1]}" if captured_dates
        else "capture date unknown"
    )

    section2 = Section2(
        harvested_error_count=len(harvest_records),
        harvest_capture_note=harvest_note,
        doc_source_note="evidence/razorpay_error_codes_snapshot.md",
        rows=[
            ExternallyAnchoredRow(
                label=harvested_row["label"], n_episodes=harvested_row["n_episodes"],
                regex_accuracy=harvested_row["regex_accuracy"],
                llm_accuracy=harvested_row["llm_accuracy"],
                llm_skipped_reason=harvested_row.get("llm_skipped_reason"),
            ),
            ExternallyAnchoredRow(
                label=doc_row["label"], n_episodes=doc_row["n_episodes"],
                regex_accuracy=doc_row["regex_accuracy"], llm_accuracy=doc_row["llm_accuracy"],
                llm_skipped_reason=doc_row.get("llm_skipped_reason"),
            ),
        ],
    )

    section3_rows = [
        HeadToHeadRow(
            family=row["family"], volume=row["volume"], regex_accuracy=row["regex_accuracy"],
            llm_accuracy=row["llm_accuracy"],
            llm_sample_note=(
                f"n={row['llm_sample_size']}"
                if row["llm_sample_size"] not in (None, row["volume"])
                else ""
            ),
        )
        for row in head_to_head["top_families"]
    ]
    # The real headline for this section is whichever externally-anchored
    # source (section2's rows — never self-generated data) has the lowest
    # LLM accuracy, computed here rather than assumed to be "harvested
    # strings" by name — that happens to be true of this project's actual
    # numbers (20% vs 88.2% on the doc snapshot) but the report should stay
    # honest if a future run's numbers ever differ. Rows with
    # llm_accuracy=None (LLM not configured this run) are excluded from
    # the comparison, not treated as a 0.
    scored_rows = [r for r in section2.rows if r.llm_accuracy is not None]
    weakest = min(scored_rows, key=lambda r: r.llm_accuracy) if scored_rows else None
    section3 = Section3(
        weakest_source_label=weakest.label if weakest else "n/a",
        weakest_n=weakest.n_episodes if weakest else 0,
        weakest_llm_accuracy=weakest.llm_accuracy if weakest else 0.0,
        weakest_regex_accuracy=weakest.regex_accuracy if weakest else 0.0,
        head_to_head_summary=(
            f"{head_to_head['summary']} (computed on the {classify_split} split via "
            "`make classify` — a general classifier finding, not scoped to this sealed "
            "batch.)"
        ),
        rows=section3_rows,
    )

    print("=== section 1: guardrail proof, admissibility, throughput, cost ===")
    guardrail = None
    if GUARDRAIL_PROOF_PATH.exists():
        gp = json.loads(GUARDRAIL_PROOF_PATH.read_text(encoding="utf-8"))
        guardrail = GuardrailProof(
            n=gp["n"], mode=gp["mode"], duplicate_links_created=gp["duplicate_links_created"],
            cap_breaches=gp["cap_breaches"], quiet_hour_contacts=gp["quiet_hour_contacts"],
            idempotency_detected=gp["idempotency_detected"],
            idempotency_total=gp["idempotency_total"],
            verification_note=gp["verification_note"],
            source_path="evidence/guardrail_proof.json",
            cancelled_count=gp.get("cancelled_count", 0),
            processed_count=gp.get("processed_count"),
            stopped_reason=gp.get("stopped_reason"),
            consecutive_error_tolerance=gp.get("consecutive_error_tolerance"),
        )

    # cost_paise is deliberately 0 on a cache hit (src/diagnose/classifier.py,
    # src/choose/selector.py: "already paid for — a cache hit costs this run
    # nothing") — correct for "did THIS run spend money" but useless for
    # "what does this cost", since a fully-cached run always reports Rs 0
    # regardless of how many real calls built that cache. real_cost_paise
    # recomputes from each record's actual token counts, ignoring cache_hit
    # entirely — mirroring scripts/classify.py's _historical_cost_paise(),
    # the same distinction that script already draws between "this run" and
    # "real" cost.
    pricing = load_pricing()

    def _real_diagnose_cost(r: DiagnoseRecord) -> int:
        if r.diagnosis.llm_model is None:  # regex-resolved, no LLM call at all
            return 0
        return compute_cost_paise(
            pricing, r.diagnosis.llm_model, r.diagnosis.input_tokens, r.diagnosis.output_tokens
        )

    def _real_select_cost(r: SelectRecord) -> int:
        if r.llm_model is None:
            return 0
        return compute_cost_paise(pricing, r.llm_model, r.input_tokens, r.output_tokens)

    diagnose_cost = sum(r.diagnosis.cost_paise for r in sr_diagnoser.records)
    select_cost = sum(r.cost_paise for r in sr_selector.records)
    diagnose_cost_real = sum(_real_diagnose_cost(r) for r in sr_diagnoser.records)
    select_cost_real = sum(_real_select_cost(r) for r in sr_selector.records)
    total_calls = len(sr_diagnoser.records) + len(sr_selector.records)
    # A regex-resolved diagnose record never touches the cache at all
    # (src/diagnose/classifier.py hardcodes cache_hit=False for that path,
    # since there's no cache lookup to hit or miss) — folding it into "not
    # a cache hit" alongside genuine LLM cache misses would make a mostly-
    # free run look like it needed far more real network calls than it did.
    # Every choose-stage call and every LLM-resolved diagnose call DOES
    # check the cache, so those are the only calls "cache hit rate" is
    # computed over.
    regex_resolved_count = sum(1 for r in sr_diagnoser.records if r.diagnosis.llm_model is None)
    cache_relevant_calls = total_calls - regex_resolved_count
    cache_hits = (
        sum(
            1 for r in sr_diagnoser.records
            if r.diagnosis.llm_model is not None and r.diagnosis.cache_hit
        )
        + sum(1 for r in sr_selector.records if r.cache_hit)
    )
    prompt_versions = sorted(
        {r.diagnosis.prompt_version for r in sr_diagnoser.records if r.diagnosis.prompt_version}
        | {r.prompt_version for r in sr_selector.records if r.prompt_version}
    )
    n_sealed = len(sealed_episodes)
    llm_cost_per_100 = (diagnose_cost + select_cost) / n_sealed * 100 if n_sealed else 0.0
    llm_cost_real_per_100 = (
        (diagnose_cost_real + select_cost_real) / n_sealed * 100 if n_sealed else 0.0
    )

    # When a stopping rule fires, every episode after the break is never
    # looped over at all and lands in by_outcome["pending"] alongside any
    # genuine non-created executions (src/runner.py) — in fixture mode
    # FixtureExecutor always returns "created" for a first-time key, so
    # "pending" only ever means "never reached" here. When no stopping rule
    # fired, every episode was looped over regardless of by_outcome shape.
    if sr_summary.stopped_reason:
        episodes_processed = sr_summary.episode_count - sr_summary.by_outcome.get("pending", 0)
    else:
        episodes_processed = sr_summary.episode_count

    section1 = Section1(
        guardrail=guardrail,
        admissibility_rate=sr_summary.admissibility_rate,
        admissibility_decisions_total=len(sr_selector.records),
        throughput_epm=sr_summary.throughput_epm,
        episodes_processed=episodes_processed,
        episodes_in_batch=sr_summary.episode_count,
        stopped_reason=sr_summary.stopped_reason,
        llm_cost_paise_this_run=diagnose_cost + select_cost,
        llm_cost_paise_per_100=llm_cost_per_100,
        llm_cost_paise_real=diagnose_cost_real + select_cost_real,
        llm_cost_paise_real_per_100=llm_cost_real_per_100,
        llm_model=settings.llm_model,
        prompt_versions=prompt_versions or ["(none — every call this run was regex-resolved)"],
        cache_hit_rate=(cache_hits / cache_relevant_calls) if cache_relevant_calls else 0.0,
        cache_hits=cache_hits,
        cache_calls=cache_relevant_calls,
        regex_resolved_count=regex_resolved_count,
    )

    print("=== section 5: exceptions ===")
    execution_failed = sr_summary.by_outcome.get("execution_failed", 0)
    section5 = build_exceptions_section(sr_conn, sr_run_id, execution_failed)

    section4 = Section4(
        second_rail=sr_figure, baseline=baseline_figure, swept_params=list(SWEPT_PARAMS),
        param_reasoning=PARAM_REASONING, window_note=WINDOW_NOTE, methodology_note=METHODOLOGY_NOTE,
    )

    section7 = build_section7()

    shift_summary = "see holdout/SHIFT.md"
    if SHIFT_PATH.exists():
        for line in SHIFT_PATH.read_text(encoding="utf-8").splitlines():
            if line.startswith("`BANK_E` is reserved"):
                shift_summary = line.strip().replace("`", "")
                break

    meta = RunMeta(
        run_id=sr_run_id, git_sha=_git_sha(), config_hash=config_hash(bundle), date=_now_iso(),
        seal_line=f"sha256 verified — {len(sealed_episodes)} episodes (see `holdout/SEAL.sha256`)",
        shift_summary=shift_summary, attribution_rule_id="AR-01",
        attribution_window_hours=g.attribution_window_hours,
    )

    data = ReportData(
        meta=meta, section1=section1, section2=section2, section3=section3,
        section4=section4, section5=section5, section6=section6, section7=section7,
    )

    print("=== rendering evidence/report.md ===")
    REPORT_PATH.write_text(render_report(data), encoding="utf-8")

    print("=== rendering evidence/charts/*.png ===")
    report_charts.render_all(
        by_outcome=sr_summary.by_outcome, per_class=section6.per_class,
        second_rail=sr_figure, baseline=baseline_figure, exception_rows=section5.rows,
    )

    METRICS_PATH.write_text(
        json.dumps(
            {
                "run_id": sr_run_id, "baseline_run_id": baseline_run_id,
                "second_rail_by_outcome": sr_summary.by_outcome,
                "baseline_by_outcome": baseline_summary.by_outcome,
                "admissibility_rate": sr_summary.admissibility_rate,
                "throughput_epm": sr_summary.throughput_epm,
                "llm_cost_paise_this_run": section1.llm_cost_paise_this_run,
                "llm_cost_paise_real": section1.llm_cost_paise_real,
                "second_rail_recovery": vars(sr_figure),
                "baseline_recovery": vars(baseline_figure),
                "classification_accuracy": section6.accuracy,
                "elapsed_s": time.monotonic() - t_start,
            },
            indent=2, sort_keys=True, default=str,
        ),
        encoding="utf-8",
    )

    sr_conn.close()
    baseline_conn.close()

    elapsed = time.monotonic() - t_start
    print(f"\n=== done in {elapsed:.1f}s — wrote evidence/report.md, evidence/charts/*.png, "
          f"evidence/eval_metrics.json ===")


def _gate_only_suppressed(conn, run_id: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS n FROM exception_entry WHERE run_id = ? AND stage = 'gate'", (run_id,)
    ).fetchone()["n"]


def _gate_eligible_from_db(conn, run_id: str, summary: RunSummary) -> int:
    """Episodes that passed all 7 gate checks. Bounded by how many episodes
    this run actually looped over, not the full batch size — a stopping
    rule (src/gate/stopping.py) can halt a run before every episode in the
    batch is even reached, and an unreached episode was never gated at all,
    so it must not be counted as "eligible" here (see this module's
    docstring on the cap_breach stopping rule firing against the sealed
    split's shifted amount distribution)."""
    processed = summary.episode_count
    if summary.stopped_reason:
        processed -= summary.by_outcome.get("pending", 0)
    gate_suppressed = _gate_only_suppressed(conn, run_id)
    return processed - gate_suppressed


if __name__ == "__main__":
    typer.run(main)
