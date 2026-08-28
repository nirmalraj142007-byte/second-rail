"""Deterministic fault-injection rig for the executor.

`FaultInjectingExecutor` wraps any `Executor` and fires a scripted failure
on exactly the Nth call to `create_recovery_link()` — N counted from 1, in
call order, reset every time `start_run()` is invoked. The rig is keyed on
episode index, never on wall-clock time or a random draw, so it fires on
the same episode every take of the demo.

A plan that targets an index outside the run raises in `start_run()`
rather than silently no-oping — a fault rig nobody can trust to fire is
worse than no rig at all.

Two backends, chosen automatically by what `inner` is:
  - RazorpayExecutor (or anything exposing a swappable `_client`): the real
    `with_backoff()` retry loop in `src/execute/executor.py` runs against a
    scripted sequence of HTTP responses, so the demo's backoff timing and
    audit records are real, not simulated.
  - Anything else (FixtureExecutor, used by `guardrail_proof.py
    --dry-run-first`): there is no HTTP layer to script, so a targeted
    index raises `ExecutorError` directly, simulating the *outcome*
    (execution_failed) without a real retry loop. That is enough to
    validate index wiring and downstream accounting before a run spends a
    real API call.

`inject_duplicate_reference_at_index` needs no backend-specific handling:
it just calls `create_recovery_link` twice for the same episode, which
exercises each executor's own real idempotency check (the local DB row /
in-memory dict) rather than simulating one — this works identically
against `RazorpayExecutor` and `FixtureExecutor`.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from src.errors import ExecutorError
from src.execute.executor import ExecutionResult, Executor
from src.gate.checks import Episode
from src.logging_setup import get_logger

logger = get_logger("faults")

_FAULT_KINDS = ("429", "timeout", "5xx", "duplicate")


@dataclass
class FaultPlan:
    inject_429_at_episode_index: int | None = None
    inject_429_repeat: int = 3  # keep failing for N attempts
    inject_timeout_at_index: int | None = None
    inject_5xx_at_index: int | None = None
    inject_duplicate_reference_at_index: int | None = None

    def targeted_indices(self) -> dict[int, str]:
        """episode index (1-based) -> fault kind, for every configured
        target. Raises if two fields target the same index (an ambiguous
        plan is not a deterministic one) or if any index is < 1."""
        targets: dict[int, str] = {}
        for index, kind in (
            (self.inject_429_at_episode_index, "429"),
            (self.inject_timeout_at_index, "timeout"),
            (self.inject_5xx_at_index, "5xx"),
            (self.inject_duplicate_reference_at_index, "duplicate"),
        ):
            if index is None:
                continue
            if index < 1:
                raise ValueError(
                    f"FaultPlan index must be >= 1 (episode indices are "
                    f"1-based), got {index} for fault kind {kind!r}"
                )
            if index in targets:
                raise ValueError(
                    f"FaultPlan targets episode index {index} with both "
                    f"{targets[index]!r} and {kind!r} — ambiguous, refusing to start"
                )
            targets[index] = kind
        return targets


class _ScriptedResponseClient:
    """Proxies a real Razorpay-client-shaped object, returning a scripted
    sequence of (status_code, body) responses before falling through to the
    real client for any call beyond the script. Only used against
    `RazorpayExecutor`'s own `with_backoff()` loop, so the injected fault
    exercises the real retry/backoff code path — not a parallel fake one."""

    def __init__(self, real: object, responses: list[tuple[int, object]]) -> None:
        self._real = real
        self._responses = list(responses)

    def create_payment_link_once(self, payload: dict, reference_id: str) -> tuple[int, object]:
        if self._responses:
            return self._responses.pop(0)
        return self._real.create_payment_link_once(payload, reference_id)  # type: ignore[attr-defined]

    def cancel_payment_link(self, plink_id: str) -> dict:
        return self._real.cancel_payment_link(plink_id)  # type: ignore[attr-defined]


class FaultInjectingExecutor:
    """Wraps `inner` and fires the plan's configured fault on exactly the
    matching episode index. See module docstring for the two backends."""

    def __init__(self, inner: Executor, plan: FaultPlan) -> None:
        self._inner = inner
        self._plan = plan
        self._targets = plan.targeted_indices()
        self._index = 0
        self._started = False

    def start_run(self, episode_count: int) -> None:
        """Call once before processing begins. Resets the episode counter
        and validates every configured fault index falls within
        [1, episode_count] — an out-of-range target raises here, at the
        start, rather than quietly never firing. Call again to reset the
        rig for a fresh take of the same run without rebuilding it."""
        out_of_range = sorted(i for i in self._targets if i > episode_count)
        if out_of_range:
            raise ValueError(
                f"FaultPlan targets episode index/indices {out_of_range} but "
                f"this run only has {episode_count} episode(s) — refusing to "
                "start rather than silently no-op"
            )
        self._index = 0
        self._started = True

    def create_recovery_link(
        self, episode: Episode, action: str, policy_rule_id: str, run_id: str
    ) -> ExecutionResult:
        if not self._started:
            raise RuntimeError(
                "FaultInjectingExecutor.start_run(episode_count) must be "
                "called before the first create_recovery_link() of a run"
            )
        self._index += 1
        kind = self._targets.get(self._index)
        if kind is None:
            return self._inner.create_recovery_link(episode, action, policy_rule_id, run_id)

        logger.info(
            "fault rig firing kind=%s at episode_index=%s payment_id=%s",
            kind, self._index, episode.payment_id,
        )

        if kind == "duplicate":
            return self._inject_duplicate(episode, action, policy_rule_id, run_id)
        if self._is_client_backed():
            return self._inject_via_client_swap(episode, action, policy_rule_id, run_id, kind)
        return self._inject_simulated(episode, kind)

    def cancel_link(self, plink_id: str) -> dict:
        return self._inner.cancel_link(plink_id)

    # -- fault kinds -------------------------------------------------

    def _inject_duplicate(
        self, episode: Episode, action: str, policy_rule_id: str, run_id: str
    ) -> ExecutionResult:
        """Call create_recovery_link twice for the same episode — the
        second call exercises the inner executor's *real* idempotency
        check (local DB row for RazorpayExecutor, in-memory dict for
        FixtureExecutor), so this fault kind needs no backend-specific
        simulation at all."""
        self._inner.create_recovery_link(episode, action, policy_rule_id, run_id)
        return self._inner.create_recovery_link(episode, action, policy_rule_id, run_id)

    def _is_client_backed(self) -> bool:
        return getattr(self._inner, "_client", None) is not None

    def _scripted_responses(self, kind: str) -> list[tuple[int, object]]:
        if kind == "429":
            body = {"error": {"description": "injected 429 (fault rig)"}}
            return [(429, body)] * self._plan.inject_429_repeat
        if kind == "timeout":
            # with_backoff() treats any status_code < 300 as success, so a
            # genuine transport-level (0, {...}) response — what
            # RazorpayClient.create_payment_link_once actually returns on
            # an httpx.TransportError — cannot be scripted through this
            # proxy without being read as a success. 408 Request Timeout is
            # the real, standard HTTP status for this failure mode, is not
            # in with_backoff()'s retryable set (429, 5xx), and so fails
            # after exactly one attempt, same as a real timeout would.
            return [(408, {"error": {"description": "simulated timeout (fault rig)"}})]
        if kind == "5xx":
            body = {"error": {"description": "injected 503 (fault rig)"}}
            # Fail every attempt the wrapped executor's own retry cap
            # allows, regardless of what that cap is configured to —
            # reading it here rather than hardcoding a count keeps this
            # fault "always exhausts retries" instead of "exhausts retries
            # only if the cap happens to be <= some guessed number".
            retry_cap = getattr(self._inner, "_retry_cap", 3)
            return [(503, body)] * retry_cap
        raise AssertionError(f"unreachable fault kind: {kind}")

    def _inject_via_client_swap(
        self, episode: Episode, action: str, policy_rule_id: str, run_id: str, kind: str
    ) -> ExecutionResult:
        real_client = self._inner._client  # type: ignore[attr-defined]
        self._inner._client = _ScriptedResponseClient(  # type: ignore[attr-defined]
            real_client, self._scripted_responses(kind)
        )
        try:
            return self._inner.create_recovery_link(episode, action, policy_rule_id, run_id)
        finally:
            self._inner._client = real_client  # type: ignore[attr-defined]

    def _inject_simulated(self, episode: Episode, kind: str) -> ExecutionResult:
        raise ExecutorError(
            f"fault rig: simulated {kind} fault (no HTTP backend on "
            f"{type(self._inner).__name__}) for {episode.payment_id}",
            code="FAULT_RIG_SIMULATED_FAILURE",
        )


def recovered_amount_paise(conn: sqlite3.Connection, run_id: str) -> int:
    """Sum of amount_paise for episodes with a successfully created
    execution in this run — used by the failure-demo narrative to show the
    recovery total excluding a failed episode. The full recovery-figure
    computation (baselines, sensitivity band) belongs to a later eval
    phase; this is a plain, real sum over one run's own execution rows,
    nothing more."""
    row = conn.execute(
        """
        SELECT COALESCE(SUM(ep.amount_paise), 0) AS total
        FROM execution ex
        JOIN episode ep ON ep.episode_id = ex.episode_id
        WHERE ex.run_id = ? AND ex.status = 'created'
        """,
        (run_id,),
    ).fetchone()
    return int(row["total"])
