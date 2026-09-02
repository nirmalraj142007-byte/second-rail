"""Demo orchestrator — end-to-end run with the executor.

`make demo` loads episodes, runs the gate + executor loop, and displays
results live. Default mode is dry-run (zero external calls); --execute
requires an explicit flag for real Razorpay calls.

Usage:
  python -m scripts.demo                      # dry-run, zero network calls
  python -m scripts.demo --execute            # real Razorpay calls (test mode)
  python -m scripts.demo --execute --limit 3  # run 3 episodes, real calls
"""

from __future__ import annotations

import contextlib
import io
from pathlib import Path

import typer
from rich.console import Console

from src.audit.writer import AuditWriter
from src.choose.policy import PolicyEngine
from src.choose.selector import ActionSelector
from src.config import load_settings, require_razorpay
from src.config_models import config_hash, load_all
from src.db.migrate import get_connection, migrate
from src.diagnose.baseline import RegexBaseline
from src.diagnose.cache import DiskCache
from src.diagnose.classifier import Diagnoser
from src.diagnose.llm_client import build_llm_client
from src.execute.executor import RazorpayExecutor
from src.logging_setup import get_logger, setup_logging
from src.razorpay_client import RazorpayClient
from src.runner import load_and_upsert_customers, load_episodes, write_exceptions_sample
from src.ui.live import LiveRunView

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCES = (ROOT / "data" / "train.jsonl", ROOT / "holdout" / "sealed.jsonl")
DEFAULT_CUSTOMERS_PATH = ROOT / "data" / "customers.jsonl"
DEFAULT_EXCEPTIONS_SAMPLE_PATH = ROOT / "evidence" / "exceptions_sample.md"

logger = get_logger("demo")


def main(
    source: list[Path] | None = typer.Option(
        None, "--source", help="JSONL episode file(s); repeatable. Default: train + sealed."
    ),
    execute: bool = typer.Option(
        False, "--execute", help="Real Razorpay calls. Default: dry-run (zero network)."
    ),
    limit: int | None = typer.Option(
        None, "--limit", help="Limit number of episodes (for quick testing)."
    ),
) -> None:
    """Run the gate + executor loop with live output."""
    setup_logging()
    settings = load_settings()
    migrate(settings.db_path)
    conn = get_connection(settings.db_path)

    try:
        bundle = load_all()
        sources_list = list(source) if source else list(DEFAULT_SOURCES)
        episodes = load_episodes(sources_list)

        if limit:
            episodes = episodes[:limit]

        load_and_upsert_customers(conn, DEFAULT_CUSTOMERS_PATH)

        # Mode is "execute" if --execute flag is set, else "dry_run"
        mode = "execute" if execute else "dry_run"

        from ulid import ULID
        run_id = str(ULID())

        # --execute with no credentials fails loudly (ConfigError, uncaught)
        # rather than silently degrading to dry-run — a demo that quietly
        # skips real calls would misreport its own mode banner.
        client = None
        if mode == "execute":
            key_id, key_secret = require_razorpay(settings)
            client = RazorpayClient(key_id, key_secret)

        console = Console()
        view = LiveRunView(console, total=len(episodes))
        view.banner(
            run_id=run_id, mode=mode, config_hash=config_hash(bundle), seal_status=_seal_status()
        )

        # Run the full pipeline — diagnose + choose, not just gate + execute,
        # so escalation tier is actually policy-driven (src/choose/policy.py)
        # and the human_keystroke approval gate (src/runner.py) has
        # something real to gate. Same wiring pattern as
        # scripts/eval.py's run_second_rail().
        taxonomy = bundle.taxonomy
        baseline = RegexBaseline(taxonomy)
        cache = DiskCache(settings.cache_dir)
        llm = build_llm_client(settings)
        diagnoser = Diagnoser(baseline, llm, cache, taxonomy, settings)
        policy_engine = PolicyEngine(bundle.policy)
        selector = ActionSelector(llm, cache, settings)

        from src.runner import Runner
        audit = AuditWriter(run_id, settings.audit_dir, conn)
        try:
            executor = RazorpayExecutor(
                conn=conn,
                client=client,
                mode=mode,
                run_id=run_id,
                audit=audit,
                retry_cap=bundle.guardrails.executor_retry_cap,
                retry_delays=[float(s) for s in bundle.guardrails.executor_backoff_seconds],
                on_retry=view.retry,
                on_retry_exhausted=view.retry_exhausted,
            )
            runner = Runner(
                conn, audit, bundle, settings,
                diagnoser=diagnoser, policy_engine=policy_engine, selector=selector,
                executor=executor, ui=view,
            )
            runner.run(episodes, mode, run_id=run_id)
        finally:
            audit.close()
            if client is not None:
                client.close()

        write_exceptions_sample(conn, run_id, DEFAULT_EXCEPTIONS_SAMPLE_PATH)

    finally:
        conn.close()


def _seal_status() -> str:
    """'OK' / 'FAIL' / 'unsealed' for the run banner — a real check
    (scripts.seal.verify), not a decorative constant, with its own noisy
    stdout captured so the banner stays the one line the phase spec asks
    for. Never raises: an unsealed or missing holdout dir is a normal state
    for a fresh checkout that hasn't run `make seal` yet, not a crash."""
    from scripts.seal import SEAL_PATH, verify

    if not SEAL_PATH.exists():
        return "unsealed"
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        try:
            ok = verify() == 0
        except Exception:
            ok = False
    return "OK" if ok else "FAIL"


if __name__ == "__main__":
    typer.run(main)
