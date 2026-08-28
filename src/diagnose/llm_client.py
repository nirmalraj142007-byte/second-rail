"""LLMClient — the one interface every LLM provider call in this codebase
goes through, plus the per-token cost model.

Raw httpx, not a vendor SDK, for the same reason src/razorpay_client.py
isn't the razorpay SDK: the audit record needs the real status code and
latency in hand, and a caching layer above this needs a stable prompt
string to hash — a vendor SDK's own retry/wrapping logic would hide both.

Every implementation makes exactly one HTTP attempt per complete() call and
raises LLMCallError on any failure (timeout, non-2xx, malformed envelope).
There is deliberately no retry loop here: src/diagnose/classifier.py's
degradation path (fall back to the baseline's guess, set llm_degraded=True,
keep the run going) is what a real LLM failure gets, not a second attempt —
this project's proposal is explicit that a wrong retried LLM call is not
where the value of retrying lives (contrast src/execute/retry.py, which
retries a Payment Link creation because a 429 there is a normal, expected,
worth-retrying condition; a 429 from an LLM provider mid-batch is not worth
spending the batch's time budget on when a cheap, correct fallback exists).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol

import httpx
import yaml
from pydantic import BaseModel

from src.errors import ConfigError, LLMCallError

DEFAULT_PRICING_PATH = Path("config/llm_pricing.yaml")
REQUEST_TIMEOUT_SECONDS = 20.0

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"
OPENAI_CHAT_COMPLETIONS_URL = "https://api.openai.com/v1/chat/completions"


# ---------------------------------------------------------------------------
# pricing
# ---------------------------------------------------------------------------


class ModelPricing(BaseModel):
    input_paise_per_1k: float
    output_paise_per_1k: float


class PricingTable(BaseModel):
    usd_to_inr: float
    models: dict[str, ModelPricing]


def load_pricing(path: Path = DEFAULT_PRICING_PATH) -> PricingTable:
    if not path.exists():
        raise ConfigError(
            f"{path}: file not found",
            code="CONFIG_FILE_MISSING",
            remediation=f"create {path}",
        )
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return PricingTable.model_validate(raw)


def compute_cost_paise(
    pricing: PricingTable, model: str, input_tokens: int, output_tokens: int
) -> int:
    entry = pricing.models.get(model)
    if entry is None:
        raise ConfigError(
            f"no pricing entry for model {model!r} in {DEFAULT_PRICING_PATH}",
            code="MISSING_MODEL_PRICING",
            remediation=f"add a {model!r} entry under models: in {DEFAULT_PRICING_PATH}",
        )
    paise = (input_tokens / 1000) * entry.input_paise_per_1k
    paise += (output_tokens / 1000) * entry.output_paise_per_1k
    return round(paise)


def hash_prompt(model: str, prompt: str) -> str:
    return sha256(f"{model}:{prompt}".encode()).hexdigest()


# ---------------------------------------------------------------------------
# response + protocol
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LLMResponse:
    text: str
    model: str
    prompt_hash: str
    input_tokens: int
    output_tokens: int
    cost_paise: int
    latency_ms: int
    cache_hit: bool


class LLMClient(Protocol):
    def complete(
        self, prompt: str, *, max_tokens: int, temperature: float, json_schema: dict
    ) -> LLMResponse: ...


# ---------------------------------------------------------------------------
# implementations
# ---------------------------------------------------------------------------


class NullClient:
    """The `llm_provider: none` implementation — every real Second Rail
    deployment with no key configured resolves to this. Raises immediately
    and loudly: "no LLM configured" is a setup mistake, not a transient
    failure, and must never be silently absorbed into a degraded guess (see
    src/errors.py's module docstring)."""

    def complete(
        self, prompt: str, *, max_tokens: int, temperature: float, json_schema: dict
    ) -> LLMResponse:
        raise ConfigError(
            "no LLM configured",
            code="NO_LLM_CONFIGURED",
            remediation=(
                "set LLM_PROVIDER=gemini or LLM_PROVIDER=openai and LLM_API_KEY in .env, "
                "or ensure every episode is resolved by the regex baseline"
            ),
        )


class GeminiClient:
    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        pricing: PricingTable | None = None,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._pricing = pricing or load_pricing()
        self._client = httpx.Client(timeout=timeout, transport=transport)

    def close(self) -> None:
        self._client.close()

    def complete(
        self, prompt: str, *, max_tokens: int, temperature: float, json_schema: dict
    ) -> LLMResponse:
        url = f"{GEMINI_BASE_URL}/{self._model}:generateContent"
        body = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "maxOutputTokens": max_tokens,
                "temperature": temperature,
                "responseMimeType": "application/json",
                "responseSchema": json_schema,
            },
        }
        start = time.monotonic()
        try:
            response = self._client.post(url, params={"key": self._api_key}, json=body)
        except httpx.TimeoutException as exc:
            raise LLMCallError(
                f"Gemini request timed out after {self._client.timeout}s", code="LLM_TIMEOUT"
            ) from exc
        except httpx.TransportError as exc:
            raise LLMCallError(f"Gemini request failed: {exc}", code="LLM_TRANSPORT_ERROR") from exc
        latency_ms = int((time.monotonic() - start) * 1000)

        if response.status_code == 429:
            raise LLMCallError("Gemini rate limit (HTTP 429)", code="LLM_RATE_LIMITED")
        if response.status_code == 403:
            raise LLMCallError(
                "Gemini quota/permission error (HTTP 403)", code="LLM_QUOTA_EXCEEDED"
            )
        if response.status_code >= 400:
            raise LLMCallError(
                f"Gemini returned HTTP {response.status_code}: {response.text[:300]}",
                code=f"LLM_HTTP_{response.status_code}",
            )

        try:
            payload = response.json()
            text = payload["candidates"][0]["content"]["parts"][0]["text"]
            usage = payload.get("usageMetadata", {})
            input_tokens = int(usage.get("promptTokenCount", 0))
            output_tokens = int(usage.get("candidatesTokenCount", 0))
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMCallError(
                f"Gemini response missing expected fields: {exc}", code="LLM_MALFORMED_ENVELOPE"
            ) from exc

        cost_paise = compute_cost_paise(self._pricing, self._model, input_tokens, output_tokens)
        return LLMResponse(
            text=text,
            model=self._model,
            prompt_hash=hash_prompt(self._model, prompt),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_paise=cost_paise,
            latency_ms=latency_ms,
            cache_hit=False,
        )

    def __enter__(self) -> GeminiClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class OpenAIClient:
    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        pricing: PricingTable | None = None,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._pricing = pricing or load_pricing()
        self._client = httpx.Client(timeout=timeout, transport=transport)

    def close(self) -> None:
        self._client.close()

    def complete(
        self, prompt: str, *, max_tokens: int, temperature: float, json_schema: dict
    ) -> LLMResponse:
        body = {
            "model": self._model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "cause_classification",
                    "strict": True,
                    "schema": json_schema,
                },
            },
        }
        headers = {"Authorization": f"Bearer {self._api_key}"}
        start = time.monotonic()
        try:
            response = self._client.post(OPENAI_CHAT_COMPLETIONS_URL, headers=headers, json=body)
        except httpx.TimeoutException as exc:
            raise LLMCallError(
                f"OpenAI request timed out after {self._client.timeout}s", code="LLM_TIMEOUT"
            ) from exc
        except httpx.TransportError as exc:
            raise LLMCallError(f"OpenAI request failed: {exc}", code="LLM_TRANSPORT_ERROR") from exc
        latency_ms = int((time.monotonic() - start) * 1000)

        if response.status_code == 429:
            raise LLMCallError("OpenAI rate limit (HTTP 429)", code="LLM_RATE_LIMITED")
        if response.status_code == 403:
            raise LLMCallError(
                "OpenAI quota/permission error (HTTP 403)", code="LLM_QUOTA_EXCEEDED"
            )
        if response.status_code >= 400:
            raise LLMCallError(
                f"OpenAI returned HTTP {response.status_code}: {response.text[:300]}",
                code=f"LLM_HTTP_{response.status_code}",
            )

        try:
            payload = response.json()
            text = payload["choices"][0]["message"]["content"]
            usage = payload.get("usage", {})
            input_tokens = int(usage.get("prompt_tokens", 0))
            output_tokens = int(usage.get("completion_tokens", 0))
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMCallError(
                f"OpenAI response missing expected fields: {exc}", code="LLM_MALFORMED_ENVELOPE"
            ) from exc

        cost_paise = compute_cost_paise(self._pricing, self._model, input_tokens, output_tokens)
        return LLMResponse(
            text=text,
            model=self._model,
            prompt_hash=hash_prompt(self._model, prompt),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_paise=cost_paise,
            latency_ms=latency_ms,
            cache_hit=False,
        )

    def __enter__(self) -> OpenAIClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def build_llm_client(settings: Any) -> LLMClient:
    """Selected by settings.llm_provider — the one place a caller turns
    config into a concrete client. `Any` rather than importing src.config
    here to avoid a cycle (src.config doesn't need to know about diagnose);
    callers pass their already-loaded Settings instance straight through."""
    if settings.llm_provider == "gemini":
        if not settings.llm_api_key:
            raise ConfigError(
                "LLM_PROVIDER=gemini but LLM_API_KEY is not set",
                code="MISSING_LLM_API_KEY",
                remediation="set LLM_API_KEY in .env",
            )
        return GeminiClient(settings.llm_api_key, settings.llm_model)
    if settings.llm_provider == "openai":
        if not settings.llm_api_key:
            raise ConfigError(
                "LLM_PROVIDER=openai but LLM_API_KEY is not set",
                code="MISSING_LLM_API_KEY",
                remediation="set LLM_API_KEY in .env",
            )
        return OpenAIClient(settings.llm_api_key, settings.llm_model)
    return NullClient()
