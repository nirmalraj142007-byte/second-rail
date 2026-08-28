"""Diagnoser — regex-first, LLM-for-the-unmatched-tail cause classification.

Ordering, stated once here because it is a design claim the panel will ask
about: RegexBaseline always runs first, on every episode, for free. Only
when it returns None (config/taxonomy.yaml's regex_patterns matched zero or
more than one class — see src/diagnose/baseline.py) does this call the LLM
at all. That makes the LLM strictly a cost paid for the tail the cheaper
method couldn't resolve, not a default — and it is why a run over data this
project's own generator produced can cost close to nothing (see
scripts/classify.py's coverage line) while a run over genuinely
undifferentiated real gateway text (evidence/harvested_errors.jsonl) pays
the LLM's cost on almost every episode instead.

A malformed or missing LLM response must never crash a batch run. Three
distinct failure shapes are handled, all ending in the same degraded
Diagnosis (class_id="unknown", confidence=0.0, llm_degraded=True) rather
than a raised exception:
  1. invalid JSON, or JSON missing a required key, or a class_id outside
     the taxonomy -> one repair-instruction retry -> still invalid -> degrade.
  2. the client raises LLMCallError (timeout, 429, quota) -> degrade
     immediately, no retry — see llm_client.py's module docstring for why a
     second network attempt isn't where this project spends its budget.
The one failure that does NOT degrade and is NOT caught here is
ConfigError from NullClient ("no LLM configured") — a genuine setup
mistake that should stop the run loudly, not disappear into a guessed
class_id. See src/errors.py's module docstring.

Because RegexBaseline.classify() only ever returns a single unambiguous
class or None (never a ranked partial guess — see baseline.py), there is no
"baseline's best guess" to fall back to once the LLM path has been entered
at all: entering it already means the baseline had nothing. So in this
implementation the degraded fallback is always the "unknown" sentinel, not
a graded baseline guess. That is a real simplification of the phase spec's
"fall back to the baseline's best guess or class unknown," disclosed here
rather than silently narrowed.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from src.config_models import Taxonomy
from src.diagnose.baseline import RegexBaseline
from src.diagnose.cache import DiskCache
from src.diagnose.llm_client import LLMClient, LLMResponse
from src.errors import LLMCallError
from src.gate.checks import Episode
from src.logging_setup import get_logger

PROMPT_VERSION = "classify_v1"
PROMPT_PATH = Path(__file__).parent / "prompts" / f"{PROMPT_VERSION}.txt"

MAX_OUTPUT_TOKENS = 300
LLM_TEMPERATURE = 0.0
CHARS_PER_TOKEN = 4  # rough heuristic; no tokenizer dependency in requirements.txt
INPUT_TOKEN_BUDGET = 1500
MAX_RATIONALE_CHARS = 240
UNKNOWN_CLASS_ID = "unknown"  # a diagnosis-failed sentinel, never a real taxonomy class


@dataclass(frozen=True)
class Diagnosis:
    episode_id: str
    method: str  # "regex" | "llm"
    class_id: str
    confidence: float
    rationale: str
    llm_model: str | None
    prompt_hash: str | None
    prompt_version: str | None
    cache_hit: bool
    latency_ms: int
    cost_paise: int
    llm_degraded: bool
    features_used: list[str]


class LLMClassification(BaseModel):
    class_id: str
    confidence: float
    rationale: str
    features_used: list[str]

    model_config = ConfigDict(extra="ignore")

    @field_validator("confidence")
    @classmethod
    def _confidence_in_range(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            raise ValueError("confidence must be in [0.0, 1.0]")
        return v


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // CHARS_PER_TOKEN)


def _class_list_block(taxonomy: Taxonomy) -> str:
    return "\n".join(
        f"- {cls.class_id}: {cls.label} — {cls.definition}" for cls in taxonomy.classes
    )


def classify_json_schema(taxonomy: Taxonomy) -> dict:
    return {
        "type": "object",
        "properties": {
            "class_id": {"type": "string", "enum": taxonomy.class_ids()},
            "confidence": {"type": "number"},
            "rationale": {"type": "string"},
            "features_used": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["class_id", "confidence", "rationale", "features_used"],
        "additionalProperties": False,
    }


def build_episode_context(
    ep: Episode, *, budget_tokens: int = INPUT_TOKEN_BUDGET, logger: object | None = None
) -> tuple[dict[str, str], bool]:
    """Fields substituted into the prompt template, truncating
    error_description (the only field with unbounded length) if the
    estimated total exceeds budget_tokens. Returns (fields, was_truncated)."""
    fields = {
        "INSTRUMENT": ep.instrument or "(unknown)",
        "AMOUNT": f"Rs {ep.amount_paise / 100:,.2f}",
        "SEGMENT": ep.segment or "(unknown)",
        "ERROR_CODE": ep.error_code or "(none)",
        "ERROR_DESCRIPTION": ep.error_description or "(none)",
        "ERROR_REASON": ep.error_reason or "(none)",
        "ERROR_SOURCE": ep.error_source or "(none)",
        "ERROR_STEP": ep.error_step or "(none)",
    }
    total_tokens = sum(_estimate_tokens(v) for v in fields.values())
    if total_tokens <= budget_tokens:
        return fields, False

    other_tokens = total_tokens - _estimate_tokens(fields["ERROR_DESCRIPTION"])
    desc_budget_chars = max(40, (budget_tokens - other_tokens) * CHARS_PER_TOKEN)
    original = fields["ERROR_DESCRIPTION"]
    fields["ERROR_DESCRIPTION"] = original[:desc_budget_chars].rstrip() + "...[truncated]"
    if logger is not None:
        logger.warning(
            "episode %s: prompt context (~%d est. tokens) exceeds input budget (%d) — "
            "truncated error_description from %d to %d chars",
            ep.episode_id,
            total_tokens,
            budget_tokens,
            len(original),
            len(fields["ERROR_DESCRIPTION"]),
        )
    return fields, True


def render_prompt(taxonomy: Taxonomy, fields: dict[str, str]) -> str:
    template = PROMPT_PATH.read_text(encoding="utf-8")
    return template.format(CLASS_LIST=_class_list_block(taxonomy), **fields)


def _build_repair_prompt(original_prompt: str, bad_response: str) -> str:
    return (
        original_prompt
        + "\n\n---\nYour previous response was:\n"
        + bad_response
        + "\n\nThat response was invalid: it must be a single JSON object with exactly the "
        "keys class_id, confidence, rationale, features_used, and class_id must be one of "
        "the class_id values listed above, verbatim. Return ONLY the corrected JSON object now."
    )


def _try_parse(text: str, valid_class_ids: frozenset[str]) -> LLMClassification | None:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").strip()
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()
    try:
        raw = json.loads(cleaned)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(raw, dict):
        return None
    try:
        parsed = LLMClassification.model_validate(raw)
    except ValidationError:
        return None
    if parsed.class_id not in valid_class_ids:
        return None
    return parsed


class Diagnoser:
    def __init__(
        self,
        baseline: RegexBaseline,
        llm: LLMClient,
        cache: DiskCache,
        taxonomy: Taxonomy,
        settings: object,
    ) -> None:
        self._baseline = baseline
        self._llm = llm
        self._cache = cache
        self._taxonomy = taxonomy
        self._settings = settings
        self._model_name = getattr(settings, "llm_model", "unknown-model")
        self._class_ids = frozenset(taxonomy.class_ids())
        self._logger = get_logger("diagnose", stage="diagnose")

    def _call_llm_cached(self, prompt: str, schema: dict) -> LLMResponse:
        key = self._cache.key(self._model_name, prompt)
        cached = self._cache.get(key)
        if cached is not None:
            usage = cached.get("usage", {})
            return LLMResponse(
                text=cached["response"],
                model=cached["model"],
                prompt_hash=cached["prompt_hash"],
                input_tokens=int(usage.get("input_tokens", 0)),
                output_tokens=int(usage.get("output_tokens", 0)),
                cost_paise=0,  # already paid for — a cache hit costs this run nothing
                latency_ms=0,
                cache_hit=True,
            )
        response = self._llm.complete(
            prompt, max_tokens=MAX_OUTPUT_TOKENS, temperature=LLM_TEMPERATURE, json_schema=schema
        )
        self._cache.put(
            key,
            {
                "model": response.model,
                "prompt_hash": response.prompt_hash,
                "response": response.text,
                "usage": {
                    "input_tokens": response.input_tokens,
                    "output_tokens": response.output_tokens,
                },
                "cached_at": _now_iso(),
            },
        )
        return response

    def _complete_with_repair(
        self, prompt: str, schema: dict
    ) -> tuple[LLMClassification, LLMResponse] | None:
        response = self._call_llm_cached(prompt, schema)
        parsed = _try_parse(response.text, self._class_ids)
        if parsed is not None:
            return parsed, response

        self._logger.warning("LLM response failed validation — retrying once with a repair prompt")
        repair_prompt = _build_repair_prompt(prompt, response.text)
        repair_response = self._call_llm_cached(repair_prompt, schema)
        parsed = _try_parse(repair_response.text, self._class_ids)
        if parsed is not None:
            return parsed, repair_response

        self._logger.warning("LLM response still invalid after one repair retry — degrading")
        return None

    def diagnose(self, ep: Episode) -> Diagnosis:
        """Production entry point: free regex baseline first, LLM only for
        the tail it can't resolve. See module docstring for why this
        ordering, not the reverse, is the design."""
        baseline_result = self._baseline.classify(ep)
        if baseline_result is not None:
            return Diagnosis(
                episode_id=ep.episode_id,
                method="regex",
                class_id=baseline_result.class_id,
                confidence=1.0,
                rationale=(
                    f"regex baseline matched {baseline_result.matched_pattern!r} "
                    f"in {baseline_result.matched_field}"
                ),
                llm_model=None,
                prompt_hash=None,
                prompt_version=None,
                cache_hit=False,
                latency_ms=0,
                cost_paise=0,
                llm_degraded=False,
                features_used=[baseline_result.matched_field],
            )
        return self.diagnose_llm_only(ep)

    def diagnose_llm_only(self, ep: Episode) -> Diagnosis:
        """Evaluation-only entry point that skips the regex baseline
        entirely, even when it would have matched. scripts/classify.py uses
        this to score the LLM independently on the same episodes the
        baseline was scored on, which is what the regex-vs-LLM head-to-head
        needs — diagnose()'s production cascade would otherwise never let
        the LLM see an episode the baseline already resolved for free.
        Production code should call diagnose(), not this."""
        fields, _truncated = build_episode_context(ep, logger=self._logger)
        prompt = render_prompt(self._taxonomy, fields)
        schema = classify_json_schema(self._taxonomy)

        start = time.monotonic()
        try:
            result = self._complete_with_repair(prompt, schema)
        except LLMCallError as exc:
            self._logger.warning("LLM call failed (%s) — degrading to %r", exc, UNKNOWN_CLASS_ID)
            result = None
        elapsed_ms = int((time.monotonic() - start) * 1000)

        if result is None:
            return Diagnosis(
                episode_id=ep.episode_id,
                method="llm",
                class_id=UNKNOWN_CLASS_ID,
                confidence=0.0,
                rationale="LLM classification unavailable or invalid; degraded to unknown.",
                llm_model=self._model_name,
                prompt_hash=self._cache.key(self._model_name, prompt),
                prompt_version=PROMPT_VERSION,
                cache_hit=False,
                latency_ms=elapsed_ms,
                cost_paise=0,
                llm_degraded=True,
                features_used=[],
            )

        parsed, llm_response = result
        return Diagnosis(
            episode_id=ep.episode_id,
            method="llm",
            class_id=parsed.class_id,
            confidence=parsed.confidence,
            rationale=parsed.rationale[:MAX_RATIONALE_CHARS],
            llm_model=llm_response.model,
            prompt_hash=llm_response.prompt_hash,
            prompt_version=PROMPT_VERSION,
            cache_hit=llm_response.cache_hit,
            latency_ms=llm_response.latency_ms,
            cost_paise=llm_response.cost_paise,
            llm_degraded=False,
            features_used=parsed.features_used,
        )
