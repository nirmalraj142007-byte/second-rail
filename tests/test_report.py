"""Tests for src/report/ and scripts/eval.py — these are guardrails on
honesty, not on formatting. Each test enforces one clause from the judge
expectations §3 ordering/evidence requirements: no bare point estimate, no
unqualified rupee figure, guardrail correctness leads, "sealed split" not
"held-out test set", the excluded-episode count is exact.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.report import render
from src.report.render import (
    ClassMetric,
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
)

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# a realistic, self-consistent sample report — every honesty test below
# renders this (or a variant of it), never a hand-wired snippet, so a test
# that passes here reflects what the real renderer actually produces.
# ---------------------------------------------------------------------------


def _sample_data(*, execution_failed_count: int = 0) -> ReportData:
    meta = RunMeta(
        run_id="01TESTRUN000000000000000000",
        git_sha="abc1234",
        config_hash="deadbeef" * 8,
        date="2026-09-01T12:00:00+05:30",
        seal_line="sha256 verified — 200 episodes (see `holdout/SEAL.sha256`)",
        shift_summary="BANK_E is reserved for the sealed split only.",
        attribution_rule_id="AR-01",
        attribution_window_hours=48,
    )
    section1 = Section1(
        guardrail=GuardrailProof(
            n=200, mode="live", duplicate_links_created=0, cap_breaches=0,
            quiet_hour_contacts=0, idempotency_detected=198, idempotency_total=200,
            verification_note="200 link(s) on the real Razorpay API carry notes.run_id=...",
            source_path="evidence/guardrail_proof.json", cancelled_count=200,
        ),
        admissibility_rate=1.0, admissibility_decisions_total=150, throughput_epm=42.0,
        episodes_processed=200, episodes_in_batch=200, stopped_reason=None,
        llm_cost_paise_this_run=210, llm_cost_paise_per_100=105.0,
        llm_cost_paise_real=1840, llm_cost_paise_real_per_100=920.0,
        llm_model="openai/gpt-oss-20b", prompt_versions=["classify_v1", "select_v1"],
        cache_hit_rate=0.9, cache_hits=135, cache_calls=150,
    )
    section2 = Section2(
        harvested_error_count=20, harvest_capture_note="captured 2026-08-26",
        doc_source_note="evidence/razorpay_error_codes_snapshot.md",
        rows=[
            ExternallyAnchoredRow(
                label="harvested strings (raw, real fields)", n_episodes=20,
                regex_accuracy=0.05, llm_accuracy=0.20, llm_skipped_reason=None,
            ),
            ExternallyAnchoredRow(
                label="Razorpay doc snapshot (independent label source)", n_episodes=17,
                regex_accuracy=0.8235, llm_accuracy=0.8824, llm_skipped_reason=None,
            ),
        ],
    )
    section3 = Section3(
        weakest_source_label="harvested strings (raw, real fields)",
        weakest_n=20,
        weakest_llm_accuracy=0.20,
        weakest_regex_accuracy=0.05,
        head_to_head_summary="regex and the LLM tied on 3/5 of the top 5 error families by "
        "volume (self-generated split).",
        rows=[
            HeadToHeadRow(family="insufficient_fund", volume=88, regex_accuracy=1.0,
                           llm_accuracy=1.0, llm_sample_note="n=8"),
            HeadToHeadRow(family="gateway_technical_error", volume=69, regex_accuracy=1.0,
                           llm_accuracy=0.9, llm_sample_note="n=8"),
        ],
    )
    second_rail = RecoveryFigure(
        label="Second Rail", gross_low_paise=107556, gross_base_paise=153651,
        gross_high_paise=199747, fp_low_paise=0, fp_base_paise=0, fp_high_paise=0,
        net_low_paise=107556, net_base_paise=153651, net_high_paise=199747,
        contacted_count=120, gate_eligible_count=150, fp_count=0,
        batch_size=200, stopped_reason=None,
    )
    baseline = RecoveryFigure(
        label="FIXED_RETRY_AT_T30 baseline", gross_low_paise=130000, gross_base_paise=185000,
        gross_high_paise=240000, fp_low_paise=0, fp_base_paise=0, fp_high_paise=0,
        net_low_paise=130000, net_base_paise=185000, net_high_paise=240000,
        contacted_count=150, gate_eligible_count=150, fp_count=0,
        batch_size=200, stopped_reason=None,
    )
    section4 = Section4(
        second_rail=second_rail, baseline=baseline, swept_params=list(SWEPT_PARAMS),
        param_reasoning=PARAM_REASONING, window_note=WINDOW_NOTE,
        methodology_note=METHODOLOGY_NOTE,
    )
    section5 = Section5(
        rows=[ExceptionRow(reason_code="duplicate_episode_this_run", count=5),
              ExceptionRow(reason_code="no_action_selected", count=12)],
        worked_examples=[
            WorkedException(payment_id="pay_synthetic_00401", instrument="card",
                             amount_rupees=92.84, error_reason="card_disabled_for_online_payments",
                             reason_code="episode_age_exceeds_cap", reason_text="age check failed"),
        ],
        execution_failed_count=execution_failed_count,
        total_exceptions=17 + execution_failed_count,
    )
    section6 = Section6(
        n_episodes=200, accuracy=0.97,
        per_class=[ClassMetric(class_id="C1", precision=0.98, recall=0.95, f1=0.965, support=40)],
        class_ids=["C1", "C2"],
        confusion_matrix={"C1": {"C1": 38, "C2": 2}, "C2": {"C1": 1, "C2": 39}},
        confusion_costs=[],
        confusion_cost_methodology="deterministic fallback_priority-first action, disclosed",
    )
    section7 = Section7(items=[
        "Real customer behaviour.",
        "Generalisation beyond the seeded distribution shift.",
        "Anything at production volume.",
        "Partial payments.",
        "Recoveries through channels we did not create.",
    ])
    return ReportData(
        meta=meta, section1=section1, section2=section2, section3=section3,
        section4=section4, section5=section5, section6=section6, section7=section7,
    )


# ---------------------------------------------------------------------------
# 1. bare float recovery value raises
# ---------------------------------------------------------------------------


def test_bare_float_recovery_value_raises() -> None:
    with pytest.raises(render.BareRecoveryValueError):
        render.format_rupee_range(107556.0, 153651, 199747)


def test_integer_range_renders_fine() -> None:
    text = render.format_rupee_range(107556, 153651, 199747)
    assert "Rs" in text and " - " in text


# ---------------------------------------------------------------------------
# 2. the rendered report's first numeric table is the guardrail table
# ---------------------------------------------------------------------------


def _markdown_tables(text: str) -> list[list[str]]:
    """Return every markdown table found, each as its list of raw lines,
    in document order."""
    tables: list[list[str]] = []
    current: list[str] = []
    for line in text.splitlines():
        if line.strip().startswith("|"):
            current.append(line)
        elif current:
            tables.append(current)
            current = []
    if current:
        tables.append(current)
    return tables


def test_first_table_is_guardrail_correctness() -> None:
    text = render_report(_sample_data())
    tables = _markdown_tables(text)
    assert tables, "no markdown table found in the rendered report"
    first_table_text = "\n".join(tables[0])
    assert "duplicate links created" in first_table_text
    assert "cap breaches" in first_table_text


# ---------------------------------------------------------------------------
# 3. every rupee figure carries a range or an explicit qualifier
# ---------------------------------------------------------------------------

_RUPEE_RE = re.compile(r"Rs\s?[\d,]+(?:\.\d+)?")
_QUALIFIER_WORDS = ("illustrative", "assumption", "measured")


def test_no_unqualified_rupee_figure() -> None:
    text = render_report(_sample_data())
    violations = []
    for line in text.splitlines():
        matches = _RUPEE_RE.findall(line)
        if not matches:
            continue
        is_range = len(matches) >= 2 or " - Rs" in line or "- Rs" in line
        has_qualifier = any(w in line.lower() for w in _QUALIFIER_WORDS)
        if not (is_range or has_qualifier):
            violations.append(line)
    assert not violations, "unqualified rupee figure(s) found:\n" + "\n".join(violations)


# ---------------------------------------------------------------------------
# 4. "held-out test set" never appears; "sealed split" does
# ---------------------------------------------------------------------------


def test_sealed_split_not_held_out_test_set() -> None:
    text = render_report(_sample_data())
    assert "held-out test set" not in text.lower()
    assert "sealed split" in text.lower()


# ---------------------------------------------------------------------------
# 5. section 3 exists and is non-empty
# ---------------------------------------------------------------------------


def test_section3_exists_and_nonempty() -> None:
    text = render_report(_sample_data())
    idx3 = text.index("## 3. Where the claim gets weakest")
    idx4 = text.index("## 4. Design target")
    body = text[idx3:idx4].strip()
    assert len(body) > len("## 3. Where the claim gets weakest")
    # The section must state the actual weakest externally-anchored
    # accuracy, not just a regex-vs-LLM comparison that may show a tie or
    # an LLM win (as it currently does) rather than a "loss" — see the
    # sample data's weakest_llm_accuracy=0.20.
    assert "20.0%" in body


def test_section3_leads_with_absolute_accuracy_not_the_comparison() -> None:
    """Structural invariant, not sample-data-specific: the section always
    states the weakest externally-anchored accuracy as the headline and
    explicitly de-emphasizes who-won-the-comparison framing — regardless
    of whether the regex-vs-LLM head-to-head happens to show a tie, an
    LLM win, or (on some future run) a genuine regex win. A "which
    approach wins" framing alone, with no absolute number, is what led
    this section to claim a loss that never happened in the data."""
    text = render_report(_sample_data())
    idx3 = text.index("## 3. Where the claim gets weakest")
    idx4 = text.index("## 4. Design target")
    body = text[idx3:idx4]
    assert "not which method produced it" in body


# ---------------------------------------------------------------------------
# 6. `make eval`'s pipeline completes with LLM_API_KEY unset, using cache only
# ---------------------------------------------------------------------------


def test_eval_pipeline_completes_with_no_llm_key_using_cache_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercises the exact mechanism `make eval` relies on for its "no API
    key required" acceptance test: a Settings instance with no provider and
    no key configured (llm_provider="none" -> NullClient, which raises if
    .complete() is ever actually called) run against a small, already-cached
    episode slice. The cache lookup happens before the client is ever
    touched (src/diagnose/classifier.py, src/choose/selector.py), so this
    only proves anything if every prompt this slice produces was already
    cached by an earlier run with a real key — true for the first few
    sealed episodes after any prior `python -m scripts.eval` pass.

    DB paths are monkeypatched to tmp_path so this never collides with a
    real `make eval` run's own evidence/eval_second_rail.db.
    """
    import scripts.eval as ev
    from src.config import Settings
    from src.config_models import load_all
    from src.runner import load_episodes

    monkeypatch.setattr(ev, "SR_DB_PATH", tmp_path / "eval_second_rail.db")

    bundle = load_all()
    settings = Settings(llm_provider="none", llm_api_key=None)
    sealed_episodes = load_episodes([ev.SEALED_PATH])[:6]

    sr_summary, sr_run_id, sr_conn, sr_diagnoser, sr_selector = ev.run_second_rail(
        sealed_episodes, settings, bundle, live=False
    )
    try:
        assert sr_summary.episode_count == 6
        total = sum(sr_summary.by_outcome.values())
        assert total == 6
    finally:
        sr_conn.close()


# ---------------------------------------------------------------------------
# 7. the excluded-episode count in section 5 matches execution_failed exactly
# ---------------------------------------------------------------------------


def test_section5_excluded_count_matches_execution_failed_exactly() -> None:
    data = _sample_data(execution_failed_count=3)
    text = render_report(data)
    assert "`execution_failed` episodes are **excluded**" in text
    assert "3 this run" in text

    zero_data = _sample_data(execution_failed_count=0)
    zero_text = render_report(zero_data)
    assert "0 this run" in zero_text
