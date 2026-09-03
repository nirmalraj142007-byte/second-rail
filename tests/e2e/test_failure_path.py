"""The same arc as test_happy_path.py, except the executor exhausts its
retry cap under an injected 429 — proving a failure is accounted for
honestly (excluded from gross recovery, never silently dropped) and that
re-processing the same episode afterward is a real no-op, not a second
attempt.
"""

from __future__ import annotations

import hashlib
import hmac
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from src.attribute.ledger import get_ledger_total
from src.audit.writer import AuditWriter
from src.choose.policy import PolicyEngine
from src.choose.selector import ActionSelector
from src.config import Settings
from src.db.repo import get_episode_by_payment_id, insert_customer_if_absent
from src.diagnose.baseline import RegexBaseline
from src.diagnose.cache import DiskCache
from src.diagnose.classifier import Diagnoser
from src.execute.executor import RazorpayExecutor
from src.execute.faults import FaultInjectingExecutor, FaultPlan
from src.gate.checks import Episode
from src.ingest.app import app, drain
from src.runner import Runner

pytestmark = pytest.mark.e2e

FIXTURES = Path(__file__).resolve().parent.parent.parent / "fixtures" / "webhooks"
WEBHOOK_SECRET = "e2e_failure_path_secret"
PAYMENT_ID = "pay_TU6NMPiyJVkobn"


def _sign(body: bytes) -> str:
    return hmac.new(WEBHOOK_SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _post_webhook(client: TestClient, body: bytes, event_id: str):
    headers = {
        "Content-Type": "application/json",
        "X-Razorpay-Signature": _sign(body),
        "X-Razorpay-Event-Id": event_id,
    }
    response = client.post("/webhooks/razorpay", content=body, headers=headers)
    drain()
    return response


def test_retry_exhaustion_is_accounted_and_a_rerun_is_a_true_no_op(
    tmp_db, config_bundle, stub_llm, frozen_time, monkeypatch
):
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", WEBHOOK_SECRET)
    monkeypatch.setattr("src.execute.executor.time.sleep", lambda _s: None)
    conn = tmp_db.conn

    # -- ingest one episode, the same way the happy path does ---------------
    with TestClient(app) as client:
        body = (FIXTURES / "payment_failed.json").read_bytes()
        assert _post_webhook(client, body, "evt_e2e_fail_001").status_code == 200

    episode_row = get_episode_by_payment_id(conn, PAYMENT_ID)
    assert episode_row is not None
    # A live-ingested episode always has customer_id=None (see
    # src/ingest/service.py) — fine for FixtureExecutor (test_happy_path.py),
    # but RazorpayExecutor._build_link_payload() unconditionally slices
    # episode.customer_id (`episode.customer_id[:8]`), which crashes on None.
    # This test needs the real RazorpayExecutor (to exercise its actual
    # with_backoff() retry loop under the fault plan), so it gives the
    # episode a synthetic customer_id — a real gap in
    # RazorpayExecutor.create_recovery_link() worth its own fix, out of
    # scope for this test-consolidation pass.
    episode = Episode.model_validate({**dict(episode_row), "customer_id": "cust_e2e_fail_001"})
    insert_customer_if_absent(
        conn,
        customer_id=episode.customer_id,
        synthetic_name=None,
        contact_hash="hash_e2e_fail",
        email_hash=None,
        segment="repeat",
        opted_out=False,
        opt_out_ts=None,
        created_at=frozen_time.isoformat(timespec="seconds"),
    )

    # See test_happy_path.py's matching comment: Runner sources episodes
    # from JSONL, never from the `episode` table ingest already wrote to, so
    # bridging the two by hand means clearing ingest's own row first.
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("DELETE FROM episode WHERE episode_id = ?", (episode.episode_id,))
    conn.commit()
    conn.execute("PRAGMA foreign_keys=ON")

    settings = Settings(llm_model="stub-model")
    diagnoser = Diagnoser(
        RegexBaseline(config_bundle.taxonomy),
        stub_llm(class_id="C8"),
        DiskCache(tmp_db.db_path.parent / "cache_diagnose"),
        config_bundle.taxonomy,
        settings,
    )
    policy_engine = PolicyEngine(config_bundle.policy)
    selector = ActionSelector(
        stub_llm(class_id="C8"), DiskCache(tmp_db.db_path.parent / "cache_choose"), settings
    )

    mock_client = Mock()
    mock_client.create_payment_link_once.side_effect = AssertionError(
        "the fault plan's 3 scripted 429s should exhaust the retry cap (3) "
        "before ever falling through to the real client"
    )
    wrapped = RazorpayExecutor(conn=conn, client=mock_client, mode="execute", run_id="e2e_fail_run")
    fault_executor = FaultInjectingExecutor(wrapped, FaultPlan(inject_429_at_episode_index=1))
    fault_executor.start_run(episode_count=1)

    run_id = "e2e_fail_run"
    writer = AuditWriter(run_id, tmp_db.audit_dir, conn)
    runner = Runner(
        conn,
        writer,
        config_bundle,
        settings,
        diagnoser=diagnoser,
        policy_engine=policy_engine,
        selector=selector,
        executor=fault_executor,
    )
    summary = runner.run([episode], "execute", run_id=run_id)
    writer.close()

    # -- execution_failed, exception_entry, RunSummary invariant ------------
    assert summary.by_outcome.get("execution_failed") == 1
    assert summary.by_outcome.get("actioned", 0) == 0
    assert summary.episode_count == 1
    outcome_keys = ("actioned", "suppressed", "execution_failed", "pending")
    total = sum(summary.by_outcome.get(k, 0) for k in outcome_keys)
    assert total == summary.episode_count

    exception_row = conn.execute(
        "SELECT * FROM exception_entry WHERE run_id = ? AND episode_id = ?",
        (run_id, episode.episode_id),
    ).fetchone()
    assert exception_row is not None
    assert exception_row["reason_code"] == "executor_retry_exhausted"
    assert exception_row["stage"] == "execute"

    # -- the episode's amount is NOT in gross_recovery -----------------------
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM execution WHERE episode_id = ? AND status = 'created'",
        (episode.episode_id,),
    ).fetchone()["n"] == 0
    assert get_ledger_total(conn, run_id, "gross_recovery") == 0

    # the retry-exhaustion path DOES record the attempt as "failed" (unlike
    # a simulated/no-backend fault) — confirms accounting isn't silently
    # dropping the episode, it's recorded as a real, failed attempt.
    failed_row = conn.execute(
        "SELECT * FROM execution WHERE episode_id = ? AND status = 'failed'",
        (episode.episode_id,),
    ).fetchone()
    assert failed_row is not None
    idempotency_key = failed_row["idempotency_key"]
    decision_row = conn.execute(
        "SELECT policy_rule_id, chosen_action FROM decision WHERE episode_id = ?",
        (episode.episode_id,),
    ).fetchone()
    assert decision_row is not None

    # -- a re-run produces duplicate_suppressed with zero new executions ----
    mock_client_rerun = Mock()
    mock_client_rerun.create_payment_link_once.side_effect = AssertionError(
        "a re-run over an idempotency_key already on file must never touch the network again"
    )
    rerun_executor = RazorpayExecutor(
        conn=conn, client=mock_client_rerun, mode="execute", run_id=run_id
    )

    result = rerun_executor.create_recovery_link(
        episode=episode,
        action=decision_row["chosen_action"],
        policy_rule_id=decision_row["policy_rule_id"],
        run_id=run_id,
    )

    assert result.status == "duplicate_suppressed"
    assert result.idempotency_key == idempotency_key
    mock_client_rerun.create_payment_link_once.assert_not_called()
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM execution WHERE episode_id = ?", (episode.episode_id,)
    ).fetchone()["n"] == 1
