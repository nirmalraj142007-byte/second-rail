"""Closed error taxonomy for Second Rail.

Every error raised anywhere in this codebase is one of the subclasses below.
Three are expected control flow and must never crash a run — each is caught
at its own stage boundary and written to the audit log as a normal episode
outcome, not a failure:

    DuplicateEventError    a replayed webhook, handled by the dedup boundary
    GateRefusalError        an ineligible episode, suppressed and audited
    IdempotencyCollision    the idempotency key already exists — this is the
                             success path that proves duplicate-link avoidance

Three abort the run outright and must propagate to the top level:

    AdmissibilityError      the agent chose outside the pre-registered
                             admissible action set
    AuditChainError         the hash chain failed to verify
    StoppingRuleTriggered   a configured stopping rule fired (consecutive
                             executor errors, cap breach, kill switch)

Everything else (SchemaDriftError, SignatureError, OutOfOrderError,
CapBreachError, ExecutorError, AttributionError) is a genuine failure: it
gets logged with its `code`, retried where retry is defined, and — if
retries are exhausted — the episode is moved to the exception list rather
than silently dropped.
"""

from __future__ import annotations


class SecondRailError(Exception):
    """Base class for every error raised in this codebase."""

    code: str = "UNKNOWN"
    stage: str = "unknown"

    def __init__(
        self,
        message: str,
        *,
        remediation: str = "",
        code: str | None = None,
        stage: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.remediation = remediation
        if code is not None:
            self.code = code
        if stage is not None:
            self.stage = stage

    def __str__(self) -> str:
        if self.remediation:
            return f"[{self.code}] {self.message} — {self.remediation}"
        return f"[{self.code}] {self.message}"


class ConfigError(SecondRailError):
    """Missing or invalid configuration."""

    code = "CONFIG_ERROR"
    stage = "config"


class SchemaDriftError(SecondRailError):
    """A Razorpay field this codebase depends on is absent or renamed."""

    code = "SCHEMA_DRIFT"
    stage = "ingest"


class SignatureError(SecondRailError):
    """Webhook HMAC signature did not match."""

    code = "SIGNATURE_MISMATCH"
    stage = "ingest"


class DuplicateEventError(SecondRailError):
    """A webhook event was already processed. Expected control flow."""

    code = "DUPLICATE_EVENT"
    stage = "ingest"


class OutOfOrderError(SecondRailError):
    """A terminal-state event arrived before its predecessor."""

    code = "OUT_OF_ORDER"
    stage = "ingest"


class GateRefusalError(SecondRailError):
    """Episode is ineligible for any recovery action. Expected control flow."""

    code = "GATE_REFUSAL"
    stage = "gate"


class CapBreachError(SecondRailError):
    """A guardrail cap would be exceeded by the proposed action."""

    code = "CAP_BREACH"
    stage = "gate"


class AdmissibilityError(SecondRailError):
    """The agent chose an action outside the admissible set. Halts the run."""

    code = "ADMISSIBILITY_VIOLATION"
    stage = "choose"


class ExecutorError(SecondRailError):
    """An external API call failed after the retry cap was exhausted."""

    code = "EXECUTOR_FAILURE"
    stage = "execute"


class IdempotencyCollision(SecondRailError):
    """Idempotency key already used. This is the success path, not a bug."""

    code = "IDEMPOTENCY_COLLISION"
    stage = "execute"


class AttributionError(SecondRailError):
    """Outcome could not be attributed to an action within the window."""

    code = "ATTRIBUTION_FAILURE"
    stage = "attribute"


class AuditChainError(SecondRailError):
    """Hash chain verification failed. Halts the run."""

    code = "AUDIT_CHAIN_BROKEN"
    stage = "audit"


class StoppingRuleTriggered(SecondRailError):
    """A configured stopping rule fired. Halts the run."""

    code = "STOPPING_RULE_TRIGGERED"
    stage = "run"


class HoldoutLeakageError(SecondRailError):
    """Something under src/ tried to read the sealed split's ground-truth
    file. Halts immediately; this is never expected control flow. See
    scripts/holdout_guard.py."""

    code = "HOLDOUT_LEAKAGE"
    stage = "eval"
