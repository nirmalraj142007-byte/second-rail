"""Payment Link executor — the only place money-adjacent actions are taken.

This module creates reversible recovery actions (Razorpay Payment Links) and
records them in the audit trail. The only external effect is a cancellable Payment
Link requiring customer authentication — no debits, no refunds, no auto-charges.

Three implementations:
  - RazorpayExecutor: real test-mode API calls
  - FixtureExecutor: replays recorded responses for testing
  - FaultInjectingExecutor: wraps another to inject failures

Every execution is idempotent and keyed by (payment_id, policy_rule_id), stored
in a UNIQUE constraint. A re-run of the same episode produces zero new links.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from ulid import ULID

from src.db.repo import insert_execution
from src.errors import ExecutorError, IdempotencyCollision
from src.execute.idempotency import idempotency_key, reference_id
from src.execute.retry import BackoffError, with_backoff
from src.gate.checks import Episode
from src.logging_setup import get_logger
from src.razorpay_client import RazorpayClient

IST = ZoneInfo("Asia/Kolkata")
logger = get_logger("executor")


@dataclass
class ExecutionResult:
    """Outcome of a single execution attempt."""

    status: str  # "created", "duplicate_suppressed", "failed", "cancelled"
    idempotency_key: str
    plink_id: str | None = None
    short_url: str | None = None
    created_new: bool = True
    response_code: int | None = None
    error: str | None = None


class Executor(Protocol):
    """Interface for executing recovery actions."""

    def create_recovery_link(
        self,
        episode: Episode,
        action: str,
        policy_rule_id: str,
        run_id: str,
    ) -> ExecutionResult:
        """Create a recovery action for a single episode."""
        ...

    def cancel_link(self, plink_id: str) -> dict[str, Any]:
        """Cancel an existing Payment Link."""
        ...


class RazorpayExecutor:
    """Real Razorpay test-mode Payment Link executor."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        client: RazorpayClient,
        mode: str,
        run_id: str,
        callback_url: str | None = None,
        audit: object | None = None,
        retry_cap: int = 3,
        retry_delays: list[float] | None = None,
    ) -> None:
        self._conn = conn
        self._client = client
        self._mode = mode  # "dry_run" or "execute"
        self._run_id = run_id
        self._callback_url = callback_url
        # Optional AuditWriter so each retry attempt (attempt number, delay)
        # can be written to the audit record as it happens — see the
        # docstring on _on_attempt() in create_recovery_link().
        self._audit = audit
        # executor_retry_cap / executor_backoff_seconds (config/guardrails.yaml)
        # — never hardcoded here, per the project's money-adjacent-config rule.
        self._retry_cap = retry_cap
        self._retry_delays = retry_delays if retry_delays is not None else [1.0, 2.0, 4.0]

    def create_recovery_link(
        self,
        episode: Episode,
        action: str,
        policy_rule_id: str,
        run_id: str,
    ) -> ExecutionResult:
        """Create a Payment Link with idempotency checking."""
        key = idempotency_key(episode.payment_id, policy_rule_id)
        ref_id = reference_id(key)

        # Step 1: Check for local dedup (idempotency key already exists)
        existing = self._conn.execute(
            "SELECT plink_id, status FROM execution WHERE idempotency_key = ?",
            (key,),
        ).fetchone()

        if existing:
            logger.info(
                "idempotency hit: payment_id=%s, rule=%s, plink_id=%s",
                episode.payment_id,
                policy_rule_id,
                existing["plink_id"],
            )
            return ExecutionResult(
                status="duplicate_suppressed",
                idempotency_key=key,
                plink_id=existing["plink_id"],
                created_new=False,
            )

        # Step 2: Build the Payment Link payload
        payload = self._build_link_payload(episode, action, key, ref_id)

        # Step 3: Dry-run: hash the payload and record it without network
        if self._mode == "dry_run":
            request_hash = hashlib.sha256(str(payload).encode()).hexdigest()
            self._record_execution(
                conn=self._conn,
                episode_id=episode.episode_id,
                payment_id=episode.payment_id,
                idempotency_key=key,
                reference_id=ref_id,
                status="created",  # In dry-run, we pretend it would succeed
                response_code=None,
                plink_id=None,
                short_url=None,
                request_body_hash=request_hash,
                attempt=0,
                delay_ms=0,
                run_id=run_id,
            )
            return ExecutionResult(
                status="created",
                idempotency_key=key,
                plink_id=f"plink_dry_{key[:16]}",  # Synthetic ID for demo
                created_new=True,
                response_code=200,
            )

        # Step 4: Execute mode: retry with backoff.
        # create_payment_link_once() makes exactly one HTTP call and never
        # sleeps or raises on non-2xx — with_backoff() below is the only
        # retry loop, and on_attempt() writes each attempt + delay to the
        # audit record as it happens, so the backoff is visible on screen
        # and in evidence/audit/*.jsonl, not hidden inside the client.
        attempts: list[dict[str, Any]] = []

        def _try_create() -> tuple[int, Any]:
            status_code, body = self._client.create_payment_link_once(payload, ref_id)
            return (status_code, body)

        def _on_attempt(attempt_num: int, delay_sec: float) -> None:
            attempts.append({"attempt": attempt_num, "delay_s": delay_sec})
            if self._audit is not None:
                self._audit.append(
                    stage="execute",
                    actor="system",
                    episode_id=episode.episode_id,
                    payment_id=episode.payment_id,
                    rationale=(
                        f"attempt {attempt_num} rate-limited or errored, "
                        f"backing off {delay_sec}s"
                    ),
                    execution={"attempt": attempt_num, "delay_ms": int(delay_sec * 1000)},
                )
            time.sleep(delay_sec)

        try:
            status_code, response = with_backoff(
                _try_create,
                cap=self._retry_cap,
                delays=self._retry_delays,
                on_attempt=_on_attempt,
                retryable=lambda code: code == 429 or (500 <= code < 600),
            )
        except BackoffError as e:
            # Razorpay documents "payment link creation with reference ID
            # already attempted" as a 400 response to a duplicate
            # reference_id (see evidence/razorpay_field_report.md Step 5 —
            # not yet empirically confirmed on this account due to rate
            # limiting during the harvest, but this is the documented
            # behavior). Treated identically to local dedup: this is the
            # strongest possible evidence of server-side duplicate
            # protection and must be visible in the audit log, not masked
            # as a failure.
            body_str = str(e.last_response_body).lower()
            if e.last_status_code == 400 and "reference id" in body_str and "already" in body_str:
                logger.info(
                    "server-side duplicate detected for payment_id=%s: %s",
                    episode.payment_id,
                    e.last_response_body,
                )
                try:
                    self._record_execution(
                        conn=self._conn,
                        episode_id=episode.episode_id,
                        payment_id=episode.payment_id,
                        idempotency_key=key,
                        reference_id=ref_id,
                        status="duplicate_suppressed",
                        response_code=400,
                        plink_id=None,
                        short_url=None,
                        request_body_hash=hashlib.sha256(str(payload).encode()).hexdigest(),
                        attempt=len(attempts),
                        delay_ms=int((attempts[-1]["delay_s"] * 1000) if attempts else 0),
                        run_id=run_id,
                    )
                except IdempotencyCollision:
                    # This exact key already has a row from an earlier
                    # attempt (e.g. a caller re-submitting the same episode
                    # list through this same executor a second time, as
                    # scripts/guardrail_proof.py's idempotency-detection
                    # pass does) — the server-side duplicate this branch
                    # exists to record is already on file under this key,
                    # so there is nothing new to persist, only nothing to
                    # crash on either.
                    logger.warning(
                        "idempotency collision recording a server-side duplicate for "
                        "payment_id=%s — already on file under this key",
                        episode.payment_id,
                    )
                return ExecutionResult(
                    status="duplicate_suppressed",
                    idempotency_key=key,
                    created_new=False,
                    response_code=400,
                )
            logger.error(
                "executor exhausted retries: payment_id=%s, last_status=%s, "
                "last_response_body=%r",
                episode.payment_id,
                e.last_status_code,
                e.last_response_body,
            )
            try:
                self._record_execution(
                    conn=self._conn,
                    episode_id=episode.episode_id,
                    payment_id=episode.payment_id,
                    idempotency_key=key,
                    reference_id=ref_id,
                    status="failed",
                    response_code=e.last_status_code,
                    plink_id=None,
                    short_url=None,
                    request_body_hash=hashlib.sha256(str(payload).encode()).hexdigest(),
                    attempt=len(attempts),
                    delay_ms=int((attempts[-1]["delay_s"] * 1000) if attempts else 0),
                    run_id=run_id,
                )
            except IdempotencyCollision:
                # A prior attempt under this same idempotency key (this run
                # or an earlier one) already recorded created/failed/
                # duplicate_suppressed — unlike the "created" success path
                # above, which re-reads and returns the existing row, this
                # attempt itself still genuinely failed and the caller
                # (src/runner.py) needs to know that, so ExecutorError is
                # still raised below; only the redundant duplicate insert
                # is what this catches.
                logger.warning(
                    "idempotency collision recording a second failed attempt for "
                    "payment_id=%s — a prior attempt under the same key is already "
                    "on file; this attempt still failed and is still reported as such",
                    episode.payment_id,
                )
            raise ExecutorError(
                f"failed to create Payment Link for {episode.payment_id}: "
                f"HTTP {e.last_status_code} — body: {e.last_response_body!r}",
                code="PAYMENT_LINK_CREATION_FAILED",
            ) from e

        # Step 5: Check for UNIQUE(idempotency_key) collision during insert
        plink_id = response.get("id")
        short_url = response.get("short_url")
        request_hash = hashlib.sha256(str(payload).encode()).hexdigest()

        try:
            self._record_execution(
                conn=self._conn,
                episode_id=episode.episode_id,
                payment_id=episode.payment_id,
                idempotency_key=key,
                reference_id=ref_id,
                status="created",
                response_code=status_code,
                plink_id=plink_id,
                short_url=short_url,
                request_body_hash=request_hash,
                attempt=len(attempts),
                delay_ms=int((attempts[-1]["delay_s"] * 1000) if attempts else 0),
                run_id=run_id,
            )
        except IdempotencyCollision:
            # A race: two threads entered at the same time with the same key.
            # Re-read the existing row and return it.
            logger.warning(
                "idempotency collision (race): payment_id=%s, retrying read",
                episode.payment_id,
            )
            existing = self._conn.execute(
                "SELECT plink_id FROM execution WHERE idempotency_key = ?",
                (key,),
            ).fetchone()
            return ExecutionResult(
                status="duplicate_suppressed",
                idempotency_key=key,
                plink_id=existing["plink_id"] if existing else None,
                created_new=False,
            )

        logger.info(
            "payment link created: payment_id=%s, plink_id=%s",
            episode.payment_id,
            plink_id,
        )

        return ExecutionResult(
            status="created",
            idempotency_key=key,
            plink_id=plink_id,
            short_url=short_url,
            created_new=True,
            response_code=status_code,
        )

    def cancel_link(self, plink_id: str) -> dict[str, Any]:
        """Cancel a Payment Link via the Razorpay API."""
        try:
            result = self._client.cancel_payment_link(plink_id)
            logger.info("cancelled payment link: %s", plink_id)
            return result
        except ExecutorError as e:
            logger.error("failed to cancel payment link %s: %s", plink_id, e)
            raise

    def _build_link_payload(
        self,
        episode: Episode,
        action: str,
        idempotency_key: str,
        ref_id: str,
    ) -> dict[str, Any]:
        """Build the Payment Link creation payload."""
        # Calculate expiry: 7 days from now
        expires_at = datetime.now(IST) + timedelta(days=7)
        expires_epoch = int(expires_at.timestamp())

        payload = {
            "amount": episode.amount_paise,  # Already in paise, never recomputed
            "currency": "INR",
            "description": f"Recovery for failed payment {episode.payment_id[:20]}...",
            "customer": {
                "name": f"Customer {episode.customer_id[:8]}",
                "contact": None,  # Synthetic only, no real phone
                "email": None,  # Synthetic only, no real email
            },
            "notify": {
                "sms": True,
                "email": True,
            },
            "reminder_enable": False,
            "notes": {
                "episode_id": episode.episode_id,
                "idempotency_key": idempotency_key,
                "policy_rule_id": "will_be_added_by_policy_engine",
                "run_id": self._run_id,
            },
            "callback_url": self._callback_url or "https://example.com/callback",
            "callback_method": "get",
            "expire_by": expires_epoch,
        }

        return payload

    def _record_execution(
        self,
        conn: sqlite3.Connection,
        episode_id: str,
        payment_id: str,
        idempotency_key: str,
        reference_id: str,
        status: str,
        response_code: int | None,
        plink_id: str | None,
        short_url: str | None,
        request_body_hash: str,
        attempt: int,
        delay_ms: int,
        run_id: str,
    ) -> None:
        """Insert an execution record via the shared typed helper.

        insert_execution() (src/db/repo.py) already converts the UNIQUE
        (idempotency_key) IntegrityError into IdempotencyCollision — the
        expected control-flow path for a racing duplicate create — so this
        method does not duplicate that translation.
        """
        insert_execution(
            conn,
            execution_id=str(ULID()),
            episode_id=episode_id,
            idempotency_key=idempotency_key,
            reference_id=reference_id,
            api="payment_links",
            plink_id=plink_id,
            short_url=short_url,
            request_body_hash=request_body_hash,
            response_code=response_code,
            attempt=attempt,
            delay_ms=delay_ms,
            status=status,
            run_id=run_id,
            created_at=datetime.now(IST).isoformat(timespec="seconds"),
        )


class FixtureExecutor:
    """No-network executor for `make eval` on a machine with no Razorpay key.

    Reads `fixture_dir/<episode_id>.json` if present (a recorded real
    response, for replaying a specific captured scenario); otherwise
    synthesizes a deterministic `plink_id` from the episode's own
    idempotency key so the same episode always replays the same fixture
    response without ever touching the network.

    `conn` is optional (defaults to `None`, matching every existing call
    site — `scripts/guardrail_proof.py --dry-run-first` and
    `tests/test_executor.py` construct this with no connection at all,
    since they only ever check the returned `ExecutionResult`). When
    supplied, every "created" result is also persisted via
    `insert_execution()`, exactly like `RazorpayExecutor` — without this, a
    caller that reads the `execution` table afterward (e.g.
    `scripts/eval.py`'s false-positive cost check, which is DB-driven) would
    silently see zero rows despite the run having "actioned" episodes.
    """

    def __init__(self, fixture_dir: Path, conn: sqlite3.Connection | None = None) -> None:
        self._fixture_dir = fixture_dir
        self._conn = conn
        self._created: dict[str, str] = {}  # idempotency_key -> plink_id

    def create_recovery_link(
        self,
        episode: Episode,
        action: str,
        policy_rule_id: str,
        run_id: str,
    ) -> ExecutionResult:
        key = idempotency_key(episode.payment_id, policy_rule_id)

        if key in self._created:
            return ExecutionResult(
                status="duplicate_suppressed",
                idempotency_key=key,
                plink_id=self._created[key],
                created_new=False,
            )

        fixture_path = self._fixture_dir / f"{episode.episode_id}.json"
        if fixture_path.exists():
            import json

            recorded = json.loads(fixture_path.read_text(encoding="utf-8"))
            plink_id = recorded["id"]
            short_url = recorded.get("short_url")
        else:
            plink_id = f"plink_fixture_{key[:16]}"
            short_url = f"https://rzp.io/fixture/{key[:16]}"

        self._created[key] = plink_id

        if self._conn is not None:
            try:
                insert_execution(
                    self._conn,
                    execution_id=str(ULID()),
                    episode_id=episode.episode_id,
                    idempotency_key=key,
                    reference_id=reference_id(key),
                    api="payment_links",
                    plink_id=plink_id,
                    short_url=short_url,
                    request_body_hash=hashlib.sha256(key.encode()).hexdigest(),
                    response_code=200,
                    attempt=0,
                    delay_ms=0,
                    status="created",
                    run_id=run_id,
                    created_at=datetime.now(IST).isoformat(timespec="seconds"),
                )
            except IdempotencyCollision:
                logger.warning(
                    "fixture idempotency collision (DB already has this key): payment_id=%s",
                    episode.payment_id,
                )

        return ExecutionResult(
            status="created",
            idempotency_key=key,
            plink_id=plink_id,
            short_url=short_url,
            created_new=True,
            response_code=200,
        )

    def cancel_link(self, plink_id: str) -> dict[str, Any]:
        """Return a fixture cancel response."""
        return {"id": plink_id, "status": "cancelled"}


class _ForcedFailureClient:
    """Proxies a RazorpayClient, forcing `fail_count` failing responses out
    of create_payment_link_once before passing every later call straight
    through to the real client. Used only by FaultInjectingExecutor so the
    injected fault exercises RazorpayExecutor's real with_backoff() call —
    not a separate, parallel fake retry path."""

    def __init__(self, real_client: RazorpayClient, fail_count: int, status_code: int) -> None:
        self._real = real_client
        self._remaining_failures = fail_count
        self._status_code = status_code

    def create_payment_link_once(
        self, payload: dict[str, Any], reference_id: str
    ) -> tuple[int, Any]:
        if self._remaining_failures > 0:
            self._remaining_failures -= 1
            return self._status_code, {"error": {"description": "injected fault"}}
        return self._real.create_payment_link_once(payload, reference_id)

    def cancel_payment_link(self, plink_id: str) -> dict[str, Any]:
        return self._real.cancel_payment_link(plink_id)


class FaultInjectingExecutor:
    """Wraps a RazorpayExecutor and forces its Nth create_recovery_link call
    to see `fail_count` consecutive failing HTTP responses before the real
    client is used — so the wrapped executor's own retry/backoff logic
    (src/execute/retry.py) runs against a real fault instead of a mocked
    outcome, reproducing the demo's "injected 429 mid-batch" scenario
    on demand.
    """

    def __init__(
        self,
        wrapped: RazorpayExecutor,
        fail_on_attempt_number: int = 7,
        fail_count: int = 3,
        status_code: int = 429,
    ) -> None:
        self._wrapped = wrapped
        self._fail_on_attempt_number = fail_on_attempt_number
        self._fail_count = fail_count
        self._status_code = status_code
        self._call_counter = 0

    def create_recovery_link(
        self,
        episode: Episode,
        action: str,
        policy_rule_id: str,
        run_id: str,
    ) -> ExecutionResult:
        self._call_counter += 1
        if self._call_counter == self._fail_on_attempt_number:
            real_client = self._wrapped._client
            self._wrapped._client = _ForcedFailureClient(
                real_client, self._fail_count, self._status_code
            )
            try:
                return self._wrapped.create_recovery_link(episode, action, policy_rule_id, run_id)
            finally:
                self._wrapped._client = real_client
        return self._wrapped.create_recovery_link(episode, action, policy_rule_id, run_id)

    def cancel_link(self, plink_id: str) -> dict[str, Any]:
        """Delegate to wrapped executor."""
        return self._wrapped.cancel_link(plink_id)
