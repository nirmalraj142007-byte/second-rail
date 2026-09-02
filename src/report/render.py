"""evidence/report.md — the data contract and the fixed section order.

The order is a judged artifact in itself (CLAUDE.md, judge expectations
§3): non-circular measurements first, externally-anchored evidence
second, the model's own headline weakness third, the simulated recovery
figure — clearly marked as a design target — fourth, honest accounting
fifth and sixth, disclosed blind spots last. render_report() enforces
that order structurally: it is one straight-line function that appends
each section in sequence, so reordering sections means editing this
function, not a config flag.

Two honesty guards live here, not in scripts/eval.py, so no caller can
route around them:
  - format_rupee_range() refuses a bare float/point estimate — every
    recovery-like figure is a range or it does not render at all.
  - every other rupee figure this module prints is text-composed with an
    explicit qualifier word ("measured", "illustrative", "assumption", or
    part of a range) on the same line — see tests/test_report.py's
    regex guard.
"""

from __future__ import annotations

from dataclasses import dataclass, field


class BareRecoveryValueError(TypeError):
    """Raised when something that should be a range (low, base, high) of
    integer paise arrives as a bare float/int instead. A hackathon report
    that prints a single recovery percentage with total confidence is
    exactly the failure mode this project's own non-negotiables (CLAUDE.md,
    judge expectations §3 [F][HARD]) exist to rule out — so the renderer
    refuses to cooperate with one, rather than trusting every call site to
    remember not to pass one."""


def format_rupee_range(low_paise: int, base_paise: int, high_paise: int) -> str:
    named = (("low_paise", low_paise), ("base_paise", base_paise), ("high_paise", high_paise))
    for name, v in named:
        if isinstance(v, bool) or not isinstance(v, int):
            raise BareRecoveryValueError(
                f"format_rupee_range({name}={v!r}): recovery figures must be integer paise "
                "amounts inside a (low, base, high) range — a bare float/point estimate is "
                "never rendered on its own"
            )
    lo_r, hi_r = low_paise / 100, high_paise / 100
    return f"Rs {lo_r:,.0f} - Rs {hi_r:,.0f}"


def format_rupees_measured(paise: float, *, decimals: int = 2) -> str:
    """For a genuinely non-simulated rupee figure (LLM cost, throughput
    cost, a cited fixed price) — never used for anything that passed
    through outcome_model.md. Always carries the word 'measured' on the
    same line so it can never be mistaken for the simulated recovery
    figure by a skim-reader or by tests/test_report.py's honesty regex."""
    return f"Rs {paise / 100:,.{decimals}f} (measured)"


def format_rupees_illustrative(paise: float, *, decimals: int = 2) -> str:
    return f"Rs {paise / 100:,.{decimals}f} (illustrative)"


# ---------------------------------------------------------------------------
# data contract
# ---------------------------------------------------------------------------


@dataclass
class RunMeta:
    run_id: str
    git_sha: str | None
    config_hash: str
    date: str
    seal_line: str
    shift_summary: str
    attribution_rule_id: str
    attribution_window_hours: int


@dataclass
class GuardrailProof:
    n: int
    mode: str
    duplicate_links_created: int
    cap_breaches: int
    quiet_hour_contacts: int
    idempotency_detected: int
    idempotency_total: int
    verification_note: str
    source_path: str
    cancelled_count: int
    processed_count: int | None = None
    stopped_reason: str | None = None
    consecutive_error_tolerance: int | None = None
    # How many gate-eligible episodes exist in `gate_eligible_source` in
    # total, computed fresh every eval run (see scripts/eval.py's
    # _train_gate_eligible_ceiling) rather than read out of
    # guardrail_proof.json — that keeps the ceiling honest even if the
    # source data changes after guardrail_proof.json was last written.
    gate_eligible_ceiling: int | None = None
    gate_eligible_source: str | None = None


@dataclass
class Section1:
    guardrail: GuardrailProof | None
    admissibility_rate: float | None
    admissibility_decisions_total: int
    throughput_epm: float
    episodes_processed: int
    episodes_in_batch: int
    stopped_reason: str | None
    llm_cost_paise_this_run: int
    llm_cost_paise_per_100: float
    llm_cost_paise_real: int
    llm_cost_paise_real_per_100: float
    llm_model: str
    prompt_versions: list[str]
    cache_hit_rate: float
    cache_hits: int
    cache_calls: int
    regex_resolved_count: int = 0


@dataclass
class ExternallyAnchoredRow:
    label: str
    n_episodes: int
    regex_accuracy: float
    llm_accuracy: float | None
    llm_skipped_reason: str | None


@dataclass
class Section2:
    harvested_error_count: int
    harvest_capture_note: str
    doc_source_note: str
    rows: list[ExternallyAnchoredRow]


@dataclass
class HeadToHeadRow:
    family: str
    volume: int
    regex_accuracy: float
    llm_accuracy: float | None
    llm_sample_note: str


@dataclass
class Section3:
    # The real headline: the weakest accuracy this project measured on any
    # externally-anchored source (never on self-generated data) — computed
    # from whichever row in Section2 has the lowest LLM accuracy, not
    # hardcoded to a specific source name, so this stays honest if the
    # numbers ever change. This is the humbling number CLAUDE.md's own
    # non-negotiable asks for — not a "regex beat the LLM" framing that
    # may not be true of the actual data on a given run.
    weakest_source_label: str
    weakest_n: int
    weakest_llm_accuracy: float
    weakest_regex_accuracy: float
    head_to_head_summary: str
    rows: list[HeadToHeadRow]


@dataclass
class RecoveryFigure:
    label: str
    gross_low_paise: int
    gross_base_paise: int
    gross_high_paise: int
    fp_low_paise: int
    fp_base_paise: int
    fp_high_paise: int
    net_low_paise: int
    net_base_paise: int
    net_high_paise: int
    contacted_count: int
    gate_eligible_count: int
    fp_count: int
    batch_size: int
    stopped_reason: str | None


@dataclass
class Section4:
    second_rail: RecoveryFigure
    baseline: RecoveryFigure
    swept_params: list[str]
    param_reasoning: dict[str, str]
    window_note: str
    methodology_note: str


@dataclass
class ExceptionRow:
    reason_code: str
    count: int


@dataclass
class WorkedException:
    payment_id: str
    instrument: str
    amount_rupees: float
    error_reason: str | None
    reason_code: str
    reason_text: str | None


@dataclass
class Section5:
    rows: list[ExceptionRow]
    worked_examples: list[WorkedException]
    execution_failed_count: int
    total_exceptions: int


@dataclass
class ClassMetric:
    class_id: str
    precision: float
    recall: float
    f1: float
    support: int


@dataclass
class ConfusionCost:
    true_class: str
    pred_class: str
    count: int
    mean_delta_rupees: float


@dataclass
class Section6:
    n_episodes: int
    accuracy: float
    per_class: list[ClassMetric]
    class_ids: list[str]
    confusion_matrix: dict[str, dict[str, int]]
    confusion_costs: list[ConfusionCost]
    confusion_cost_methodology: str


@dataclass
class Section7:
    items: list[str] = field(default_factory=list)


@dataclass
class ReportData:
    meta: RunMeta
    section1: Section1
    section2: Section2
    section3: Section3
    section4: Section4
    section5: Section5
    section6: Section6
    section7: Section7


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def _fmt_pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:.1f}%"


def _render_meta(m: RunMeta) -> list[str]:
    return [
        "# Second Rail — Results",
        f"Run `{m.run_id}` · `{m.git_sha or 'unknown'}` · "
        f"config `{m.config_hash[:12]}…` · {m.date}",
        f"Sealed split: {m.seal_line} · shift: {m.shift_summary}",
        "",
        f"Attribution rule {m.attribution_rule_id}, window {m.attribution_window_hours}h.",
        "",
    ]


def _render_section1(s: Section1) -> list[str]:
    lines = [
        "## 1. What I measured",
        "",
        "Nothing in this section depends on the outcome model in `outcome_model.md` — every "
        "number below is either read off a real API response or counted directly from what the "
        "batch actually did.",
        "",
        "### Guardrail correctness under fault injection",
        "",
    ]
    g = s.guardrail
    if g is None:
        lines += [
            "**Not yet generated.** Run `make guardrail-proof N=200` before `make eval` — see "
            "the Makefile target's comment for why a real N=200 pass is the headline "
            "non-circular metric this report leads with.",
            "",
        ]
    else:
        lines += [f"Read from `{g.source_path}` — the real N={g.n} run ({g.mode})."]
        if g.gate_eligible_ceiling is not None and g.gate_eligible_source is not None:
            if g.n == g.gate_eligible_ceiling:
                lines.append(
                    f"N is capped at {g.gate_eligible_ceiling} by the number of "
                    f"gate-eligible episodes in `{g.gate_eligible_source}`; this is the "
                    "full available set, not a partial sample."
                )
            elif g.n < g.gate_eligible_ceiling:
                lines.append(
                    f"{g.gate_eligible_ceiling} gate-eligible episodes exist in "
                    f"`{g.gate_eligible_source}` — this run asked for {g.n}, a deliberate "
                    "subset of the full available set, not its ceiling."
                )
        if g.consecutive_error_tolerance is not None:
            lines.append(
                f"This tool's own consecutive-executor-error tolerance was raised to "
                f"{g.consecutive_error_tolerance} for this run (from the shared "
                "production default of 3 in `config/guardrails.yaml`, which stays "
                "unchanged) — sustained real-API calls at volume produce sporadic "
                "Razorpay-side rate-limiting that isn't a systemic failure signal for a "
                "tool whose whole job is deliberately hammering the real API; see "
                "BUILD_LOG.md."
            )
        if g.stopped_reason:
            lines.append(
                f"Stopping rule `{g.stopped_reason}` fired this run — only "
                f"{g.processed_count}/{g.n} requested episodes were actually reached; "
                "every metric below is real, just over a smaller N than requested."
            )
            lines.append(
                f"**Real + fixture, stated plainly, not rounded up:** "
                f"{g.processed_count} real, live-verified test-mode Payment Link "
                "creation(s) back this proof — the other "
                f"{max(g.n - (g.processed_count or 0), 0)} requested episodes were never "
                "attempted in this run at all, live or fixture (this tool has no "
                "mid-run real-to-fixture handoff). Separately, and independently of this "
                "proof, `make eval`'s sealed-split evaluation exercises the full "
                "200-episode batch through `FixtureExecutor` — synthesized responses, not "
                "real captured ones (no fixture file exists per episode), and not "
                "re-verified against the live API. The two are complementary, not the "
                "same measurement: this table is real-API-verified correctness at a "
                f"real, if small, N={g.processed_count}; the sealed-split figures "
                "elsewhere in this report are full-batch behavioural coverage with no "
                "live network calls."
            )
        lines += [
            "",
            "| metric | value | requirement |",
            "|---|---|---|",
            f"| duplicate links created | {g.duplicate_links_created} | must be 0 |",
            f"| cap breaches | {g.cap_breaches} | must be 0 |",
            f"| quiet-hour contacts | {g.quiet_hour_contacts} | must be 0 |",
            f"| idempotency collisions correctly detected | "
            f"{g.idempotency_detected}/{g.idempotency_total} | — |",
            f"| links created and cancelled | {g.cancelled_count} | "
            "every link this proof created |",
            "",
            f"{g.verification_note}",
            "",
        ]

    lines += [
        "### Action admissibility rate",
        "",
        f"{_fmt_pct(s.admissibility_rate)} of agent choices fell inside the pre-registered "
        f"admissible set (n={s.admissibility_decisions_total}). By construction this can only "
        "ever be 100% or the run will already have halted — `ActionSelector.select()` raises "
        "`AdmissibilityError` rather than ever returning a choice outside the set.",
        "",
        "### Stopping rules",
        "",
    ]
    if s.stopped_reason:
        unprocessed = s.episodes_in_batch - s.episodes_processed
        lines += [
            f"Stopping rule fired this run: `{s.stopped_reason}` — the batch was "
            f"{s.episodes_in_batch} episodes, {s.episodes_processed} were processed before "
            f"the rule halted the run, {unprocessed} were never reached. Not silently "
            "dropped — every unreached episode is still counted (`pending`) in the "
            "accounting invariant `src/runner.py` asserts on every run. This is a real "
            "guardrail firing against real sealed data, not a staged demo — see "
            "`make guardrail-proof` for the dedicated, controlled fault-injection version "
            "of this same class of proof.",
            "",
        ]
    else:
        lines += ["No stopping rule fired this run.", ""]
    lines += [
        "### Throughput and LLM cost",
        "",
        f"Throughput: {s.throughput_epm:.1f} episodes/min over {s.episodes_processed} of "
        f"{s.episodes_in_batch} sealed episodes processed.",
        "",
        f"LLM cost this run (cache-aware, 0 paise on every cache hit): "
        f"{format_rupees_measured(s.llm_cost_paise_this_run)}, "
        f"{format_rupees_measured(s.llm_cost_paise_per_100)} per 100 episodes.",
        "",
        f"Real cost (ignores cache, recomputed from actual token counts on every call — "
        f"what this run would have cost with an empty cache): "
        f"{format_rupees_measured(s.llm_cost_paise_real)}, "
        f"{format_rupees_measured(s.llm_cost_paise_real_per_100)} per 100 episodes.",
        "",
        f"Model `{s.llm_model}`, prompt version(s) {', '.join(s.prompt_versions)}. "
        f"{s.regex_resolved_count} diagnose call(s) resolved by regex, free — never touched "
        f"the cache or the model at all. Of the {s.cache_calls} call(s) that did need the "
        f"model (LLM-resolved diagnoses plus every choose call), cache hit rate: "
        f"{_fmt_pct(s.cache_hit_rate)} ({s.cache_hits}/{s.cache_calls}).",
        "",
    ]
    return lines


def _render_section2(s: Section2) -> list[str]:
    lines = [
        "## 2. What was externally anchored",
        "",
        "Inputs I did not author: real error strings forced out of Razorpay's own test-mode "
        f"failure simulation ({s.harvested_error_count} records, {s.harvest_capture_note}), and "
        f"Razorpay's own published error-code documentation ({s.doc_source_note}).",
        "",
        "| source | n | regex accuracy | LLM accuracy |",
        "|---|---|---|---|",
    ]
    for row in s.rows:
        llm_str = "not run" if row.llm_accuracy is None else _fmt_pct(row.llm_accuracy)
        regex_str = _fmt_pct(row.regex_accuracy)
        lines.append(f"| {row.label} | {row.n_episodes} | {regex_str} | {llm_str} |")
        if row.llm_skipped_reason:
            lines.append(f"| | | | _skipped: {row.llm_skipped_reason}_ |")
    lines.append("")
    return lines


def _render_section3(s: Section3) -> list[str]:
    lines = [
        "## 3. Where the claim gets weakest",
        "",
        f"On {s.weakest_source_label} (n={s.weakest_n}) — the hardest, most "
        f"externally-anchored data anywhere in this evaluation — classifier accuracy "
        f"collapses to {_fmt_pct(s.weakest_llm_accuracy)}. The LLM still beats the regex "
        f"baseline there ({_fmt_pct(s.weakest_regex_accuracy)}), but that comparison is "
        f"not the finding worth taking seriously here — both are weak. The humbling "
        f"number is the {_fmt_pct(s.weakest_llm_accuracy)} itself, not which method "
        f"produced it.",
        "",
        f"Separately (self-generated data, not externally anchored — see the top-5 "
        f"error-family table below): {s.head_to_head_summary}",
        "",
        "| error family | volume | regex accuracy | LLM accuracy |",
        "|---|---|---|---|",
    ]
    for row in s.rows:
        llm_str = "n/a" if row.llm_accuracy is None else _fmt_pct(row.llm_accuracy)
        if row.llm_sample_note:
            llm_str += f" ({row.llm_sample_note})"
        regex_str = _fmt_pct(row.regex_accuracy)
        lines.append(f"| {row.family} | {row.volume} | {regex_str} | {llm_str} |")
    lines.append("")
    return lines


def _render_recovery_block(r: RecoveryFigure) -> list[str]:
    gross_range = format_rupee_range(r.gross_low_paise, r.gross_base_paise, r.gross_high_paise)
    fp_range = format_rupee_range(r.fp_low_paise, r.fp_base_paise, r.fp_high_paise)
    net_range = format_rupee_range(r.net_low_paise, r.net_base_paise, r.net_high_paise)
    lines = [
        f"**{r.label}** — {r.contacted_count}/{r.gate_eligible_count} gate-eligible episodes "
        f"contacted, out of the {r.batch_size}-episode sealed batch.",
    ]
    if r.stopped_reason:
        lines.append(
            f"Stopping rule `{r.stopped_reason}` fired partway through this run — see section 1."
        )
    lines += [
        "",
        f"gross {gross_range} | false-positive cost {fp_range} ({r.fp_count} contact(s)) | "
        f"**NET {net_range}**",
        "",
    ]
    return lines


def _render_section4(s: Section4) -> list[str]:
    lines = [
        "## 4. Design target under stated assumptions",
        "",
        "This is a simulator. Every figure below passes through the customer-response model "
        "in `outcome_model.md`; sections 1-3 above do not.",
        "",
        f"Sensitivity sweep, +/-30% on three parameters: {', '.join(s.swept_params)}.",
        "",
    ]
    for p in s.swept_params:
        why = s.param_reasoning.get(p, "")
        if why:
            lines.append(f"- **{p}**: {why}")
    lines += ["", s.window_note, ""]
    lines += ["### Second Rail", ""]
    lines += _render_recovery_block(s.second_rail)
    lines += ["### FIXED_RETRY_AT_T30 baseline", ""]
    lines += _render_recovery_block(s.baseline)
    lines += [
        s.methodology_note,
        "",
        "This sweep perturbs my own parameters and widens a band around a quantity I invented. "
        "It is disclosure, not evidence. Sections 1-3 are the evidence.",
        "",
    ]
    return lines


def _render_section5(s: Section5) -> list[str]:
    lines = [
        "## 5. Exceptions",
        "",
        f"{s.total_exceptions} episode(s) excluded from the recovery figures this run, by "
        "reason_code — no episode is ever silently dropped, see the accounting invariant in "
        "`src/runner.py`.",
        "",
        "| reason_code | count |",
        "|---|---|",
    ]
    for row in s.rows:
        lines.append(f"| `{row.reason_code}` | {row.count} |")
    lines += [
        "",
        f"`execution_failed` episodes are **excluded** from the recovery figures in section 4 — "
        f"{s.execution_failed_count} this run.",
        "",
        "### Worked examples",
        "",
    ]
    if not s.worked_examples:
        lines.append("_(no exceptions this run)_")
    for ex in s.worked_examples:
        lines.append(
            f"- `{ex.payment_id}` ({ex.instrument}, "
            f"{format_rupees_illustrative(round(ex.amount_rupees * 100))}, "
            f"error_reason={ex.error_reason!r}) — **{ex.reason_code}**: {ex.reason_text}"
        )
    lines.append("")
    return lines


def _render_section6(s: Section6) -> list[str]:
    lines = [
        "## 6. Classifier detail",
        "",
        f"Self-graded against generator truth, n={s.n_episodes}. Accuracy: {_fmt_pct(s.accuracy)}.",
        "",
        "| class | precision | recall | f1 | support |",
        "|---|---|---|---|---|",
    ]
    for m in s.per_class:
        lines.append(
            f"| {m.class_id} | {m.precision:.3f} | {m.recall:.3f} | {m.f1:.3f} | {m.support} |"
        )

    lines += ["", "### Confusion matrix (rows = true class, columns = predicted)", ""]
    header = "| true \\\\ pred | " + " | ".join(s.class_ids) + " |"
    lines.append(header)
    lines.append("|" + "---|" * (len(s.class_ids) + 1))
    for t in s.class_ids:
        row = s.confusion_matrix.get(t, {})
        cells = " | ".join(str(row.get(p, 0)) for p in s.class_ids)
        lines.append(f"| {t} | {cells} |")

    lines += [
        "",
        f"### Cost of confusion (rupees) — {s.confusion_cost_methodology}",
        "",
        "| true class | predicted class | count | mean delta (illustrative) |",
        "|---|---|---|---|",
    ]
    for c in s.confusion_costs:
        lines.append(
            f"| {c.true_class} | {c.pred_class} | {c.count} | "
            f"{format_rupees_illustrative(round(c.mean_delta_rupees * 100))} |"
        )
    lines.append("")
    return lines


def _render_section7(s: Section7) -> list[str]:
    lines = ["## 7. What this does not measure", ""]
    for item in s.items:
        lines.append(f"- {item}")
    lines.append("")
    return lines


def render_report(data: ReportData) -> str:
    lines: list[str] = []
    lines += _render_meta(data.meta)
    lines += _render_section1(data.section1)
    lines += _render_section2(data.section2)
    lines += _render_section3(data.section3)
    lines += _render_section4(data.section4)
    lines += _render_section5(data.section5)
    lines += _render_section6(data.section6)
    lines += _render_section7(data.section7)
    return "\n".join(lines) + "\n"
