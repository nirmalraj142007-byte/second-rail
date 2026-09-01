"""Phase 14, experiment 3 — `executor_retry_cap`.

QUESTION: `config/guardrails.yaml` currently sets this to 3 with a
`# TODO justify` comment. Every executor call that hits a retryable
HTTP status (429 or 5xx) gets `executor_retry_cap` attempts, spaced by
`executor_backoff_seconds` ([1, 2, 4] seconds, repeating the last value
for any attempt beyond the configured list — see `src.execute.retry`),
before the episode is abandoned to the exception list
(`reason_code="executor_retry_exhausted"`, `src/runner.py`). Raise the
cap and more transient 429s eventually succeed; raise it too far and a
genuinely dead episode burns an increasing amount of real API budget
and wall-clock time before finally giving up.

METHOD: sweep `executor_retry_cap` over {1, 3, 5, 10}. At each setting,
run 15 synthetic episodes through the real `src.execute.executor.
RazorpayExecutor` — the same class `src/runner.py` calls in "execute"
mode, not a re-implementation — each wrapped around a scripted client
that returns HTTP 429 for the first `k` calls, then a normal 200. `k` is
drawn from a fixed, disclosed distribution (`K_DISTRIBUTION` below):
mostly small values (modelling a transient rate limit that clears fast)
with a long tail (modelling an issuer outage that does not clear inside
any reasonable retry budget). This exercises the real `with_backoff()`
retry loop (`src/execute/retry.py`) and the real
`RazorpayExecutor.create_recovery_link()` control flow — cap exhaustion
raises the real `ExecutorError` production code raises — against a
scripted, deterministic fault instead of the live network, so the sweep
is fast and reproducible.

`time.sleep` is monkeypatched to a no-op accumulator for the duration of
this script only (restored in a `finally`): the reported "modeled
wall-clock time" is the sum of the exact backoff delay VALUES
`with_backoff()`'s real `on_attempt` callback computes and would sleep
for in production — nothing about the retry/backoff arithmetic is
simulated, only the literal `time.sleep()` block is skipped so a
15-episode x 4-setting sweep with tail values up to k=25 doesn't cost
several real minutes.

Writes:
  experiments/thresholds/results_retry_cap.json
  experiments/thresholds/charts/retry_cap.png
  experiments/thresholds/retry_cap.md
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from statistics import mean
from zoneinfo import ZoneInfo

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402  (backend must be set first)

import src.execute.executor as executor_mod  # noqa: E402
from src.config_models import load_all  # noqa: E402
from src.db.migrate import get_connection, migrate  # noqa: E402
from src.db.repo import insert_episode, start_run  # noqa: E402
from src.errors import ExecutorError  # noqa: E402
from src.execute.executor import RazorpayExecutor  # noqa: E402
from src.gate.checks import Episode  # noqa: E402

OUT_DIR = Path(__file__).resolve().parent
SWEEP: list[int] = [1, 3, 5, 10]
CURRENT_DEFAULT = 3
IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 8, 26, 12, 0, 0, tzinfo=IST)

# Number of forced-429 responses before the scripted client starts
# returning 200, one value per synthetic episode. Skewed toward small
# values (a rate limit that clears within a couple of seconds — the
# common case) with a long tail (an issuer outage that outlasts any of
# the four swept caps) — calibrated so {1, 3, 5, 10} each land in a
# genuinely different place on the recovered/abandoned split, rather
# than two settings tying by construction.
K_DISTRIBUTION: list[int] = [0, 0, 1, 1, 2, 3, 4, 5, 6, 8, 10, 12, 15, 20, 25]


class _FailNTimesClient:
    """Returns HTTP 429 for the first `fail_count` calls, then 200 —
    duck-types the same two methods `src.razorpay_client.RazorpayClient`
    exposes, so `RazorpayExecutor` cannot tell this from the real thing."""

    def __init__(self, fail_count: int) -> None:
        self._remaining = fail_count
        self.calls = 0

    def create_payment_link_once(self, payload: dict, reference_id: str) -> tuple[int, dict]:
        self.calls += 1
        if self._remaining > 0:
            self._remaining -= 1
            return 429, {"error": {"description": "modeled 429 (retry-cap experiment)"}}
        return 200, {"id": f"plink_exp_{reference_id[:16]}", "short_url": "https://rzp.io/exp"}

    def cancel_payment_link(self, plink_id: str) -> dict:
        return {"id": plink_id, "status": "cancelled"}


class _AccumulatingSleep:
    """Replaces `time.sleep` for the duration of this script — records the
    requested duration instead of actually blocking. See module docstring
    for why this is disclosed as "modeled", not measured, wall-clock time."""

    def __init__(self) -> None:
        self.total = 0.0

    def __call__(self, seconds: float) -> None:
        self.total += seconds


def _make_episode(idx: int, k: int) -> Episode:
    return Episode(
        episode_id=f"exp_retry_{idx:02d}_k{k}",
        payment_id=f"pay_exp_retry_{idx:02d}_k{k}",
        customer_id=f"cust_exp_retry_{idx:02d}",
        amount_paise=100_000,
        failed_at=NOW,
        received_at=NOW,
    )


@dataclass
class RunResult:
    retry_cap: int
    recovered: int
    abandoned: int
    total_episodes: int
    total_api_calls: int
    wasted_calls_on_abandoned_episodes: int
    total_modeled_wall_clock_seconds: float
    mean_seconds_to_abandon: float
    max_k_recovered: int | None

    def as_dict(self) -> dict:
        return {
            "executor_retry_cap": self.retry_cap,
            "recovered": self.recovered,
            "abandoned": self.abandoned,
            "total_episodes": self.total_episodes,
            "total_api_calls": self.total_api_calls,
            "wasted_calls_on_abandoned_episodes": self.wasted_calls_on_abandoned_episodes,
            "total_modeled_wall_clock_seconds": round(self.total_modeled_wall_clock_seconds, 1),
            "mean_seconds_to_abandon": round(self.mean_seconds_to_abandon, 1),
            "max_k_recovered": self.max_k_recovered,
        }


def run_one(retry_cap: int, backoff_seconds: list[float]) -> RunResult:
    tmp_dir = tempfile.mkdtemp(prefix="second_rail_retry_cap_sweep_")
    db_path = Path(tmp_dir) / "sweep.db"
    migrate(db_path)
    conn = get_connection(db_path)

    fast_sleep = _AccumulatingSleep()
    original_sleep = executor_mod.time.sleep
    executor_mod.time.sleep = fast_sleep  # patches the real stdlib time.sleep for this process

    # execution.run_id carries a FOREIGN KEY REFERENCES run(run_id)
    # (src/db/schema.sql) — a real Runner.run() always calls start_run()
    # first; without a matching row here, every insert_execution() call
    # below fails on that FK, which src/db/repo.py's insert_execution()
    # then misreports as an IdempotencyCollision (it catches
    # sqlite3.IntegrityError broadly, not just the UNIQUE violation),
    # producing confusing "collision" log noise for a constraint that was
    # never actually violated.
    start_run(
        conn, run_id="exp-retry-cap", started_at=NOW.isoformat(timespec="seconds"),
        mode="execute",
    )

    recovered = 0
    abandoned = 0
    total_calls = 0
    wasted_calls = 0
    recovered_ks: list[int] = []
    abandoned_delays: list[float] = []
    all_delays: list[float] = []

    try:
        for idx, k in enumerate(K_DISTRIBUTION):
            client = _FailNTimesClient(fail_count=k)
            executor = RazorpayExecutor(
                conn=conn, client=client, mode="execute", run_id="exp-retry-cap",
                retry_cap=retry_cap, retry_delays=backoff_seconds,
            )
            episode = _make_episode(idx, k)
            # execution.episode_id also carries a FOREIGN KEY REFERENCES
            # episode(episode_id) — same reasoning as the run_id FK above.
            insert_episode(
                conn, episode_id=episode.episode_id, payment_id=episode.payment_id,
                order_id=None, customer_id=None, amount_paise=episode.amount_paise,
                currency="INR", instrument=None, issuer_family=None, error_code=None,
                error_description=None, error_source=None, error_step=None, error_reason=None,
                failed_at=episode.failed_at.isoformat(timespec="seconds"),
                received_at=episode.received_at.isoformat(timespec="seconds"),
                split=None, is_synthetic=True, harvested_from=None,
            )
            fast_sleep.total = 0.0
            try:
                executor.create_recovery_link(
                    episode=episode, action="placeholder_action",
                    policy_rule_id="EXP-RETRY-CAP", run_id="exp-retry-cap",
                )
                recovered += 1
                recovered_ks.append(k)
            except ExecutorError:
                abandoned += 1
                abandoned_delays.append(fast_sleep.total)
                wasted_calls += client.calls
            total_calls += client.calls
            all_delays.append(fast_sleep.total)
    finally:
        executor_mod.time.sleep = original_sleep
        conn.close()

    return RunResult(
        retry_cap=retry_cap,
        recovered=recovered,
        abandoned=abandoned,
        total_episodes=len(K_DISTRIBUTION),
        total_api_calls=total_calls,
        wasted_calls_on_abandoned_episodes=wasted_calls,
        total_modeled_wall_clock_seconds=sum(all_delays),
        mean_seconds_to_abandon=mean(abandoned_delays) if abandoned_delays else 0.0,
        max_k_recovered=max(recovered_ks) if recovered_ks else None,
    )


def render_chart(results: list[RunResult]) -> Path:
    caps = [r.retry_cap for r in results]
    recovered = [r.recovered for r in results]
    abandoned = [r.abandoned for r in results]
    wasted = [r.wasted_calls_on_abandoned_episodes for r in results]

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11, 4.5))

    ax1.bar([str(c) for c in caps], recovered, label="recovered", color="#55A868")
    ax1.bar([str(c) for c in caps], abandoned, bottom=recovered, label="abandoned", color="#C44E52")
    ax1.set_xlabel("executor_retry_cap")
    ax1.set_ylabel("episodes (of 15)")
    ax1.set_title("recovered vs. abandoned")
    ax1.legend()

    ax2.plot([str(c) for c in caps], wasted, marker="o", color="#8172B2")
    ax2.set_xlabel("executor_retry_cap")
    ax2.set_ylabel("API calls spent on episodes\nthat were abandoned anyway")
    ax2.set_title("wasted API calls")

    fig.suptitle("executor_retry_cap sweep — fault-injection rig, 15 scripted episodes")
    fig.tight_layout()

    charts_dir = OUT_DIR / "charts"
    charts_dir.mkdir(parents=True, exist_ok=True)
    path = charts_dir / "retry_cap.png"
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return path


def render_md(results: list[RunResult]) -> str:
    by_cap = {r.retry_cap: r for r in results}
    r3 = by_cap[CURRENT_DEFAULT]
    r10 = by_cap[10]

    conclusion = (
        f"Between cap={CURRENT_DEFAULT} and cap=10: recovered episodes go from "
        f"{r3.recovered}/15 to {r10.recovered}/15, and abandoned goes from {r3.abandoned}/15 to "
        f"{r10.abandoned}/15 — but the API calls spent on episodes that were abandoned anyway "
        f"rises from {r3.wasted_calls_on_abandoned_episodes} to "
        f"{r10.wasted_calls_on_abandoned_episodes}, because each of the still-abandoned episodes "
        f"now burns a full 10 attempts instead of 3 before giving up. Cap=10 recovers "
        f"{r10.recovered - r3.recovered} more episodes than cap={CURRENT_DEFAULT} at the cost of "
        f"{r10.wasted_calls_on_abandoned_episodes - r3.wasted_calls_on_abandoned_episodes} more "
        f"wasted calls and "
        f"{r10.mean_seconds_to_abandon - r3.mean_seconds_to_abandon:.0f}s more modeled "
        f"wall-clock time per episode that still ends up on the exception list — cap=3 is the "
        f"cheaper failure, cap=10 is the more thorough recovery attempt, and neither is free."
    )

    lines = [
        "# Experiment 3 — `executor_retry_cap`",
        "",
        "## Question",
        "",
        "`config/guardrails.yaml` set `executor_retry_cap: 3` with a `# TODO justify` comment. "
        "Raising it should recover more transient 429s; it should also cost more real API calls "
        "and more wall-clock time on episodes that were never going to succeed. Where does that "
        "trade-off actually land?",
        "",
        "## Method",
        "",
        "Swept `executor_retry_cap` over {1, 3, 5, 10}. At each setting, ran 15 synthetic "
        "episodes through the real `src.execute.executor.RazorpayExecutor` — the same class "
        "`src/runner.py` calls in `execute` mode, not a re-implementation — each wrapped around "
        "a scripted client that returns HTTP 429 for the first `k` calls, then a normal 200. "
        "`k` is drawn from a fixed, disclosed distribution "
        "(`K_DISTRIBUTION = " + str(K_DISTRIBUTION) + "`): mostly small values (a rate limit "
        "that clears fast) with a long tail (an outage that outlasts every cap in this sweep). "
        "This exercises the real `with_backoff()` retry loop and the real "
        "`RazorpayExecutor.create_recovery_link()` control flow — cap exhaustion raises the same "
        "`ExecutorError` production code raises — against a scripted, deterministic fault "
        "instead of the live network.",
        "",
        "`time.sleep` is monkeypatched to a no-op accumulator for the duration of this script "
        "only: the reported \"modeled wall-clock time\" sums the exact backoff delay VALUES the "
        "real `on_attempt` callback computes and would sleep for in production "
        "(`executor_backoff_seconds` = [1, 2, 4], repeating 4 for attempts beyond that list) — "
        "nothing about the retry arithmetic is simulated, only the literal blocking sleep is "
        "skipped so this sweep runs in seconds instead of the several real minutes a k=25 tail "
        "at cap=10 would otherwise cost.",
        "",
        "## Results",
        "",
        "| retry_cap | recovered | abandoned | total API calls | wasted calls (on abandoned "
        "episodes) | modeled wall-clock (total, s) | mean modeled seconds to abandon |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            f"| {r.retry_cap} | {r.recovered}/15 | {r.abandoned}/15 | {r.total_api_calls} | "
            f"{r.wasted_calls_on_abandoned_episodes} | "
            f"{r.total_modeled_wall_clock_seconds:.1f} | {r.mean_seconds_to_abandon:.1f} |"
        )

    lines += [
        "",
        "![executor_retry_cap sweep](charts/retry_cap.png)",
        "",
        "## Conclusion",
        "",
        conclusion,
        "",
        "## What I would measure with more time",
        "",
        "`K_DISTRIBUTION` is a hand-picked, disclosed guess at how long a real rate limit or "
        "outage lasts — it is not measured from Razorpay's actual test-mode 429 behaviour under "
        "load. The real next step is re-running `scripts/guardrail_proof.py` at each retry_cap "
        "setting against the live API (not the scripted client here) and recording how many "
        "real, unplanned 429s (like the ones logged in `BUILD_LOG.md`'s guardrail-proof "
        "session) each cap setting actually needs to clear, rather than assuming a distribution.",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    bundle = load_all()
    backoff_seconds = [float(s) for s in bundle.guardrails.executor_backoff_seconds]

    results = [run_one(cap, backoff_seconds) for cap in SWEEP]

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    results_path = OUT_DIR / "results_retry_cap.json"
    results_path.write_text(
        json.dumps([r.as_dict() for r in results], indent=2), encoding="utf-8"
    )

    chart_path = render_chart(results)
    md_path = OUT_DIR / "retry_cap.md"
    md_path.write_text(render_md(results), encoding="utf-8")

    for r in results:
        print(
            f"cap={r.retry_cap} recovered={r.recovered}/15 abandoned={r.abandoned}/15 "
            f"calls={r.total_api_calls} wasted={r.wasted_calls_on_abandoned_episodes} "
            f"wall_clock={r.total_modeled_wall_clock_seconds:.1f}s"
        )
    print(f"wrote {results_path.relative_to(OUT_DIR.parent.parent)}")
    print(f"wrote {chart_path.relative_to(OUT_DIR.parent.parent)}")
    print(f"wrote {md_path.relative_to(OUT_DIR.parent.parent)}")


if __name__ == "__main__":
    main()
