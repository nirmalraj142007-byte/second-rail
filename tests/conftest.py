"""Shared fixtures for the whole suite.

Hermetic by construction: every fixture here either lives entirely under
`tmp_path`, or reads `config/` (real, read-only, small, and exactly what
CLAUDE.md says every threshold must live in) — nothing here opens a real
network socket, reads the real `second_rail.db`, or depends on the current
wall clock unless a test opts into `frozen_time`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from freezegun import freeze_time

from src.audit.writer import AuditWriter
from src.config_models import ConfigBundle, load_all
from src.diagnose.llm_client import LLMResponse
from src.execute.executor import FixtureExecutor
from src.gate.checks import Episode

IST = ZoneInfo("Asia/Kolkata")
REPO_ROOT = Path(__file__).resolve().parent.parent

# 10:30 IST, one day after the `fixtures/webhooks/*.json` payloads' own
# embedded `created_at` (2026-08-25 23:59:30 IST — see the fixture files) —
# close enough that episode_age never trips (well under the 72h cap), and
# 10:30 sits outside quiet_hours (21:00-09:00) by construction, so any test
# using this fixture never has to reason about either check separately.
FROZEN_AT_ISO = "2026-08-26T10:30:00+05:30"


# ---------------------------------------------------------------------------
# database / audit
# ---------------------------------------------------------------------------


@dataclass
class TmpDb:
    conn: object
    db_path: Path
    audit_dir: Path


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """A freshly migrated SQLite database under tmp_path. Also exports
    DB_PATH / AUDIT_DIR as environment variables so any code that calls
    `load_settings()` internally (the ingest FastAPI app, `scripts.demo`,
    `src.runner`'s CLI) resolves to this same temporary database rather than
    the real `second_rail.db`."""
    from src.db.migrate import get_connection, migrate

    db_path = tmp_path / "second_rail.db"
    audit_dir = tmp_path / "audit"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("AUDIT_DIR", str(audit_dir))

    migrate(db_path)
    conn = get_connection(db_path)
    yield TmpDb(conn=conn, db_path=db_path, audit_dir=audit_dir)
    conn.close()


@pytest.fixture
def audit_writer(tmp_db):
    """A factory: `audit_writer(run_id="...")` returns an AuditWriter bound
    to `tmp_db`, writing into `tmp_db.audit_dir`. Every writer this factory
    hands out is closed automatically at teardown."""
    writers: list[AuditWriter] = []

    def _make(run_id: str | None = "test_run") -> AuditWriter:
        writer = AuditWriter(run_id, tmp_db.audit_dir, tmp_db.conn)
        writers.append(writer)
        return writer

    yield _make
    for writer in writers:
        writer.close()


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


@pytest.fixture
def config_bundle() -> ConfigBundle:
    """The real, committed config/ — taxonomy + policy_table + guardrails.
    Reading this directory is explicitly allowed outside tmp_path: it is the
    thing CLAUDE.md's non-negotiables say every money-adjacent threshold
    must live in, so a test that doesn't load the real file isn't actually
    testing the config that ships."""
    return load_all(REPO_ROOT / "config")


class FakeSettings:
    """A `Settings`-shaped stand-in carrying only what Diagnoser/
    ActionSelector actually read off it (`llm_model`, via `getattr(...,
    "unknown-model")`) — avoids constructing a real `Settings()` (which
    reads `.env`/environment) when a test only needs the one attribute."""

    llm_model = "stub-model"


# ---------------------------------------------------------------------------
# LLM stub
# ---------------------------------------------------------------------------


def _hash_prompt(model: str, prompt: str) -> str:
    return sha256(f"{model}:{prompt}".encode()).hexdigest()


class StubLLMClient:
    """Deterministic stand-in for `src.diagnose.llm_client.LLMClient`.

    Inspects the `json_schema` it's asked to answer against to decide
    whether it's being called from `src/diagnose/classifier.py` (schema has
    a `class_id` property) or `src/choose/selector.py` (schema has a
    `chosen_action` property), and returns a valid response for whichever
    one it is — so a single stub works for both stages without the caller
    having to know which is which. Always answers inside the schema's own
    `enum` where one is given (choose), which is what keeps every choice
    inside the admissible set.

    `fixture_map` optionally maps an exact prompt string to a canned raw
    JSON response, for a test that needs a specific, named answer for a
    specific episode rather than the generic deterministic default.
    """

    def __init__(
        self,
        *,
        class_id: str = "C8",
        confidence: float = 0.42,
        chosen_action: str | None = None,
        fixture_map: dict[str, str] | None = None,
        model: str = "stub-model",
    ) -> None:
        self._class_id = class_id
        self._confidence = confidence
        self._chosen_action = chosen_action
        self._fixture_map = dict(fixture_map or {})
        self._model = model
        self.calls = 0
        self.prompts: list[str] = []

    def complete(
        self, prompt: str, *, max_tokens: int, temperature: float, json_schema: dict
    ) -> LLMResponse:
        self.calls += 1
        self.prompts.append(prompt)

        if prompt in self._fixture_map:
            text = self._fixture_map[prompt]
        elif "chosen_action" in json_schema.get("properties", {}):
            enum = json_schema["properties"]["chosen_action"]["enum"]
            action = self._chosen_action if self._chosen_action in enum else enum[0]
            text = json.dumps(
                {
                    "chosen_action": action,
                    "features_used": ["error_code", "amount_band"],
                    "rationale": "stub_llm: deterministic selection for tests",
                    "copy_customer_facing": (
                        "We noticed your last payment didn't go through — here's a "
                        "fresh link to try again."
                    ),
                }
            )
        else:
            text = json.dumps(
                {
                    "class_id": self._class_id,
                    "confidence": self._confidence,
                    "rationale": "stub_llm: deterministic classification for tests",
                    "features_used": ["error_reason"],
                }
            )

        return LLMResponse(
            text=text,
            model=self._model,
            prompt_hash=_hash_prompt(self._model, prompt),
            input_tokens=10,
            output_tokens=10,
            cost_paise=0,
            latency_ms=0,
            cache_hit=False,
        )


@pytest.fixture
def stub_llm():
    """Factory fixture: `stub_llm(class_id="C1")` or
    `stub_llm(chosen_action="defer_2h")` returns a fresh `StubLLMClient`."""

    def _make(**kwargs) -> StubLLMClient:
        return StubLLMClient(**kwargs)

    return _make


# ---------------------------------------------------------------------------
# executor
# ---------------------------------------------------------------------------


@pytest.fixture
def fixture_executor(tmp_path, tmp_db):
    """A no-network `FixtureExecutor`, wired to `tmp_db`'s connection so its
    `created` executions are actually persisted and queryable."""
    return FixtureExecutor(fixture_dir=tmp_path / "link_fixtures", conn=tmp_db.conn)


# ---------------------------------------------------------------------------
# time
# ---------------------------------------------------------------------------


@pytest.fixture
def frozen_time():
    """Freezes the wall clock at 10:30 IST — deliberately outside quiet
    hours — for the duration of the test. Yields the frozen `datetime` so a
    test can build episodes relative to it without hard-coding the string
    twice."""
    with freeze_time(FROZEN_AT_ISO):
        yield datetime.fromisoformat(FROZEN_AT_ISO)


# ---------------------------------------------------------------------------
# episodes
# ---------------------------------------------------------------------------


def _make_episode(**overrides) -> Episode:
    base = dict(
        episode_id="epi_sample_001",
        payment_id="pay_sample_001",
        customer_id="cust_sample_001",
        amount_paise=85000,
        instrument="upi",
        segment="repeat",
        issuer_family="BANK_A",
        error_code="BAD_REQUEST_ERROR",
        error_description="Payment failed",
        error_reason="insufficient_fund",
        failed_at=FROZEN_AT_ISO,
        received_at=FROZEN_AT_ISO,
        split="train",
        is_synthetic=True,
    )
    base.update(overrides)
    return Episode.model_validate(base)


@pytest.fixture
def sample_episodes() -> list[Episode]:
    """A handful of ready-made, gate-eligible episodes spanning a few
    amount bands / instruments / cause strings — for tests that need real
    Episode objects but don't care about the specific scenario."""
    return [
        _make_episode(
            episode_id="epi_sample_001",
            payment_id="pay_sample_001",
            customer_id="cust_sample_001",
            amount_paise=85000,
            instrument="upi",
            segment="repeat",
            error_reason="insufficient_fund",
        ),
        _make_episode(
            episode_id="epi_sample_002",
            payment_id="pay_sample_002",
            customer_id="cust_sample_002",
            amount_paise=300000,
            instrument="card",
            segment="high_value",
            error_reason="card_declined",
        ),
        _make_episode(
            episode_id="epi_sample_003",
            payment_id="pay_sample_003",
            customer_id="cust_sample_003",
            amount_paise=650000,
            instrument="netbanking",
            segment="first_time",
            error_reason="payment_timed_out",
        ),
    ]
