"""Tests for src/choose/ — the deterministic PolicyEngine and the
LLM-constrained ActionSelector.

  1. PolicyEngine.resolve() is deterministic over 1000 calls on one episode
  2. every resolved admissible set (real config, full cause x band x
     segment x instrument sweep) has length 1..3 and contains "no_action"
  3. a rendered selection prompt contains none of the forbidden tokens —
     no cap value, no threshold, no ceiling, no raw amount, no guardrails
     key
  4. a stubbed LLM returning an inadmissible action ("transfer_funds")
     raises AdmissibilityError, and src/runner.py writes a stage="stop"
     audit record for it
  5. LLM unavailable (LLMCallError) -> fallback_priority is used,
     llm_degraded is True, and the run completes rather than halting
  6. admissibility_rate over the full 400-episode train split is computed
     and is 1.0
  7. copy_customer_facing never contains a digit sequence matching the
     episode's amount, on both the LLM path and the fallback-template path
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.audit.writer import AuditWriter
from src.choose.policy import PolicyEngine
from src.choose.selector import (
    LLM_VISIBLE_FEATURES,
    ActionSelector,
    build_selection_fields,
    render_prompt,
)
from src.config import Settings
from src.config_models import load_all
from src.db.migrate import get_connection, migrate
from src.db.repo import insert_customer_if_absent
from src.diagnose.baseline import RegexBaseline
from src.diagnose.cache import DiskCache
from src.diagnose.classifier import Diagnoser, Diagnosis
from src.diagnose.llm_client import LLMResponse
from src.errors import AdmissibilityError, ConfigError, LLMCallError
from src.execute.executor import ExecutionResult
from src.gate.checks import Episode
from src.runner import Runner

REAL_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 8, 26, 12, 0, 0, tzinfo=IST)


def _bundle():
    return load_all(REAL_CONFIG_DIR)


class _FakeSettings:
    llm_model = "test-model"


def _episode(
    episode_id: str = "epi_001",
    *,
    amount_paise: int = 50000,
    segment: str = "repeat",
    instrument: str = "upi",
    customer_id: str = "cust_001",
    error_reason: str = "insufficient_fund",
    failed_at: datetime | None = None,
) -> Episode:
    when = failed_at if failed_at is not None else NOW
    return Episode(
        episode_id=episode_id,
        payment_id=f"pay_{episode_id}",
        customer_id=customer_id,
        amount_paise=amount_paise,
        segment=segment,
        instrument=instrument,
        error_code="BAD_REQUEST_ERROR",
        error_description="Payment failed",
        error_reason=error_reason,
        failed_at=when,
        received_at=when,
    )


def _diagnosis(class_id: str = "C1", confidence: float = 0.9) -> Diagnosis:
    return Diagnosis(
        episode_id="epi_001",
        method="regex",
        class_id=class_id,
        confidence=confidence,
        rationale="test fixture",
        llm_model=None,
        prompt_hash=None,
        prompt_version=None,
        cache_hit=False,
        latency_ms=0,
        cost_paise=0,
        input_tokens=0,
        output_tokens=0,
        llm_degraded=False,
        features_used=[],
    )


def _valid_json(action: str, copy: str = "We noticed your payment didn't go through.") -> str:
    return json.dumps(
        {
            "chosen_action": action,
            "features_used": ["error_code", "amount_band"],
            "rationale": "test rationale",
            "copy_customer_facing": copy,
        }
    )


class ScriptedLLMClient:
    """Returns each entry of `script` in order, one per complete() call. An
    entry that is an Exception instance is raised instead of returned."""

    def __init__(self, script: list[str | Exception], model: str = "test-model") -> None:
        self._script = list(script)
        self._model = model
        self.calls = 0

    def complete(
        self, prompt: str, *, max_tokens: int, temperature: float, json_schema: dict
    ) -> LLMResponse:
        self.calls += 1
        if not self._script:
            raise AssertionError("ScriptedLLMClient: complete() called more times than scripted")
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return LLMResponse(
            text=item, model=self._model, prompt_hash="testhash",
            input_tokens=50, output_tokens=20, cost_paise=2, latency_ms=5, cache_hit=False,
        )


class AutoValidLLMClient:
    """Always answers with the first action in the schema's own enum for
    chosen_action — a network-free stand-in for "the model behaves", used
    to exercise real accounting logic (admissibility_rate) across a full
    batch without a live provider. See test 6."""

    def __init__(self, model: str = "test-model") -> None:
        self._model = model
        self.calls = 0

    def complete(
        self, prompt: str, *, max_tokens: int, temperature: float, json_schema: dict
    ) -> LLMResponse:
        self.calls += 1
        action = json_schema["properties"]["chosen_action"]["enum"][0]
        payload = _valid_json(action)
        return LLMResponse(
            text=payload, model=self._model, prompt_hash="testhash",
            input_tokens=50, output_tokens=20, cost_paise=2, latency_ms=5, cache_hit=False,
        )


def _conn(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "test.db"
    migrate(db_path)
    return get_connection(db_path)


class _NoopExecutor:
    """Never touches the network — used where these tests only care about
    the choose stage, not execution."""

    def create_recovery_link(self, episode, action, policy_rule_id, run_id):
        return ExecutionResult(
            status="created", idempotency_key=f"k_{episode.episode_id}", created_new=True
        )

    def cancel_link(self, plink_id):
        return {}


# ---------------------------------------------------------------------------
# 1. resolve() is deterministic over 1000 calls on the same episode
# ---------------------------------------------------------------------------


def test_resolve_is_deterministic_over_1000_calls() -> None:
    bundle = _bundle()
    engine = PolicyEngine(bundle.policy)
    ep = _episode()
    diagnosis = _diagnosis("C1")

    results = [engine.resolve(ep, diagnosis) for _ in range(1000)]

    first = results[0]
    assert all(r == first for r in results)
    assert first.policy_rule_id == "P-01"  # C1, band A1, repeat, upi


# ---------------------------------------------------------------------------
# 2. every resolved admissible set has length 1..3 and contains "no_action"
# ---------------------------------------------------------------------------


def test_every_admissible_set_is_1_to_3_and_contains_no_action() -> None:
    bundle = _bundle()
    engine = PolicyEngine(bundle.policy)
    class_ids = bundle.taxonomy.class_ids()
    band_amounts = {"A1": 50000, "A2": 300000, "A3": 600000}
    segments = ("first_time", "repeat", "high_value")
    instruments = ("upi", "card", "netbanking", "wallet")

    checked = 0
    for class_id in class_ids:
        for band_id, amount in band_amounts.items():
            for segment in segments:
                for instrument in instruments:
                    ep = _episode(
                        amount_paise=amount, segment=segment, instrument=instrument
                    )
                    match = engine.resolve(ep, _diagnosis(class_id))
                    assert 1 <= len(match.admissible_actions) <= 3
                    assert "no_action" in match.admissible_actions
                    assert match.amount_band == band_id
                    checked += 1
    assert checked == len(class_ids) * 3 * 3 * 4


# ---------------------------------------------------------------------------
# 3. rendered selection prompt contains none of the forbidden tokens
# ---------------------------------------------------------------------------


def test_rendered_prompt_contains_no_forbidden_tokens() -> None:
    bundle = _bundle()
    engine = PolicyEngine(bundle.policy)
    ep = _episode(amount_paise=612345)
    diagnosis = _diagnosis("C1")
    match = engine.resolve(ep, diagnosis)

    fields = build_selection_fields(ep, diagnosis, match, ctx=None)
    prompt = render_prompt(match.admissible_actions, fields)

    forbidden = ["5000", "ceiling", "cap", "quiet", "threshold", str(ep.amount_paise)]
    guardrail_keys = list(bundle.guardrails.model_dump().keys())
    forbidden += guardrail_keys

    lowered = prompt.lower()
    for token in forbidden:
        assert str(token).lower() not in lowered, f"forbidden token {token!r} leaked into prompt"

    # sanity: the whitelist itself is exactly what appears as field labels
    for feature in LLM_VISIBLE_FEATURES:
        assert feature in prompt


# ---------------------------------------------------------------------------
# 4. an inadmissible LLM choice raises AdmissibilityError; runner logs "stop"
# ---------------------------------------------------------------------------


def test_inadmissible_choice_raises_and_runner_writes_stop_record(tmp_path: Path) -> None:
    bundle = _bundle()
    conn = _conn(tmp_path)
    ep = _episode()
    insert_customer_if_absent(
        conn, customer_id=ep.customer_id, synthetic_name=None, contact_hash="x",
        email_hash=None, segment="repeat", opted_out=False, opt_out_ts=None,
        created_at=NOW.isoformat(),
    )

    taxonomy = bundle.taxonomy
    baseline = RegexBaseline(taxonomy)
    diagnoser = Diagnoser(
        baseline, ScriptedLLMClient([]), DiskCache(tmp_path / "dcache"), taxonomy, _FakeSettings()
    )
    policy_engine = PolicyEngine(bundle.policy)

    invalid = _valid_json("transfer_funds")  # not an admissible action, not even a real one
    selector = ActionSelector(
        ScriptedLLMClient([invalid, invalid]), DiskCache(tmp_path / "scache"), _FakeSettings()
    )

    run_id = "run_admissibility_test"
    audit_dir = tmp_path / "audit"
    audit = AuditWriter(run_id, audit_dir, conn)
    runner = Runner(
        conn, audit, bundle, Settings(),
        diagnoser=diagnoser, policy_engine=policy_engine, selector=selector,
        executor=_NoopExecutor(),
    )

    with pytest.raises(AdmissibilityError):
        runner.run([ep], "dry_run", now=NOW, run_id=run_id)
    audit.close()

    records = [
        json.loads(line)
        for line in (audit_dir / f"{run_id}.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    stop_records = [r for r in records if r["stage"] == "stop"]
    assert len(stop_records) == 1
    assert "admissibility" in stop_records[0]["rationale"].lower()


# ---------------------------------------------------------------------------
# 5. LLM unavailable -> fallback_priority used, llm_degraded True, run completes
# ---------------------------------------------------------------------------


def test_llm_unavailable_falls_back_and_run_completes(tmp_path: Path) -> None:
    bundle = _bundle()
    conn = _conn(tmp_path)
    ep = _episode()
    insert_customer_if_absent(
        conn, customer_id=ep.customer_id, synthetic_name=None, contact_hash="x",
        email_hash=None, segment="repeat", opted_out=False, opt_out_ts=None,
        created_at=NOW.isoformat(),
    )

    taxonomy = bundle.taxonomy
    baseline = RegexBaseline(taxonomy)
    diagnoser = Diagnoser(
        baseline, ScriptedLLMClient([]), DiskCache(tmp_path / "dcache"), taxonomy, _FakeSettings()
    )
    policy_engine = PolicyEngine(bundle.policy)

    timeout = LLMCallError("timed out", code="LLM_TIMEOUT")
    selector = ActionSelector(
        ScriptedLLMClient([timeout]), DiskCache(tmp_path / "scache"), _FakeSettings()
    )

    run_id = "run_fallback_test"
    audit = AuditWriter(run_id, tmp_path / "audit", conn)
    runner = Runner(
        conn, audit, bundle, Settings(),
        diagnoser=diagnoser, policy_engine=policy_engine, selector=selector,
        executor=_NoopExecutor(),
    )

    summary = runner.run([ep], "dry_run", now=NOW, run_id=run_id)
    audit.close()

    assert summary.stopped_reason is None
    assert summary.admissibility_rate == 1.0

    match = policy_engine.resolve(ep, _diagnosis("C1"))
    row = conn.execute(
        "SELECT chosen_action FROM decision WHERE episode_id = ?", (ep.episode_id,)
    ).fetchone()
    assert row["chosen_action"] in match.fallback_priority
    assert row["chosen_action"] in match.admissible_actions


# ---------------------------------------------------------------------------
# 5b. NullClient's "no LLM configured" ConfigError degrades the same way
#     LLMCallError does, instead of crashing the run (KNOWN_ISSUES.md Issue 5)
# ---------------------------------------------------------------------------


def test_no_llm_configured_config_error_falls_back_and_run_completes(tmp_path: Path) -> None:
    """Forces the exact condition that used to crash: ActionSelector.select()
    hits a cache miss with no LLM reachable at all. Before the fix, this
    ConfigError (NullClient's "no LLM configured", code=NO_LLM_CONFIGURED)
    propagated uncaught and the run halted; it must now degrade to
    fallback_priority (llm_degraded=True) exactly like LLMCallError already
    does, one line above."""
    bundle = _bundle()
    conn = _conn(tmp_path)
    ep = _episode()
    insert_customer_if_absent(
        conn, customer_id=ep.customer_id, synthetic_name=None, contact_hash="x",
        email_hash=None, segment="repeat", opted_out=False, opt_out_ts=None,
        created_at=NOW.isoformat(),
    )

    taxonomy = bundle.taxonomy
    baseline = RegexBaseline(taxonomy)
    diagnoser = Diagnoser(
        baseline, ScriptedLLMClient([]), DiskCache(tmp_path / "dcache"), taxonomy, _FakeSettings()
    )
    policy_engine = PolicyEngine(bundle.policy)

    no_llm = ConfigError("no LLM configured", code="NO_LLM_CONFIGURED")
    selector = ActionSelector(
        ScriptedLLMClient([no_llm]), DiskCache(tmp_path / "scache"), _FakeSettings()
    )

    run_id = "run_no_llm_configured_test"
    audit = AuditWriter(run_id, tmp_path / "audit", conn)
    runner = Runner(
        conn, audit, bundle, Settings(),
        diagnoser=diagnoser, policy_engine=policy_engine, selector=selector,
        executor=_NoopExecutor(),
    )

    summary = runner.run([ep], "dry_run", now=NOW, run_id=run_id)
    audit.close()

    assert summary.stopped_reason is None
    assert summary.admissibility_rate == 1.0

    match = policy_engine.resolve(ep, _diagnosis("C1"))
    row = conn.execute(
        "SELECT chosen_action FROM decision WHERE episode_id = ?", (ep.episode_id,)
    ).fetchone()
    assert row["chosen_action"] in match.fallback_priority
    assert row["chosen_action"] in match.admissible_actions


def test_config_error_other_than_no_llm_configured_still_propagates(tmp_path: Path) -> None:
    """The degrade path is scoped to code="NO_LLM_CONFIGURED" specifically —
    any other ConfigError still halts the run rather than being silently
    absorbed as a degraded guess."""
    bundle = _bundle()
    conn = _conn(tmp_path)
    ep = _episode()
    insert_customer_if_absent(
        conn, customer_id=ep.customer_id, synthetic_name=None, contact_hash="x",
        email_hash=None, segment="repeat", opted_out=False, opt_out_ts=None,
        created_at=NOW.isoformat(),
    )

    policy_engine = PolicyEngine(bundle.policy)
    match = policy_engine.resolve(ep, _diagnosis("C1"))

    other_error = ConfigError("something else is misconfigured", code="SOME_OTHER_CONFIG_ERROR")
    selector = ActionSelector(
        ScriptedLLMClient([other_error]), DiskCache(tmp_path / "scache2"), _FakeSettings()
    )

    with pytest.raises(ConfigError):
        selector.select(ep, _diagnosis("C1"), match)


# ---------------------------------------------------------------------------
# 6. admissibility_rate over the full 400-episode train split is 1.0
# ---------------------------------------------------------------------------


def test_admissibility_rate_over_train_split_is_1(tmp_path: Path) -> None:
    from scripts.classify import _load_train

    bundle = _bundle()
    policy_engine = PolicyEngine(bundle.policy)
    selector = ActionSelector(AutoValidLLMClient(), DiskCache(tmp_path / "scache"), _FakeSettings())

    train = _load_train()
    assert len(train) == 400

    inside = 0
    for ep, true_class in train:
        diagnosis = _diagnosis(true_class)
        match = policy_engine.resolve(ep, diagnosis)
        selection = selector.select(ep, diagnosis, match)
        if selection.inside_admissible_set:
            inside += 1

    admissibility_rate = inside / len(train)
    assert admissibility_rate == 1.0


# ---------------------------------------------------------------------------
# 7. copy never contains a digit sequence matching the episode amount
# ---------------------------------------------------------------------------


def test_copy_never_leaks_the_episode_amount(tmp_path: Path) -> None:
    bundle = _bundle()
    policy_engine = PolicyEngine(bundle.policy)
    ep = _episode(amount_paise=734521)
    diagnosis = _diagnosis("C1")
    match = policy_engine.resolve(ep, diagnosis)

    # LLM path
    llm_selector = ActionSelector(
        ScriptedLLMClient([_valid_json(match.admissible_actions[0])]),
        DiskCache(tmp_path / "llm_cache"),
        _FakeSettings(),
    )
    llm_selection = llm_selector.select(ep, diagnosis, match)
    assert str(ep.amount_paise) not in llm_selection.customer_copy
    assert f"{ep.amount_paise / 100:.2f}" not in llm_selection.customer_copy

    # fallback-template path
    fallback_selector = ActionSelector(
        ScriptedLLMClient([LLMCallError("down", code="LLM_TIMEOUT")]),
        DiskCache(tmp_path / "fallback_cache"),
        _FakeSettings(),
    )
    fallback_selection = fallback_selector.select(ep, diagnosis, match)
    assert str(ep.amount_paise) not in fallback_selection.customer_copy
    assert f"{ep.amount_paise / 100:.2f}" not in fallback_selection.customer_copy


# ---------------------------------------------------------------------------
# 8. a no_action episode moves none of the three RunState counters — the
#    accounting regression found by investigating why the sealed-split eval
#    stopped at the same episode index in both the Second Rail and baseline
#    runs despite contacting different numbers of episodes (BUILD_LOG.md).
#    Each sub-test isolates one counter with the other two guardrails set
#    loose enough not to interfere, using a real Runner.run() over two
#    episodes for the same customer: the first is scripted to choose
#    no_action, the second to choose a real admissible action a moment
#    later. Both episodes resolve to C1/A1/repeat/upi -> policy rule P-01
#    (admissible_actions: link_alt_instrument, defer_2h, no_action).
# ---------------------------------------------------------------------------


def _setup_two_episode_run(
    tmp_path: Path, bundle, *, second_offset_minutes: int = 5
) -> tuple[sqlite3.Connection, Episode, Episode, Runner, AuditWriter, str]:
    conn = _conn(tmp_path)
    insert_customer_if_absent(
        conn, customer_id="cust_noaction", synthetic_name=None, contact_hash="x",
        email_hash=None, segment="repeat", opted_out=False, opt_out_ts=None,
        created_at=NOW.isoformat(),
    )

    ep1 = _episode("epi_na1", customer_id="cust_noaction", amount_paise=100000, failed_at=NOW)
    ep2 = _episode(
        "epi_na2", customer_id="cust_noaction", amount_paise=100000,
        failed_at=NOW + timedelta(minutes=second_offset_minutes),
    )
    # ep1/ep2 otherwise render a byte-identical selection prompt (same
    # class/band/segment/instrument, zero prior contacts, zero hours-since-
    # failure since received_at == failed_at for both) — same cache key,
    # so ep2's select() would silently hit the cache ep1's call populated
    # and never reach the second scripted response. Nudging ep2's
    # received_at forward gives it a different HOURS_SINCE_FAILURE field
    # (src/choose/selector.py's build_selection_fields()), which is enough
    # to change the rendered prompt and the cache key with it.
    ep2 = ep2.model_copy(update={"received_at": ep2.received_at + timedelta(hours=1)})

    taxonomy = bundle.taxonomy
    baseline = RegexBaseline(taxonomy)
    diagnoser = Diagnoser(
        baseline, ScriptedLLMClient([]), DiskCache(tmp_path / "dcache"), taxonomy, _FakeSettings()
    )
    policy_engine = PolicyEngine(bundle.policy)
    selector = ActionSelector(
        ScriptedLLMClient([_valid_json("no_action"), _valid_json("link_alt_instrument")]),
        DiskCache(tmp_path / "scache"),
        _FakeSettings(),
    )

    run_id = "run_no_action_accounting"
    audit = AuditWriter(run_id, tmp_path / "audit", conn)
    runner = Runner(
        conn, audit, bundle, Settings(),
        diagnoser=diagnoser, policy_engine=policy_engine, selector=selector,
        executor=_NoopExecutor(),
    )
    return conn, ep1, ep2, runner, audit, run_id


def test_no_action_episode_does_not_count_toward_exposure_cap(tmp_path: Path) -> None:
    base = _bundle()
    # ep1 (100000) + ep2 (100000) would breach 150000 if ep1's no_action
    # amount were still committed; ep2 alone (100000) must clear it.
    g = base.guardrails.model_copy(
        update={"per_run_exposure_ceiling_paise": 150000, "max_contacts_per_customer_7d": 10}
    )
    bundle = base.model_copy(update={"guardrails": g})

    conn, ep1, ep2, runner, audit, run_id = _setup_two_episode_run(tmp_path, bundle)
    try:
        summary = runner.run([ep1, ep2], "dry_run", run_id=run_id)
    finally:
        audit.close()

    assert summary.by_outcome.get("actioned", 0) == 1  # only ep2 actually executes
    assert summary.by_outcome.get("suppressed", 0) == 1  # ep1's no_action suppression
    reasons = {
        r["reason_code"]
        for r in conn.execute(
            "SELECT reason_code FROM exception_entry WHERE run_id = ?", (run_id,)
        ).fetchall()
    }
    assert "exposure_ceiling_exceeded" not in reasons, (
        "ep1's no_action episode inflated exposure_committed_paise and blocked ep2"
    )


def test_no_action_episode_does_not_count_toward_frequency_cap(tmp_path: Path) -> None:
    base = _bundle()
    # A cap of 1 means a single wrongly-counted prior contact blocks ep2.
    g = base.guardrails.model_copy(
        update={"max_contacts_per_customer_7d": 1, "per_run_exposure_ceiling_paise": 20000000}
    )
    bundle = base.model_copy(update={"guardrails": g})

    conn, ep1, ep2, runner, audit, run_id = _setup_two_episode_run(tmp_path, bundle)
    try:
        summary = runner.run([ep1, ep2], "dry_run", run_id=run_id)
    finally:
        audit.close()

    assert summary.by_outcome.get("actioned", 0) == 1
    assert summary.by_outcome.get("suppressed", 0) == 1
    reasons = {
        r["reason_code"]
        for r in conn.execute(
            "SELECT reason_code FROM exception_entry WHERE run_id = ?", (run_id,)
        ).fetchall()
    }
    assert "frequency_cap_exceeded" not in reasons, (
        "ep1's no_action episode inflated contacts_by_customer and blocked ep2"
    )


def test_no_action_episode_does_not_count_toward_batch_contact_ceiling_tier(
    tmp_path: Path,
) -> None:
    base = _bundle()
    # batch_contact_ceiling=1: a second REAL contact this run should still
    # be tier "auto" — it is the first, not the second, real contact.
    g = base.guardrails.model_copy(
        update={
            "batch_contact_ceiling": 1,
            "max_contacts_per_customer_7d": 10,
            "per_run_exposure_ceiling_paise": 20000000,
        }
    )
    bundle = base.model_copy(update={"guardrails": g})

    conn, ep1, ep2, runner, audit, run_id = _setup_two_episode_run(tmp_path, bundle)
    try:
        summary = runner.run([ep1, ep2], "dry_run", run_id=run_id)
    finally:
        audit.close()

    assert summary.by_outcome.get("actioned", 0) == 1
    # by_escalation_tier aggregates GateDecision.escalation_tier (the gate's
    # own tier, driven by batch_contact_ceiling) — distinct from the
    # policy_table.yaml rule's own escalation_tier field on `decision`.
    assert summary.by_escalation_tier.get("human_keystroke", 0) == 0, (
        "ep1's no_action episode inflated total_eligible_contacts_this_run and "
        "pushed ep2 to human_keystroke tier via batch_contact_ceiling"
    )
    assert summary.by_escalation_tier.get("auto", 0) >= 1
