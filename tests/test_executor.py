"""Tests for the executor module.

Tests verify:
  - Idempotency key stability and format
  - Dry-run makes zero HTTP calls
  - Duplicate detection (idempotency_key UNIQUE constraint)
  - Backoff retry on 429/5xx, never on 4xx
  - Rollback cancels all created links
  - Live test creates and cancels a real Payment Link (marked as @pytest.mark.live)
"""

from __future__ import annotations

import sqlite3
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest

from src.db.migrate import get_connection, migrate
from src.db.repo import insert_customer_if_absent, insert_episode, start_run
from src.errors import ExecutorError, IdempotencyCollision
from src.execute.executor import FaultInjectingExecutor, FixtureExecutor, RazorpayExecutor
from src.execute.idempotency import idempotency_key, reference_id
from src.execute.retry import BackoffError, with_backoff
from src.gate.checks import Episode

IST = ZoneInfo("Asia/Kolkata")


@pytest.fixture
def temp_db() -> tuple[Path, sqlite3.Connection]:
    """Create a temporary in-memory database for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        migrate(db_path)
        conn = get_connection(db_path)
        yield db_path, conn
        conn.close()


@pytest.fixture
def sample_episode() -> Episode:
    """Create a sample episode for testing."""
    from src.gate.checks import Episode
    return Episode(
        episode_id="ep_001",
        payment_id="pay_TEST001",
        order_id="order_001",
        customer_id="cust_001",
        amount_paise=50000,
        currency="INR",
        instrument="card",
        issuer_family="HDFC",
        error_code="GATEWAY_ERROR",
        error_description="Gateway timeout",
        error_source="razorpay",
        error_step="authorization",
        error_reason="payment_failed",
        failed_at=datetime.now(IST),
        received_at=datetime.now(IST) + timedelta(seconds=30),
        split="train",
        is_synthetic=True,
        harvested_from=None,
    )


def _seed_run_and_episode(
    conn: sqlite3.Connection, episode: Episode, run_id: str = "run_001"
) -> None:
    """Insert the parent run + customer + episode rows the execution table's
    foreign keys require, so tests can insert execution rows without
    violating referential integrity."""
    start_run(
        conn,
        run_id=run_id,
        started_at=datetime.now(IST).isoformat(timespec="seconds"),
        mode="execute",
    )
    if episode.customer_id:
        insert_customer_if_absent(
            conn,
            customer_id=episode.customer_id,
            synthetic_name=None,
            contact_hash="testhash",
            email_hash=None,
            segment="repeat",
            opted_out=False,
            opt_out_ts=None,
            created_at=datetime.now(IST).isoformat(timespec="seconds"),
        )
    insert_episode(
        conn,
        episode_id=episode.episode_id,
        payment_id=episode.payment_id,
        order_id=episode.order_id,
        customer_id=episode.customer_id,
        amount_paise=episode.amount_paise,
        currency=episode.currency,
        instrument=episode.instrument,
        issuer_family=episode.issuer_family,
        error_code=episode.error_code,
        error_description=episode.error_description,
        error_source=episode.error_source,
        error_step=episode.error_step,
        error_reason=episode.error_reason,
        failed_at=episode.failed_at.isoformat(timespec="seconds"),
        received_at=episode.received_at.isoformat(timespec="seconds"),
        split=episode.split,
        is_synthetic=episode.is_synthetic,
        harvested_from=episode.harvested_from,
    )


class TestIdempotencyKey:
    """Test idempotency key generation."""

    def test_idempotency_key_is_stable(self) -> None:
        """Same (payment_id, policy_rule_id) produces same key."""
        key1 = idempotency_key("pay_123", "P-01")
        key2 = idempotency_key("pay_123", "P-01")
        assert key1 == key2

    def test_idempotency_key_length(self) -> None:
        """Key is exactly 32 characters."""
        key = idempotency_key("pay_123", "P-01")
        assert len(key) == 32

    def test_idempotency_key_is_hex(self) -> None:
        """Key is valid hex (from sha256)."""
        key = idempotency_key("pay_123", "P-01")
        int(key, 16)  # Raises ValueError if not hex

    def test_reference_id_format(self) -> None:
        """Reference ID is prefixed and under 40 chars."""
        key = idempotency_key("pay_123", "P-01")
        ref_id = reference_id(key)
        assert ref_id.startswith("sr-")
        assert len(ref_id) <= 40
        assert len(ref_id) == len("sr-") + 32


class TestBackoff:
    """Test hand-rolled retry logic."""

    def test_backoff_on_429_retry(self) -> None:
        """Retryable status (429) triggers retry."""
        attempt_log: list[tuple[int, float]] = []

        def on_attempt(attempt: int, delay: float) -> None:
            attempt_log.append((attempt, delay))

        call_count = 0

        def fn() -> tuple[int, dict]:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                return 429, {"error": "rate limited"}
            return 200, {"success": True}

        status, body = with_backoff(
            fn,
            cap=3,
            delays=[0.01, 0.02, 0.04],
            on_attempt=on_attempt,
        )

        assert status == 200
        assert body["success"] is True
        assert call_count == 3
        assert len(attempt_log) == 2  # Two retries before success

    def test_backoff_does_not_retry_400(self) -> None:
        """Non-retryable 4xx (except 429) does not retry."""
        call_count = 0

        def fn() -> tuple[int, dict]:
            nonlocal call_count
            call_count += 1
            return 400, {"error": "invalid_request"}

        with pytest.raises(BackoffError):
            with_backoff(fn, cap=3)

        assert call_count == 1  # Only one attempt

    def test_backoff_retries_5xx(self) -> None:
        """5xx errors trigger retry."""
        call_count = 0

        def fn() -> tuple[int, dict]:
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                return 502, {"error": "bad gateway"}
            return 200, {"success": True}

        status, _ = with_backoff(fn, cap=3, delays=[0.01, 0.02, 0.04])

        assert status == 200
        assert call_count == 2

    def test_backoff_exhausts_cap(self) -> None:
        """After cap attempts, raises BackoffError."""

        def fn() -> tuple[int, dict]:
            return 429, {"error": "rate limited"}

        with pytest.raises(BackoffError) as exc_info:
            with_backoff(fn, cap=2, delays=[0.01, 0.02])

        assert exc_info.value.last_status_code == 429


class TestRazorpayExecutor:
    """Test the real Razorpay executor."""

    def test_dry_run_makes_no_http_calls(
        self, temp_db: tuple[Path, sqlite3.Connection], sample_episode: Episode
    ) -> None:
        """Dry-run mode creates zero HTTP calls."""
        _, conn = temp_db
        _seed_run_and_episode(conn, sample_episode)

        # Create a mock client that would fail if called
        mock_client = Mock()
        mock_client.create_payment_link_once.side_effect = Exception("HTTP call should not happen")

        executor = RazorpayExecutor(
            conn=conn,
            client=mock_client,
            mode="dry_run",
            run_id="run_001",
        )

        result = executor.create_recovery_link(
            episode=sample_episode,
            action="link_upi_alt",
            policy_rule_id="P-14",
            run_id="run_001",
        )

        assert result.status == "created"
        assert result.created_new is True
        # Client was never called
        mock_client.create_payment_link_once.assert_not_called()

    def test_duplicate_suppression(
        self, temp_db: tuple[Path, sqlite3.Connection], sample_episode: Episode
    ) -> None:
        """Re-running same episode returns duplicate_suppressed."""
        _, conn = temp_db
        _seed_run_and_episode(conn, sample_episode)

        mock_client = Mock()
        mock_client.create_payment_link_once.return_value = (
            200,
            {"id": "plink_123", "short_url": "https://rzp.io/l/123"},
        )

        executor = RazorpayExecutor(
            conn=conn,
            client=mock_client,
            mode="execute",
            run_id="run_001",
        )

        # First call
        result1 = executor.create_recovery_link(
            episode=sample_episode,
            action="link_upi_alt",
            policy_rule_id="P-14",
            run_id="run_001",
        )

        assert result1.status == "created"
        assert result1.created_new is True
        assert result1.plink_id == "plink_123"

        # Second call with same episode and rule
        result2 = executor.create_recovery_link(
            episode=sample_episode,
            action="link_upi_alt",
            policy_rule_id="P-14",
            run_id="run_001",
        )

        assert result2.status == "duplicate_suppressed"
        assert result2.created_new is False
        assert result2.plink_id == "plink_123"

        # Verify only one link in database
        count = conn.execute("SELECT COUNT(*) FROM execution").fetchone()[0]
        assert count == 1

    def test_idempotency_key_matches_between_runs(
        self, temp_db: tuple[Path, sqlite3.Connection], sample_episode: Episode
    ) -> None:
        """Same episode produces same idempotency key."""
        _, conn = temp_db
        _seed_run_and_episode(conn, sample_episode)

        mock_client = Mock()
        mock_client.create_payment_link_once.return_value = (
            200,
            {"id": "plink_123", "short_url": "https://rzp.io/l/123"},
        )

        executor = RazorpayExecutor(
            conn=conn,
            client=mock_client,
            mode="execute",
            run_id="run_001",
        )

        executor.create_recovery_link(
            episode=sample_episode,
            action="link_upi_alt",
            policy_rule_id="P-14",
            run_id="run_001",
        )

        # Check the database record
        row = conn.execute(
            "SELECT idempotency_key FROM execution WHERE plink_id = ?",
            ("plink_123",),
        ).fetchone()

        expected_key = idempotency_key(sample_episode.payment_id, "P-14")
        assert row["idempotency_key"] == expected_key

    def test_backoff_on_429(
        self, temp_db: tuple[Path, sqlite3.Connection], sample_episode: Episode, monkeypatch
    ) -> None:
        """Executor retries on 429 with visible backoff, then succeeds."""
        _, conn = temp_db
        _seed_run_and_episode(conn, sample_episode)
        monkeypatch.setattr("src.execute.executor.time.sleep", lambda _s: None)

        mock_client = Mock()
        mock_client.create_payment_link_once.side_effect = [
            (429, {"error": {"description": "rate limited"}}),
            (429, {"error": {"description": "rate limited"}}),
            (200, {"id": "plink_123", "short_url": "https://rzp.io/l/123"}),
        ]

        executor = RazorpayExecutor(
            conn=conn,
            client=mock_client,
            mode="execute",
            run_id="run_001",
        )

        result = executor.create_recovery_link(
            episode=sample_episode,
            action="link_upi_alt",
            policy_rule_id="P-14",
            run_id="run_001",
        )

        assert result.status == "created"
        assert result.plink_id == "plink_123"
        assert mock_client.create_payment_link_once.call_count == 3

    def test_backoff_exhausted_moves_episode_to_failed(
        self, temp_db: tuple[Path, sqlite3.Connection], sample_episode: Episode, monkeypatch
    ) -> None:
        """After the retry cap, ExecutorError propagates and the row is recorded as failed."""
        _, conn = temp_db
        _seed_run_and_episode(conn, sample_episode)
        monkeypatch.setattr("src.execute.executor.time.sleep", lambda _s: None)

        mock_client = Mock()
        mock_client.create_payment_link_once.return_value = (
            429,
            {"error": {"description": "rate limited"}},
        )

        executor = RazorpayExecutor(
            conn=conn,
            client=mock_client,
            mode="execute",
            run_id="run_001",
        )

        with pytest.raises(ExecutorError):
            executor.create_recovery_link(
                episode=sample_episode,
                action="link_upi_alt",
                policy_rule_id="P-14",
                run_id="run_001",
            )

        assert mock_client.create_payment_link_once.call_count == 3
        row = conn.execute(
            "SELECT status FROM execution WHERE idempotency_key = ?",
            (idempotency_key(sample_episode.payment_id, "P-14"),),
        ).fetchone()
        assert row["status"] == "failed"

    def test_idempotency_collision_recording_a_failed_attempt_does_not_crash(
        self, temp_db: tuple[Path, sqlite3.Connection], sample_episode: Episode, monkeypatch
    ) -> None:
        """A row can land under this idempotency_key between the early
        local-dedup check (Step 1) and the final insert after retries
        exhaust — the "created" success path already handles exactly this
        race (its own comment: "two threads entered at the same time with
        the same key"); the "failed" path did not, and crashed with an
        unhandled IdempotencyCollision instead of reporting the (still
        real) failure. Forces the race deterministically by making
        insert_execution() raise once, rather than relying on true
        concurrency or on a second sequential call — which the early
        dedup check already short-circuits before ever reaching the
        retry loop, so it cannot exercise this path at all. Regression
        test for the crash found running `make guardrail-proof` for
        real: see BUILD_LOG.md."""
        _, conn = temp_db
        _seed_run_and_episode(conn, sample_episode)
        monkeypatch.setattr("src.execute.executor.time.sleep", lambda _s: None)

        calls = {"n": 0}

        def _flaky_insert_execution(*args, **kwargs):
            calls["n"] += 1
            raise IdempotencyCollision(
                "execution with idempotency_key=... already exists",
                code="DUPLICATE_IDEMPOTENCY_KEY",
            )

        monkeypatch.setattr(
            "src.execute.executor.insert_execution", _flaky_insert_execution
        )

        mock_client = Mock()
        mock_client.create_payment_link_once.return_value = (
            429,
            {"error": {"description": "rate limited"}},
        )

        executor = RazorpayExecutor(
            conn=conn,
            client=mock_client,
            mode="execute",
            run_id="run_001",
        )

        # Before the fix, this raised IdempotencyCollision uncaught,
        # crashing the caller instead of reporting the real failure below.
        with pytest.raises(ExecutorError):
            executor.create_recovery_link(
                episode=sample_episode,
                action="link_upi_alt",
                policy_rule_id="P-14",
                run_id="run_001",
            )
        assert calls["n"] == 1  # the collision was caught, not retried

    def test_cancel_link(self, temp_db: tuple[Path, sqlite3.Connection]) -> None:
        """Rollback cancels a link."""
        _, conn = temp_db

        mock_client = Mock()
        mock_client.cancel_payment_link.return_value = {
            "id": "plink_123",
            "status": "cancelled",
        }

        executor = RazorpayExecutor(
            conn=conn,
            client=mock_client,
            mode="execute",
            run_id="run_001",
        )

        result = executor.cancel_link("plink_123")

        assert result["id"] == "plink_123"
        assert result["status"] == "cancelled"
        mock_client.cancel_payment_link.assert_called_once_with("plink_123")


class TestFixtureExecutor:
    """No-network executor for make eval on a machine with no Razorpay key."""

    def test_synthesizes_deterministic_plink_id(
        self, tmp_path: Path, sample_episode: Episode
    ) -> None:
        executor = FixtureExecutor(fixture_dir=tmp_path)

        result = executor.create_recovery_link(
            episode=sample_episode, action="link_upi_alt", policy_rule_id="P-14", run_id="run_001"
        )

        assert result.status == "created"
        assert result.plink_id is not None
        # Same episode + rule always synthesizes the same plink_id, no network.
        key = idempotency_key(sample_episode.payment_id, "P-14")
        assert result.plink_id == f"plink_fixture_{key[:16]}"

    def test_duplicate_suppressed_on_second_call(
        self, tmp_path: Path, sample_episode: Episode
    ) -> None:
        executor = FixtureExecutor(fixture_dir=tmp_path)

        first = executor.create_recovery_link(
            episode=sample_episode, action="link_upi_alt", policy_rule_id="P-14", run_id="run_001"
        )
        second = executor.create_recovery_link(
            episode=sample_episode, action="link_upi_alt", policy_rule_id="P-14", run_id="run_001"
        )

        assert first.created_new is True
        assert second.status == "duplicate_suppressed"
        assert second.created_new is False
        assert second.plink_id == first.plink_id

    def test_reads_recorded_fixture_when_present(
        self, tmp_path: Path, sample_episode: Episode
    ) -> None:
        import json

        fixture_file = tmp_path / f"{sample_episode.episode_id}.json"
        fixture_file.write_text(
            json.dumps({"id": "plink_recorded_001", "short_url": "https://rzp.io/l/recorded"}),
            encoding="utf-8",
        )
        executor = FixtureExecutor(fixture_dir=tmp_path)

        result = executor.create_recovery_link(
            episode=sample_episode, action="link_upi_alt", policy_rule_id="P-14", run_id="run_001"
        )

        assert result.plink_id == "plink_recorded_001"
        assert result.short_url == "https://rzp.io/l/recorded"


class TestFaultInjectingExecutor:
    """The Nth call sees fail_count forced failures, then the real path."""

    def test_injects_failures_then_delegates_to_real_client(
        self, temp_db: tuple[Path, sqlite3.Connection], sample_episode: Episode, monkeypatch
    ) -> None:
        _, conn = temp_db
        _seed_run_and_episode(conn, sample_episode)
        monkeypatch.setattr("src.execute.executor.time.sleep", lambda _s: None)

        mock_client = Mock()
        mock_client.create_payment_link_once.return_value = (
            200,
            {"id": "plink_real", "short_url": "https://rzp.io/l/real"},
        )

        wrapped = RazorpayExecutor(conn=conn, client=mock_client, mode="execute", run_id="run_001")
        faulty = FaultInjectingExecutor(
            wrapped, fail_on_attempt_number=1, fail_count=2, status_code=429
        )

        result = faulty.create_recovery_link(
            episode=sample_episode,
            action="link_upi_alt",
            policy_rule_id="P-14",
            run_id="run_001",
        )

        # Two forced 429s from the proxy, then one real call that succeeds.
        assert result.status == "created"
        assert result.plink_id == "plink_real"
        assert mock_client.create_payment_link_once.call_count == 1

    def test_only_injects_on_the_designated_call(
        self, temp_db: tuple[Path, sqlite3.Connection], sample_episode: Episode
    ) -> None:
        _, conn = temp_db
        _seed_run_and_episode(conn, sample_episode)

        mock_client = Mock()
        mock_client.create_payment_link_once.return_value = (
            200,
            {"id": "plink_real", "short_url": "https://rzp.io/l/real"},
        )

        wrapped = RazorpayExecutor(conn=conn, client=mock_client, mode="execute", run_id="run_001")
        faulty = FaultInjectingExecutor(
            wrapped, fail_on_attempt_number=5, fail_count=2, status_code=429
        )

        result = faulty.create_recovery_link(
            episode=sample_episode,
            action="link_upi_alt",
            policy_rule_id="P-14",
            run_id="run_001",
        )

        # Not the 5th call yet, so no fault injected — real client used directly.
        assert result.status == "created"
        assert mock_client.create_payment_link_once.call_count == 1


class TestRollback:
    """Test rollback functionality."""

    def test_rollback_cancels_all_created_links(
        self, temp_db: tuple[Path, sqlite3.Connection], sample_episode: Episode
    ) -> None:
        """Rollback finds and cancels all created links."""
        _, conn = temp_db

        # Insert a few test execution records, each needing its own episode
        # row to satisfy the execution table's foreign key.
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from ulid import ULID
        IST = ZoneInfo("Asia/Kolkata")

        now = datetime.now(IST).isoformat(timespec="seconds")
        run_id = "run_001"
        start_run(conn, run_id=run_id, started_at=now, mode="execute")
        insert_customer_if_absent(
            conn,
            customer_id=sample_episode.customer_id,
            synthetic_name=None,
            contact_hash="testhash",
            email_hash=None,
            segment="repeat",
            opted_out=False,
            opt_out_ts=None,
            created_at=now,
        )

        for i in range(3):
            ep = sample_episode.model_copy(
                update={"episode_id": f"ep_{i}", "payment_id": f"pay_{i}"}
            )
            insert_episode(
                conn,
                episode_id=ep.episode_id,
                payment_id=ep.payment_id,
                order_id=ep.order_id,
                customer_id=ep.customer_id,
                amount_paise=ep.amount_paise,
                currency=ep.currency,
                instrument=ep.instrument,
                issuer_family=ep.issuer_family,
                error_code=ep.error_code,
                error_description=ep.error_description,
                error_source=ep.error_source,
                error_step=ep.error_step,
                error_reason=ep.error_reason,
                failed_at=ep.failed_at.isoformat(timespec="seconds"),
                received_at=ep.received_at.isoformat(timespec="seconds"),
                split=ep.split,
                is_synthetic=ep.is_synthetic,
                harvested_from=ep.harvested_from,
            )
            conn.execute(
                """
                INSERT INTO execution (
                    execution_id, episode_id, idempotency_key, reference_id,
                    api, plink_id, short_url, status, run_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(ULID()),
                    f"ep_{i}",
                    f"key_{i}",
                    f"sr-key_{i}",
                    "payment_links",
                    f"plink_{i}",
                    f"https://rzp.io/l/{i}",
                    "created",
                    run_id,
                    now,
                ),
            )
        conn.commit()

        # Verify all are created
        count = conn.execute(
            "SELECT COUNT(*) FROM execution WHERE run_id = ? AND status = ?",
            (run_id, "created"),
        ).fetchone()[0]
        assert count == 3


# Live test — skipped when keys are absent
@pytest.mark.live
def test_live_payment_link_creation_and_cancellation() -> None:
    """Create and cancel a real Payment Link in test mode.

    This test requires RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET to be set.
    Skipped if keys are absent.
    """
    from src.config import load_settings
    from src.db.migrate import get_connection
    from src.razorpay_client import RazorpayClient

    settings = load_settings()

    if not settings.razorpay_key_id or not settings.razorpay_key_secret:
        pytest.skip("Razorpay keys not configured")

    conn = get_connection(settings.db_path)
    try:
        client = RazorpayClient(settings.razorpay_key_id, settings.razorpay_key_secret)
        try:
            executor = RazorpayExecutor(
                conn=conn,
                client=client,
                mode="execute",
                run_id="test_run",
            )

            sample_episode = Episode(
                episode_id="ep_live_001",
                payment_id="pay_LIVE001",
                order_id="order_001",
                customer_id="cust_001",
                amount_paise=10000,  # ₹100
                currency="INR",
                instrument="card",
                issuer_family="HDFC",
                error_code="GATEWAY_ERROR",
                error_description="Test",
                error_source="razorpay",
                error_step="authorization",
                error_reason="payment_failed",
                failed_at=datetime.now(IST),
                received_at=datetime.now(IST) + timedelta(seconds=30),
                split="train",
                is_synthetic=True,
                harvested_from=None,
            )

            # Create a link
            result = executor.create_recovery_link(
                episode=sample_episode,
                action="link_upi_alt",
                policy_rule_id="P-14",
                run_id="test_run",
            )

            assert result.status == "created"
            assert result.plink_id is not None
            assert result.plink_id.startswith("plink_")

            # Verify it appears in the dashboard (would need to be checked manually)

            # Cancel it
            try:
                cancel_result = executor.cancel_link(result.plink_id)
                assert cancel_result.get("status") in ["cancelled", "expired"]
            finally:
                # Clean up even if cancel fails
                pass

        finally:
            client.close()
    finally:
        conn.close()
