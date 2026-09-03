"""The same arc as test_happy_path.py, except the executor exhausts its
retry cap under an injected 429 — proving a failure is accounted for
honestly (excluded from gross recovery, never silently dropped) and that
re-processing the same episode afterward is a real no-op, not a second
attempt.
"""

from __future__ import annotations

import hashlib
import hmac
import json
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
from src.execute.executor import ExecutionResult, RazorpayExecutor
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


def _records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


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

    # -- that same re-submission, run through Runner, writes it to the audit
    #    log too -- not just the executor's own return value ---------------
    # RazorpayExecutor's real idempotency check (Step 1 of
    # create_recovery_link, exercised for real just above) never itself
    # calls the audit writer -- only Runner.run()'s per-episode wrapper
    # does (src/runner.py, the `else` branch after a successful executor
    # call). A genuine second Runner.run() pass over this same episode
    # would be suppressed at the gate's own `duplicate` check first (it's
    # payment_id-scoped against the `episode` table, not run-scoped), so
    # the only way to reach Runner's execute-stage wrapper with a
    # duplicate_suppressed result is to hand it an executor that reports
    # one -- exactly the return value just proven genuine above. This
    # isolates what this block actually verifies (does Runner write the
    # outcome it's given?) using the same executor-boundary-stub technique
    # this file already uses for the injected-429 fault plan.
    # Runner's own first pass above inserted an `episode` row for this
    # payment_id (required for the decision/gate_check/execution rows'
    # foreign keys) -- gate's own `duplicate` check would suppress this
    # second pass before it ever reached execute otherwise, same reason
    # this test bridges ingest -> Runner by hand near the top. `decision`
    # also carries UNIQUE(episode_id) (one decision per episode, ever) --
    # clearing it and `gate_check` too lets this second pass re-decide the
    # same episode_id rather than colliding on either.
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("DELETE FROM decision WHERE episode_id = ?", (episode.episode_id,))
    conn.execute("DELETE FROM gate_check WHERE episode_id = ?", (episode.episode_id,))
    conn.execute("DELETE FROM episode WHERE episode_id = ?", (episode.episode_id,))
    conn.commit()
    conn.execute("PRAGMA foreign_keys=ON")

    stub_executor = Mock()
    stub_executor.create_recovery_link.return_value = ExecutionResult(
        status="duplicate_suppressed",
        idempotency_key=idempotency_key,
        plink_id=failed_row["plink_id"],
        created_new=False,
    )
    dup_run_id = "e2e_fail_dup_run"
    dup_audit = AuditWriter(dup_run_id, tmp_db.audit_dir, conn)
    dup_runner = Runner(
        conn, dup_audit, config_bundle, settings,
        diagnoser=diagnoser, policy_engine=policy_engine, selector=selector,
        executor=stub_executor,
    )
    dup_runner.run([episode], "dry_run", run_id=dup_run_id)
    dup_audit.close()

    stub_executor.create_recovery_link.assert_called_once()
    dup_records = _records(tmp_db.audit_dir / f"{dup_run_id}.jsonl")
    dup_outcomes = [r.get("outcome") for r in dup_records if r.get("episode_id") == episode.episode_id]
    assert "duplicate_suppressed" in dup_outcomes, (
        f"Runner did not write duplicate_suppressed to the audit log; saw {dup_outcomes}"
    )
