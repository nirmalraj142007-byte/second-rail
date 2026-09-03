"""The full episode arc, in-process, as one test.

This is the executable version of the demo script. If it passes, the demo
works: a real webhook comes in, gets diagnosed and actioned, and the
resulting Payment Link's `paid` outcome gets attributed back to a net
recovery figure — with a hash-chained audit trail tying every stage of that
one episode together under a single correlating id (`episode_id`), from the
moment the webhook was received to the moment its outcome was posted to the
ledger.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.attribute.ledger import compute_fp_cost, parse_outcome_assumptions, post_gross, post_net
from src.attribute.watcher import OutcomeWatcher
from src.audit.verify import verify_chain
from src.audit.writer import AuditWriter
from src.choose.policy import PolicyEngine
from src.choose.selector import ActionSelector
from src.config import Settings
from src.db.repo import get_episode_by_payment_id, get_ledger_total
from src.diagnose.baseline import RegexBaseline
from src.diagnose.cache import DiskCache
from src.diagnose.classifier import Diagnoser
from src.execute.executor import FixtureExecutor
from src.gate.checks import Episode
from src.ingest.app import app, drain
from src.runner import Runner

pytestmark = pytest.mark.e2e

FIXTURES = Path(__file__).resolve().parent.parent.parent / "fixtures" / "webhooks"
WEBHOOK_SECRET = "e2e_happy_path_secret"
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


def _paid_webhook_body(
    *, plink_id: str, order_id: str, amount_paise: int, paid_epoch: int
) -> bytes:
    template = json.loads((FIXTURES / "payment_link_paid.json").read_text(encoding="utf-8"))
    template["payload"]["payment_link"]["entity"]["id"] = plink_id
    template["payload"]["payment_link"]["entity"]["order_id"] = order_id
    template["payload"]["payment_link"]["entity"]["amount"] = amount_paise
    template["payload"]["payment"]["entity"]["order_id"] = order_id
    template["payload"]["payment"]["entity"]["amount"] = amount_paise
    # created_at drives the recovered outcome's `occurred_at` (see
    # src/attribute/watcher.py's `_payment_link_outcome_event`) — set it a
    # couple of hours after the link's own creation, inside the fixture's
    # hard-coded value, so this reads as "customer paid 2h later," not
    # "paid before the link existed."
    template["payload"]["payment"]["entity"]["created_at"] = paid_epoch
    template["payload"]["order"]["entity"]["id"] = order_id
    template["payload"]["order"]["entity"]["amount"] = amount_paise
    return json.dumps(template).encode("utf-8")


def _records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def test_full_episode_arc_end_to_end(tmp_db, config_bundle, stub_llm, frozen_time, monkeypatch):
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", WEBHOOK_SECRET)
    conn = tmp_db.conn

    # -- 1. POST a signed payment.failed fixture to the TestClient ----------
    with TestClient(app) as client:
        body = (FIXTURES / "payment_failed.json").read_bytes()
        response = _post_webhook(client, body, "evt_e2e_happy_001")
        assert response.status_code == 200

    # -- 2. assert one episode created ---------------------------------------
    episode_row = get_episode_by_payment_id(conn, PAYMENT_ID)
    assert episode_row is not None
    assert conn.execute("SELECT COUNT(*) AS n FROM episode").fetchone()["n"] == 1
    episode = Episode.model_validate(dict(episode_row))

    # src/ingest/ and src/runner.py's Runner each independently guard against
    # re-processing the same payment_id (episode.UNIQUE(payment_id) at
    # ingest; GateEngine's check_duplicate for a batch pass) — but nothing
    # in this codebase currently wires "a webhook just created this episode"
    # into "now hand it to Runner.run()" live; docs/out-of-scope.md is
    # explicit that batch replay over JSONL, not live processing, is the
    # implemented shape (see its "Real-time streaming infrastructure"
    # entry). Runner sources episodes from data/*.jsonl, never from the
    # `episode` table ingest already wrote to — so simulating the two
    # stages back-to-back against one database means clearing ingest's own
    # row first, or Runner's check_duplicate (correctly, by its own rules)
    # refuses an episode whose payment_id is already on file. This is the
    # one seam this test bridges by hand; everything after it exercises
    # real, wired-together code. Ingest's own audit_record row (mirroring
    # ingest.jsonl) still references this episode_id via a foreign key —
    # toggled off only for this one DELETE, so that record (needed for the
    # correlating-id check below) survives the episode row it points at.
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("DELETE FROM episode WHERE episode_id = ?", (episode.episode_id,))
    conn.commit()
    conn.execute("PRAGMA foreign_keys=ON")

    # -- 3. run Runner with fixture executor + stub LLM over that episode ---
    run_id = "e2e_happy_run"
    audit = AuditWriter(run_id, tmp_db.audit_dir, conn)
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
    executor = FixtureExecutor(fixture_dir=tmp_db.db_path.parent / "link_fixtures", conn=conn)

    runner = Runner(
        conn,
        audit,
        config_bundle,
        settings,
        diagnoser=diagnoser,
        policy_engine=policy_engine,
        selector=selector,
        executor=executor,
    )
    summary = runner.run([episode], "dry_run", run_id=run_id)

    # -- 4a. gate passed all 7 checks ----------------------------------------
    gate_checks = conn.execute(
        "SELECT check_name, result FROM gate_check WHERE episode_id = ? ORDER BY order_index",
        (episode.episode_id,),
    ).fetchall()
    assert [c["check_name"] for c in gate_checks] == [
        "duplicate", "terminal_seen", "opt_out", "episode_age",
        "amount_cap", "frequency_cap", "quiet_hours",
    ]
    assert all(c["result"] == "pass" for c in gate_checks)

    run_records = _records(tmp_db.audit_dir / f"{run_id}.jsonl")
    episode_records = [r for r in run_records if r["episode_id"] == episode.episode_id]

    # -- 4b. diagnosis recorded ----------------------------------------------
    diagnose_records = [r for r in episode_records if r["stage"] == "diagnose"]
    assert len(diagnose_records) == 1
    assert diagnose_records[0]["llm"]["model"] == "stub-model"

    # -- 4c. admissible set of <= 3, chosen action inside it -----------------
    decision_row = conn.execute(
        "SELECT * FROM decision WHERE episode_id = ?", (episode.episode_id,)
    ).fetchone()
    assert decision_row is not None
    candidate_actions = json.loads(decision_row["candidate_actions"])
    assert 1 <= len(candidate_actions) <= 3
    assert decision_row["chosen_action"] in candidate_actions
    assert bool(decision_row["inside_admissible_set"]) is True
    assert summary.admissibility_rate == 1.0

    # -- 4d. execution row with an idempotency key ---------------------------
    execution_row = conn.execute(
        "SELECT * FROM execution WHERE episode_id = ?", (episode.episode_id,)
    ).fetchone()
    assert execution_row is not None
    assert execution_row["status"] == "created"
    assert execution_row["idempotency_key"]
    assert len(execution_row["idempotency_key"]) == 32

    # -- 4e. audit chain records for this episode, correct stage sequence ---
    # gate, diagnose, choose, execute — the 5th (attribute) lands after step
    # 6 below, once a real outcome exists to attribute; this episode never
    # goes through "approve" (ui=None — no human_keystroke gating, see
    # src/runner.py's __init__ docstring) or a retry, so 5 is this episode's
    # real ceiling, not an arbitrarily chosen minimum.
    stage_sequence = [r["stage"] for r in episode_records]
    assert stage_sequence == ["gate", "diagnose", "choose", "execute"]
    # every record's own seq is monotonically increasing within this file
    all_seqs = [r["seq"] for r in run_records]
    assert all_seqs == sorted(all_seqs)

    # -- 5. deliver a payment_link.paid fixture inside the window ------------
    order_id = episode_row["order_id"]
    paid_at = frozen_time + timedelta(hours=2)
    paid_body = _paid_webhook_body(
        plink_id=execution_row["plink_id"],
        order_id=order_id,
        amount_paise=episode.amount_paise,
        paid_epoch=int(paid_at.timestamp()),
    )
    with TestClient(app) as client:
        paid_response = _post_webhook(client, paid_body, "evt_e2e_happy_paid_001")
        assert paid_response.status_code == 200

    # -- 6. attribution recovered, exact gross ledger entry, net computed ---
    watcher = OutcomeWatcher(window_hours=config_bundle.guardrails.attribution_window_hours)
    attributions = watcher.from_webhooks(conn, run_id)
    assert len(attributions) == 1
    attribution = attributions[0]
    assert attribution.episode_id == episode.episode_id
    assert attribution.outcome == "recovered"
    assert attribution.recovered_amount_paise == episode.amount_paise

    # Same shape as src/runner.py's own attribution block — appended here,
    # on the still-open writer from step 3, rather than through
    # Runner.run()'s attributor= parameter, because the real outcome this
    # attribution reports on didn't exist until the webhook in step 5
    # arrived, after that run() call had already returned.
    audit.append(
        stage="attribute",
        actor="system",
        episode_id=attribution.episode_id,
        outcome=attribution.outcome,
        rationale=f"{attribution.attribution_rule_id}: {attribution.reason_code}",
    )

    post_gross(conn, run_id, attribution)
    assert get_ledger_total(conn, run_id, "gross_recovery") == episode.amount_paise

    assumptions = parse_outcome_assumptions()
    fp = compute_fp_cost(conn, run_id, assumptions)
    assert fp.fp_count == 0  # the only contact this run made was a genuine recovery

    net_paise = post_net(conn, run_id)
    assert net_paise == episode.amount_paise
    audit.close()

    # -- 7. verify_chain intact ------------------------------------------------
    final_records = _records(tmp_db.audit_dir / f"{run_id}.jsonl")
    final_episode_records = [r for r in final_records if r["episode_id"] == episode.episode_id]
    assert [r["stage"] for r in final_episode_records] == [
        "gate", "diagnose", "choose", "execute", "attribute",
    ]

    result = verify_chain(tmp_db.audit_dir / f"{run_id}.jsonl")
    assert result.intact
    assert result.count == len(final_records)

    # -- extra: a single correlating id threads from webhook receipt through
    #    to the final audit record for this episode, verified with a real
    #    trace rather than assumed. The webhook receiver has no run_id (it's
    #    a long-lived process, not a batch run — see AuditWriter's module
    #    docstring), so its own trail lives in ingest.jsonl; the episode_id
    #    it minted at ingest time is the thread tying that file to every
    #    later gate/diagnose/choose/execute/attribute record in the run's
    #    own file.
    ingest_records = _records(tmp_db.audit_dir / "ingest.jsonl")
    ingest_hits = [r for r in ingest_records if r.get("episode_id") == episode.episode_id]
    assert ingest_hits, "webhook receipt never wrote an audit record naming this episode_id"
    assert ingest_hits[0]["outcome"] == "new_episode"
    stages_seen = {r["stage"] for r in final_episode_records}
    assert {"gate", "diagnose", "choose", "execute", "attribute"} <= stages_seen
