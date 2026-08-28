from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.config_models import load_all
from src.diagnose.baseline import RegexBaseline
from src.diagnose.cache import DiskCache
from src.diagnose.classifier import UNKNOWN_CLASS_ID, Diagnoser, build_episode_context
from src.diagnose.llm_client import LLMResponse
from src.errors import LLMCallError
from src.gate.checks import Episode

REAL_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 8, 26, 12, 0, 0, tzinfo=IST)


class _FakeSettings:
    llm_model = "test-model"


def _taxonomy():
    return load_all(REAL_CONFIG_DIR).taxonomy


def _episode(
    episode_id: str,
    *,
    error_reason: str | None = "payment_failed",
    error_description: str | None = "Payment failed",
    amount_paise: int = 10000,
    instrument: str | None = "card",
) -> Episode:
    """Default fields reproduce the genuinely generic Razorpay envelope
    (evidence/harvested_errors.jsonl: 19/20 records) — no taxonomy regex
    pattern matches this text, so RegexBaseline.classify() returns None and
    every episode built with the defaults exercises the LLM path."""
    return Episode(
        episode_id=episode_id,
        payment_id=f"pay_{episode_id}",
        amount_paise=amount_paise,
        instrument=instrument,
        error_code="BAD_REQUEST_ERROR",
        error_description=error_description,
        error_reason=error_reason,
        error_source="gateway",
        error_step="payment_authorization",
        failed_at=NOW,
        received_at=NOW,
    )


def _valid_json(class_id: str = "C8") -> str:
    return json.dumps(
        {
            "class_id": class_id,
            "confidence": 0.4,
            "rationale": "generic gateway envelope, low-confidence guess",
            "features_used": ["error_reason", "error_description"],
        }
    )


class ScriptedLLMClient:
    """Returns each entry of `script` in order, one per complete() call.
    An entry that is an Exception instance is raised instead of returned."""

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
            text=item,
            model=self._model,
            prompt_hash="testhash",
            input_tokens=50,
            output_tokens=20,
            cost_paise=2,
            latency_ms=5,
            cache_hit=False,
        )


class RaisingLLMClient:
    """A client that fails the test if it is ever called."""

    def complete(
        self, prompt: str, *, max_tokens: int, temperature: float, json_schema: dict
    ) -> LLMResponse:
        raise AssertionError("LLM client should not have been called")


# ---------------------------------------------------------------------------
# 1. cache key stability across processes
# ---------------------------------------------------------------------------


def test_cache_key_stable_across_processes(tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    cache_a = DiskCache(cache_dir)
    cache_b = DiskCache(cache_dir)  # simulates a second process opening the same dir

    key_a = cache_a.key("gemini-2.5-flash", "some prompt text")
    key_b = cache_b.key("gemini-2.5-flash", "some prompt text")

    assert key_a == key_b
    assert len(key_a) == 64  # sha256 hex digest
    assert key_a != cache_a.key("gemini-2.5-flash", "different prompt text")
    assert key_a != cache_a.key("gpt-4o-mini", "some prompt text")


def test_cache_get_put_roundtrip(tmp_path: Path) -> None:
    cache = DiskCache(tmp_path / "cache")
    key = cache.key("test-model", "prompt")

    assert cache.get(key) is None

    payload = {
        "model": "test-model", "prompt_hash": "abc", "response": "{}",
        "usage": {}, "cached_at": "now",
    }
    cache.put(key, payload)

    assert cache.get(key) == payload


# ---------------------------------------------------------------------------
# 2. second run of the same 20 episodes makes zero LLM calls, 20/20 cache hits
# ---------------------------------------------------------------------------


def test_repeated_run_hits_cache_and_makes_zero_llm_calls(tmp_path: Path) -> None:
    taxonomy = _taxonomy()
    baseline = RegexBaseline(taxonomy)
    cache_dir = tmp_path / "cache"

    episodes = [_episode(f"epi_{i:03d}", amount_paise=1000 + i) for i in range(20)]

    warm_client = ScriptedLLMClient([_valid_json() for _ in range(20)])
    warm_cache = DiskCache(cache_dir)
    warm_diagnoser = Diagnoser(baseline, warm_client, warm_cache, taxonomy, _FakeSettings())
    first_run = [warm_diagnoser.diagnose(ep) for ep in episodes]

    assert warm_client.calls == 20
    assert all(d.method == "llm" and not d.cache_hit for d in first_run)

    cold_client = RaisingLLMClient()
    cold_cache = DiskCache(cache_dir)  # fresh instance, same directory on disk
    cold_diagnoser = Diagnoser(baseline, cold_client, cold_cache, taxonomy, _FakeSettings())
    second_run = [cold_diagnoser.diagnose(ep) for ep in episodes]

    assert all(d.cache_hit for d in second_run)
    assert sum(1 for d in second_run if d.cache_hit) == 20


# ---------------------------------------------------------------------------
# 3. malformed JSON -> one repair retry -> degradation, no crash
# ---------------------------------------------------------------------------


def test_malformed_json_repairs_once_then_degrades(tmp_path: Path) -> None:
    taxonomy = _taxonomy()
    baseline = RegexBaseline(taxonomy)
    client = ScriptedLLMClient(["not json at all", "still not json"])
    cache = DiskCache(tmp_path / "cache")
    diagnoser = Diagnoser(baseline, client, cache, taxonomy, _FakeSettings())

    diagnosis = diagnoser.diagnose(_episode("epi_bad_json"))

    assert client.calls == 2  # original attempt + exactly one repair retry
    assert diagnosis.llm_degraded is True
    assert diagnosis.class_id == UNKNOWN_CLASS_ID


# ---------------------------------------------------------------------------
# 4. a class_id outside the taxonomy is rejected, not stored as the diagnosis
# ---------------------------------------------------------------------------


def test_invented_class_id_is_rejected(tmp_path: Path) -> None:
    taxonomy = _taxonomy()
    baseline = RegexBaseline(taxonomy)
    invented = _valid_json(class_id="C99")  # C99 does not exist in config/taxonomy.yaml
    client = ScriptedLLMClient([invented, invented])
    cache = DiskCache(tmp_path / "cache")
    diagnoser = Diagnoser(baseline, client, cache, taxonomy, _FakeSettings())

    diagnosis = diagnoser.diagnose(_episode("epi_invented_class"))

    assert client.calls == 2
    assert diagnosis.class_id != "C99"
    assert diagnosis.class_id == UNKNOWN_CLASS_ID
    assert diagnosis.llm_degraded is True


# ---------------------------------------------------------------------------
# 5. LLM timeout -> degradation to baseline, run completes, no crash
# ---------------------------------------------------------------------------


def test_llm_timeout_degrades_without_retry(tmp_path: Path) -> None:
    taxonomy = _taxonomy()
    baseline = RegexBaseline(taxonomy)
    timeout_error = LLMCallError("Gemini request timed out after 20.0s", code="LLM_TIMEOUT")
    client = ScriptedLLMClient([timeout_error])
    cache = DiskCache(tmp_path / "cache")
    diagnoser = Diagnoser(baseline, client, cache, taxonomy, _FakeSettings())

    diagnosis = diagnoser.diagnose(_episode("epi_timeout"))

    assert client.calls == 1  # no retry on a network-level failure — see llm_client.py
    assert diagnosis.llm_degraded is True
    assert diagnosis.class_id == UNKNOWN_CLASS_ID


# ---------------------------------------------------------------------------
# 6. regex-only path makes zero LLM calls
# ---------------------------------------------------------------------------


def test_regex_match_makes_zero_llm_calls(tmp_path: Path) -> None:
    taxonomy = _taxonomy()
    baseline = RegexBaseline(taxonomy)
    client = RaisingLLMClient()
    cache = DiskCache(tmp_path / "cache")
    diagnoser = Diagnoser(baseline, client, cache, taxonomy, _FakeSettings())

    ep = _episode("epi_regex", error_reason="insufficient_fund", error_description="Payment failed")
    diagnosis = diagnoser.diagnose(ep)

    assert diagnosis.method == "regex"
    assert diagnosis.class_id == "C1"
    assert diagnosis.cost_paise == 0


# ---------------------------------------------------------------------------
# 7. token budget: an oversized episode context is truncated with a warning
# ---------------------------------------------------------------------------


class _RecordingLogger:
    def __init__(self) -> None:
        self.warnings: list[tuple] = []

    def warning(self, *args: object, **kwargs: object) -> None:
        self.warnings.append(args)


def test_oversized_context_is_truncated_with_a_logged_warning() -> None:
    logger = _RecordingLogger()
    ep = _episode("epi_huge", error_description="x" * 10_000)

    fields, truncated = build_episode_context(ep, budget_tokens=50, logger=logger)

    assert truncated is True
    assert len(fields["ERROR_DESCRIPTION"]) < 10_000
    assert fields["ERROR_DESCRIPTION"].endswith("...[truncated]")
    assert len(logger.warnings) == 1


def test_context_within_budget_is_not_truncated() -> None:
    logger = _RecordingLogger()
    ep = _episode("epi_small", error_description="Payment failed")

    fields, truncated = build_episode_context(ep, budget_tokens=1500, logger=logger)

    assert truncated is False
    assert fields["ERROR_DESCRIPTION"] == "Payment failed"
    assert logger.warnings == []
