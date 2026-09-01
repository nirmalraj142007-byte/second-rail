"""ActionSelector — the one place in this codebase where a model picks from
a menu it did not write.

The design claim this module exists to make literally true and testable:
the LLM sees an admissible action set that src/choose/policy.py already
constrained to at most 3 entries, and it cannot expand that set. Two
mechanisms enforce this, both load-bearing:

  1. LLM_VISIBLE_FEATURES + render_prompt() — the prompt never contains a
     cap value, a threshold, a ceiling, a guardrail name, policy rule text,
     or the raw amount in rupees/paise. Only the admissible action ids and
     their one-line descriptions, the diagnosis class and confidence, and
     the small whitelisted feature set below. tests/test_choose.py asserts
     the rendered prompt contains none of the forbidden tokens; see
     docs/where-the-llm-is-not.md for the same list documented for a judge.
  2. select() validates the raw response against `match.admissible_actions`
     itself — never trusting the JSON-schema `enum` alone, since a stub or
     a non-compliant provider can return anything. A response that still
     isn't a verbatim admissible action id after one repair retry raises
     AdmissibilityError, which HALTS THE RUN (src/errors.py). This is
     deliberately asymmetric with src/diagnose/classifier.py, which
     degrades a bad response to "unknown" and keeps going: a wrong
     *diagnosis* is a normal, expected kind of being wrong; a model
     *choosing outside its box* is the one failure mode this project
     refuses to paper over. LLMCallError (the LLM was unreachable at all)
     is a different, non-adversarial failure and gets the graceful
     fallback_priority path instead — see select()'s docstring.

BUDGET NOTE: this is the second LLM call per episode (after
src/diagnose/classifier.py's classify call). CLAUDE.md's U-11 flags that
two calls per episode contradicts an earlier "~1 per episode" cost
assumption. This module resolves that by merging action selection and
copy drafting into ONE request: the response schema below returns both
`chosen_action` and `copy_customer_facing` from a single complete() call,
so the per-episode LLM call count stays at 2 (classify + select-and-draft)
rather than 3. prompts/copy_v1.txt exists as a separate, readable file for
exactly what the copy-drafting rules are, but its contents are spliced
into the one select_v1.txt request at render time (render_prompt()) — it
is never sent as its own API call.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from src.choose.policy import PolicyMatch
from src.diagnose.cache import DiskCache
from src.diagnose.classifier import Diagnosis
from src.diagnose.llm_client import LLMClient, LLMResponse
from src.errors import AdmissibilityError, LLMCallError
from src.gate.checks import Episode, GateContext
from src.logging_setup import get_logger

PROMPT_VERSION = "select_v1"
PROMPTS_DIR = Path(__file__).parent / "prompts"
SELECT_PROMPT_PATH = PROMPTS_DIR / f"{PROMPT_VERSION}.txt"
COPY_PROMPT_PATH = PROMPTS_DIR / "copy_v1.txt"
DEFAULT_COPY_TEMPLATES_PATH = Path("config/copy_templates.yaml")

# Same 1200-token lesson as src/diagnose/classifier.py's MAX_OUTPUT_TOKENS
# (see that module's comment): a provider whose thinking tokens count
# against the same output budget as the visible answer needs real headroom,
# not the phase spec's original 120-token figure for copy alone — that
# figure survives instead as MAX_COPY_CHARS below, a hard post-hoc
# truncation of the copy field specifically, independent of what the
# provider's own token accounting looked like.
MAX_OUTPUT_TOKENS = 1200
LLM_TEMPERATURE = 0.0
MAX_RATIONALE_CHARS = 240
MAX_COPY_CHARS = 480  # ~120 tokens at ~4 chars/token, matching the phase spec's cap

# The complete whitelist of episode-derived fields src/choose ever shows an
# LLM. Nothing else — no cap value, no threshold, no raw amount, no
# guardrail name — is ever substituted into a selection prompt. Enforced by
# tests/test_choose.py; documented for a judge in
# docs/where-the-llm-is-not.md.
LLM_VISIBLE_FEATURES: tuple[str, ...] = (
    "error_code",
    "amount_band",
    "segment",
    "instrument",
    "prior_contacts_7d",
    "hours_since_failure",
)

# One-line, non-money descriptions the prompt renders next to each
# admissible action id. Not a config file: these are prompt copy, not a
# money-adjacent threshold, so CLAUDE.md's config-not-code rule does not
# apply to them.
ACTION_DESCRIPTIONS: dict[str, str] = {
    "link_same_instrument": (
        "send a fresh payment link, nudging the customer to retry with the same "
        "instrument they already tried"
    ),
    "link_alt_instrument": (
        "send a fresh payment link, suggesting the customer try a different instrument"
    ),
    "defer_2h": "wait roughly two hours and re-offer, for causes likely to resolve on their own",
    "open_ticket": "hand this episode to a human support agent for manual follow-up",
    "no_action": "take no recovery action on this episode",
}


class LLMSelection(BaseModel):
    chosen_action: str
    features_used: list[str]
    rationale: str
    copy_customer_facing: str

    model_config = ConfigDict(extra="ignore")


@dataclass(frozen=True)
class Selection:
    episode_id: str
    chosen_action: str
    features_used: list[str]
    # Populated, never raised on, per RULE 4: a model naming a feature
    # outside LLM_VISIBLE_FEATURES is an interesting finding for the report,
    # not a crash — logged and recorded here.
    features_used_outside_whitelist: list[str]
    rationale: str
    customer_copy: str
    inside_admissible_set: bool
    llm_degraded: bool
    policy_rule_id: str
    llm_model: str | None
    prompt_hash: str | None
    prompt_version: str | None
    cache_hit: bool
    latency_ms: int
    cost_paise: int
    input_tokens: int
    output_tokens: int


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def load_copy_templates(path: Path = DEFAULT_COPY_TEMPLATES_PATH) -> dict[str, str]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {str(k): str(v) for k, v in raw.items()}


def _action_list_block(admissible_actions: list[str]) -> str:
    return "\n".join(
        f"- {action_id}: {ACTION_DESCRIPTIONS.get(action_id, action_id)}"
        for action_id in admissible_actions
    )


def build_selection_fields(
    ep: Episode, diagnosis: Diagnosis, match: PolicyMatch, ctx: GateContext | None
) -> dict[str, str]:
    """The only episode/diagnosis/match data that ever reaches a selection
    prompt. hours_since_failure is derived from ep.received_at -
    ep.failed_at, mirroring src/runner.py's own convention of treating each
    episode's own received_at as "now" for that episode (see runner.py's
    module docstring) — no wall-clock read here, so this stays
    deterministic given the episode alone. prior_contacts_7d needs the
    run's accumulated contact history (GateContext.prior_contacts_7d),
    which does not live on Episode itself; ctx is optional and defaults to
    0 contacts when not supplied (e.g. in isolated unit tests), a disclosed
    simplification of the phase spec's exact `select(self, ep, diagnosis,
    match)` signature — see ActionSelector.select()'s docstring."""
    hours_since_failure = (ep.received_at - ep.failed_at).total_seconds() / 3600.0
    prior_contacts_7d = 0
    if ctx is not None and ep.customer_id:
        prior_contacts_7d = ctx.prior_contacts_7d(ep.customer_id, before=ep.failed_at)
    return {
        "CLASS_ID": diagnosis.class_id,
        "CONFIDENCE": f"{diagnosis.confidence:.2f}",
        "ERROR_CODE": ep.error_code or "(none)",
        "AMOUNT_BAND": match.amount_band,
        "SEGMENT": ep.segment or "(unknown)",
        "INSTRUMENT": ep.instrument or "(unknown)",
        "PRIOR_CONTACTS_7D": str(prior_contacts_7d),
        "HOURS_SINCE_FAILURE": f"{hours_since_failure:.1f}",
    }


def render_prompt(admissible_actions: list[str], fields: dict[str, str]) -> str:
    template = SELECT_PROMPT_PATH.read_text(encoding="utf-8")
    copy_instructions = COPY_PROMPT_PATH.read_text(encoding="utf-8").rstrip("\n")
    return template.format(
        COPY_INSTRUCTIONS=copy_instructions,
        ACTION_LIST=_action_list_block(admissible_actions),
        **fields,
    )


def select_json_schema(admissible_actions: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {
            "chosen_action": {"type": "string", "enum": list(admissible_actions)},
            "features_used": {"type": "array", "items": {"type": "string"}},
            "rationale": {"type": "string"},
            "copy_customer_facing": {"type": "string"},
        },
        "required": ["chosen_action", "features_used", "rationale", "copy_customer_facing"],
        "additionalProperties": False,
    }


def _build_repair_prompt(
    original_prompt: str, bad_response: str, admissible_actions: list[str]
) -> str:
    return (
        original_prompt
        + "\n\n---\nYour previous response was:\n"
        + bad_response
        + "\n\nThat response was invalid: it must be a single JSON object with exactly the "
        "keys chosen_action, features_used, rationale, copy_customer_facing, and "
        f"chosen_action must be exactly one of {admissible_actions!r}, verbatim. "
        "Return ONLY the corrected JSON object now."
    )


def _try_parse(text: str, admissible_actions: frozenset[str]) -> LLMSelection | None:
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
        parsed = LLMSelection.model_validate(raw)
    except ValidationError:
        return None
    if parsed.chosen_action not in admissible_actions:
        return None
    return parsed


class ActionSelector:
    def __init__(self, llm: LLMClient, cache: DiskCache, settings: object) -> None:
        self._llm = llm
        self._cache = cache
        self._settings = settings
        self._model_name = getattr(settings, "llm_model", "unknown-model")
        self._copy_templates = load_copy_templates()
        self._logger = get_logger("choose", stage="choose")

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
                cost_paise=0,
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
        self, prompt: str, schema: dict, admissible_actions: list[str]
    ) -> tuple[LLMSelection | None, LLMResponse]:
        """Mirrors src/diagnose/classifier.py's own repair-once pattern.
        Always returns the last LLMResponse actually received so real spend
        on a failed repair attempt is never lost. Raises LLMCallError
        (uncaught here) only if the network call itself failed — select()
        routes that to the graceful fallback_priority path, distinct from
        the AdmissibilityError path below for a response that was received
        but still names something outside the admissible set."""
        admissible_set = frozenset(admissible_actions)
        response = self._call_llm_cached(prompt, schema)
        parsed = _try_parse(response.text, admissible_set)
        if parsed is not None:
            return parsed, response

        self._logger.warning(
            "selection response failed validation — retrying once with a repair prompt"
        )
        repair_prompt = _build_repair_prompt(prompt, response.text, admissible_actions)
        repair_response = self._call_llm_cached(repair_prompt, schema)
        parsed = _try_parse(repair_response.text, admissible_set)
        if parsed is not None:
            return parsed, repair_response

        self._logger.warning(
            "selection response still invalid after one repair retry — "
            "this will raise AdmissibilityError, not degrade"
        )
        return None, repair_response

    def _fallback_selection(self, ep: Episode, match: PolicyMatch) -> Selection:
        chosen = next(
            (a for a in match.fallback_priority if a in match.admissible_actions), None
        )
        if chosen is None:
            # Unreachable for any config that passed load_all() —
            # fallback_priority is validated to contain "no_action"
            # (config_models.py), and every admissible_actions set is
            # validated to contain it too. Kept as an explicit floor rather
            # than an assert so a hand-built PolicyMatch in a test can't
            # silently produce a Selection naming an action that isn't
            # actually admissible.
            chosen = "no_action"
        self._logger.warning(
            "episode %s: LLM unavailable, falling back to %r via fallback_priority",
            ep.episode_id,
            chosen,
        )
        return Selection(
            episode_id=ep.episode_id,
            chosen_action=chosen,
            features_used=[],
            features_used_outside_whitelist=[],
            rationale=(
                f"LLM unavailable; deterministic fallback_priority selected {chosen!r} "
                "from the admissible set."
            ),
            customer_copy=self._copy_templates.get(chosen, ""),
            inside_admissible_set=True,
            llm_degraded=True,
            policy_rule_id=match.policy_rule_id,
            llm_model=self._model_name,
            prompt_hash=None,
            prompt_version=PROMPT_VERSION,
            cache_hit=False,
            latency_ms=0,
            cost_paise=0,
            input_tokens=0,
            output_tokens=0,
        )

    def select(
        self,
        ep: Episode,
        diagnosis: Diagnosis,
        match: PolicyMatch,
        *,
        ctx: GateContext | None = None,
    ) -> Selection:
        """`ctx` is an addition beyond the phase spec's literal
        `select(self, ep, diagnosis, match)` signature: it is the only way
        to compute the real prior_contacts_7d feature (run-accumulated
        state that does not live on Episode — see
        build_selection_fields()'s docstring), and it is keyword-only with
        a default of None so every call site the spec describes still
        works unchanged. src/runner.py passes the same GateContext it
        already built for this episode's gate evaluation.

        Raises AdmissibilityError (halts the run) if the LLM responds but
        never names an admissible action id, even after one repair retry.
        Never raises for LLMCallError (network failure) — that degrades
        to the deterministic fallback_priority path instead, with
        llm_degraded=True and the run continuing. See this module's
        docstring for why those two failure modes are handled so
        differently.
        """
        fields = build_selection_fields(ep, diagnosis, match, ctx)
        prompt = render_prompt(match.admissible_actions, fields)
        schema = select_json_schema(match.admissible_actions)

        try:
            parsed, response = self._complete_with_repair(prompt, schema, match.admissible_actions)
        except LLMCallError as exc:
            self._logger.warning("LLM call failed (%s) — using deterministic fallback", exc)
            return self._fallback_selection(ep, match)

        if parsed is None:
            raise AdmissibilityError(
                f"episode {ep.episode_id}: LLM response named an action outside the "
                f"admissible set {match.admissible_actions!r}, even after one repair "
                f"retry (last raw response: {response.text[:200]!r})",
                remediation=(
                    "the model chose outside its pre-registered admissible set — "
                    "this halts the run by design, see src/choose/selector.py"
                ),
            )

        outside_whitelist = sorted(set(parsed.features_used) - set(LLM_VISIBLE_FEATURES))
        if outside_whitelist:
            self._logger.warning(
                "episode %s: model named feature(s) outside LLM_VISIBLE_FEATURES: %s",
                ep.episode_id,
                outside_whitelist,
            )

        return Selection(
            episode_id=ep.episode_id,
            chosen_action=parsed.chosen_action,
            features_used=list(parsed.features_used),
            features_used_outside_whitelist=outside_whitelist,
            rationale=parsed.rationale[:MAX_RATIONALE_CHARS],
            customer_copy=parsed.copy_customer_facing[:MAX_COPY_CHARS],
            inside_admissible_set=True,
            llm_degraded=False,
            policy_rule_id=match.policy_rule_id,
            llm_model=response.model,
            prompt_hash=response.prompt_hash,
            prompt_version=PROMPT_VERSION,
            cache_hit=response.cache_hit,
            latency_ms=response.latency_ms,
            cost_paise=response.cost_paise,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
        )
