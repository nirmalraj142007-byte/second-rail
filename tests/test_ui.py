"""Tests for the terminal demo surface (src/ui/).

Covers the phase spec's four required checks:
  1. each of the 8 required states renders without raising
  2. the cluster refusal renders exactly one line for 40 episodes
  3. the approval prompt times out at 60s (freezegun) and auto-rejects with
     the reason recorded
  4. no ANSI-only signalling: every state's plain text contains its label
"""

from __future__ import annotations

from datetime import timedelta
from zoneinfo import ZoneInfo

from freezegun import freeze_time
from rich.console import Console

from src.choose.policy import PolicyMatch
from src.choose.selector import Selection
from src.diagnose.classifier import Diagnosis
from src.execute.executor import ExecutionResult
from src.gate.checks import CheckResult, Episode
from src.runner import RunSummary
from src.ui.live import LiveRunView, render_approval_panel

IST = ZoneInfo("Asia/Kolkata")


def _episode(**overrides) -> Episode:
    base = dict(
        episode_id="ep_017",
        payment_id="pay_TEST00000000000017",
        customer_id="cust_0001",
        amount_paise=124000,
        instrument="upi",
        issuer_family="BANK_A",
        error_code="BAD_REQUEST_ERROR",
        error_reason="insufficient_funds",
        failed_at="2026-08-30T13:55:00+05:30",
        received_at="2026-08-30T14:03:11+05:30",
        split="train",
    )
    base.update(overrides)
    return Episode.model_validate(base)


def _diagnosis(**overrides) -> Diagnosis:
    base = dict(
        episode_id="ep_017",
        method="regex",
        class_id="C3_insufficient_funds",
        confidence=0.86,
        rationale="regex baseline matched",
        llm_model=None,
        prompt_hash=None,
        prompt_version=None,
        cache_hit=False,
        latency_ms=0,
        cost_paise=0,
        input_tokens=0,
        output_tokens=0,
        llm_degraded=False,
        features_used=["error_reason"],
    )
    base.update(overrides)
    return Diagnosis(**base)


def _match(**overrides) -> PolicyMatch:
    base = dict(
        policy_rule_id="R-001",
        admissible_actions=["link_alt_instrument", "open_ticket", "no_action"],
        escalation_tier="auto",
        amount_band="band_low",
        fallback_priority=["no_action"],
    )
    base.update(overrides)
    return PolicyMatch(**base)


def _selection(**overrides) -> Selection:
    base = dict(
        episode_id="ep_017",
        chosen_action="link_alt_instrument",
        features_used=["error_code"],
        features_used_outside_whitelist=[],
        rationale="chosen because...",
        customer_copy="please retry",
        inside_admissible_set=True,
        llm_degraded=False,
        policy_rule_id="R-001",
        llm_model="test-model",
        prompt_hash="abc123",
        prompt_version="select_v1",
        cache_hit=False,
        latency_ms=10,
        cost_paise=0,
        input_tokens=0,
        output_tokens=0,
    )
    base.update(overrides)
    return Selection(**base)


def _execution_result(**overrides) -> ExecutionResult:
    base = dict(
        status="created", idempotency_key="abc", plink_id="plink_Nx00001", response_code=200
    )
    base.update(overrides)
    return ExecutionResult(**base)


def _summary(**overrides) -> RunSummary:
    base = dict(
        run_id="01J8RUNEXAMPLE",
        episode_count=25,
        by_outcome={"actioned": 22, "suppressed": 2, "pending": 1},
        by_escalation_tier={"auto": 20, "human_keystroke": 5},
        exception_count=2,
        elapsed_s=3.2,
        throughput_epm=468.8,
        stopped_reason=None,
        admissibility_rate=1.0,
    )
    base.update(overrides)
    return RunSummary(**base)


def _console() -> Console:
    # width=200 so a logical line never physically wraps — otherwise
    # "exactly one line" assertions would be counting terminal rows, not
    # log lines.
    return Console(record=True, width=200, force_terminal=True, no_color=False)


def _run_streaming_episode(view: LiveRunView) -> None:
    ep = _episode()
    view.episode_start(ep)
    for name in ("duplicate", "terminal_seen", "opt_out", "episode_age"):
        view.guardrail(CheckResult(name, "pass", None))
    view.diagnosis(_diagnosis())
    match = _match()
    view.candidates(match)
    view.decision(_selection(), "auto")
    view.execution(_execution_result())


# ---------------------------------------------------------------------------
# 1. each of the 8 required states renders without raising
# ---------------------------------------------------------------------------

STATE_NAMES = [
    "empty", "loading", "streaming", "partial", "blocking", "error", "refusal", "success",
]


def _drive_state(view: LiveRunView, console: Console, state: str) -> None:
    if state == "empty":
        view.summary(_summary(episode_count=12, by_outcome={"suppressed": 12}))
    elif state == "loading":
        view.banner(run_id="01J8RUNEXAMPLE", mode="dry_run", config_hash="a3f9e1cddeadbeef")
    elif state == "streaming":
        _run_streaming_episode(view)
    elif state == "partial":
        view.episode_start(_episode(episode_id="ep_018"))
        for name in ("duplicate", "terminal_seen", "opt_out", "episode_age"):
            view.guardrail(CheckResult(name, "pass", None))
        view.diagnosis(_diagnosis(method="llm", class_id="unknown", confidence=0.0,
                                   llm_degraded=True))
    elif state == "blocking":
        panel = render_approval_panel(
            _episode(episode_id="ep_018", amount_paise=680000),
            "C1_issuer_decline",
            "open_ticket",
            ["link_same_instrument", "open_ticket", "no_action"],
            "amount_paise > auto_approve_ceiling_paise",
            59.0,
            60.0,
        )
        console.print(panel)
    elif state == "error":
        view.episode_start(_episode(episode_id="ep_019"))
        view.retry(1, 1.0, 429)
        view.retry(2, 2.0, 429)
        view.retry_exhausted(3)
    elif state == "refusal":
        view.cluster_refusal("BANK_D_timeout", 40)
    elif state == "success":
        view.summary(_summary())
    else:
        raise AssertionError(f"unknown state {state!r}")


def test_all_eight_states_render_without_raising() -> None:
    console = _console()
    view = LiveRunView(console, total=25)
    for state in STATE_NAMES:
        _drive_state(view, console, state)  # must not raise


# ---------------------------------------------------------------------------
# 2. cluster refusal renders exactly one line for 40 episodes
# ---------------------------------------------------------------------------


def test_cluster_refusal_is_exactly_one_line_for_forty_episodes() -> None:
    console = _console()
    view = LiveRunView(console, total=200)
    view.cluster_refusal("BANK_D_timeout", 40)
    text = console.export_text()
    lines = [line for line in text.splitlines() if line.strip()]
    matching = [line for line in lines if "cluster:" in line]
    assert len(matching) == 1, f"expected exactly one cluster line, got: {matching}"
    assert "40 episodes" in matching[0]
    assert "hard_refuse" in matching[0]
    assert "shared_cause_cluster" in matching[0]


# ---------------------------------------------------------------------------
# 3. approval prompt times out at 60s and auto-rejects with the reason
#    recorded (freezegun-driven, no real 60s wait)
# ---------------------------------------------------------------------------


def test_approval_prompt_times_out_at_60s_and_auto_rejects() -> None:
    console = _console()
    view = LiveRunView(console, total=1)
    ep = _episode(episode_id="ep_018", amount_paise=680000)

    with freeze_time("2026-08-30T14:05:00+05:30") as frozen:

        def fake_sleep(seconds: float) -> None:
            frozen.tick(timedelta(seconds=seconds))

        result = view.approval_prompt(
            ep,
            "C1_issuer_decline",
            "open_ticket",
            ["link_same_instrument", "open_ticket", "no_action"],
            "amount_paise > auto_approve_ceiling_paise",
            timeout_s=60.0,
            key_reader=lambda: None,  # nobody ever presses a key
            sleep=fake_sleep,
            poll_interval_s=5.0,
        )

    assert result.decision == "approval_timeout"
    assert result.actor == "system"
    assert result.reason == "approval_timeout"
    assert result.elapsed_s == 60.0

    text = console.export_text()
    assert "approval_timeout" in text


# ---------------------------------------------------------------------------
# 4. no ANSI-only signalling: every state's plain text contains its label
# ---------------------------------------------------------------------------


def test_no_ansi_only_signalling_every_state_has_a_text_label() -> None:
    console = _console()
    view = LiveRunView(console, total=25)

    _drive_state(view, console, "empty")
    text = console.export_text()
    assert "0 eligible episodes" in text
    assert "12 suppressed" in text

    console = _console()
    view = LiveRunView(console, total=25)
    _drive_state(view, console, "streaming")
    text = console.export_text()
    # gate check status: glyph AND label, never colour/glyph alone
    assert "OK" in text
    assert "auto" in text  # tier label
    assert "created" in text  # execution status label

    console = _console()
    view = LiveRunView(console, total=25)
    _drive_state(view, console, "partial")
    text = console.export_text()
    assert "llm_degraded" in text
    assert "regex baseline used" in text

    console = _console()
    view = LiveRunView(console, total=25)
    _drive_state(view, console, "error")
    text = console.export_text()
    assert "attempt 1" in text
    assert "HTTP 429" in text
    assert "retry cap 3" in text
    assert "not retrying" in text

    console = _console()
    view = LiveRunView(console, total=25)
    _drive_state(view, console, "refusal")
    text = console.export_text()
    assert "hard_refuse" in text
    assert "shared_cause_cluster" in text

    console = _console()
    view = LiveRunView(console, total=25)
    _drive_state(view, console, "success")
    text = console.export_text()
    assert "RUN SUMMARY" in text
    assert "actioned" in text
    assert "22" in text
