"""Four stopping rules, each returning a distinct reason string. Any one of
them halts the whole run — `Runner` writes a stage="stop" audit record and
exits cleanly with a summary, never via sys.exit inside the engine."""

from __future__ import annotations

from pathlib import Path

from src.config_models import Guardrails
from src.gate.checks import RunState

REASON_CONSECUTIVE_EXECUTOR_ERRORS = "consecutive_executor_errors"
REASON_CAP_BREACH = "cap_breach"
REASON_KILL_SWITCH = "kill_switch"
REASON_CLUSTER_ESCALATION = "cluster_escalation"


class StoppingRules:
    def check(self, state: RunState, g: Guardrails) -> str | None:
        if state.consecutive_executor_errors >= g.consecutive_executor_errors_stop:
            return REASON_CONSECUTIVE_EXECUTOR_ERRORS
        if state.cap_breached:
            return REASON_CAP_BREACH
        if Path(g.kill_switch_path).exists():
            return REASON_KILL_SWITCH
        if state.cluster_escalated:
            return REASON_CLUSTER_ESCALATION
        return None
