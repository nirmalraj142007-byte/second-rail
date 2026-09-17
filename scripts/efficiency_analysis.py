"""What the gate actually prevents, sized against a genuinely gate-free
baseline — not the FIXED_RETRY_AT_T30 arm (which already runs through the
same 7 gate checks; see src/runner.py, Runner.run() applies the gate
identically whether or not a diagnoser/selector is wired in). This script
answers a different, narrower question: over the full 200-episode sealed
batch, how many contacts would three specific protections — opt-out,
quiet hours, and the per-run exposure cap — have blocked if nothing
blocked them at all.

Method, stated plainly because it differs per check:
  - opt_out and quiet_hours are evaluated independently, per episode,
    over the FULL batch (not stopped early by any other check or by a
    stopping rule) — both checks are stateless (opt_out) or depend only
    on the episode's own `received_at` (quiet_hours, matching Runner.
    run()'s own convention of using each episode's own timestamp as
    "now" for a batch replay), so "how many of the 200 would this one
    check alone have blocked" is a direct count, not a simulation.
  - the exposure cap is different: whether episode N breaches it depends
    on how much was already committed by every episode before it, so
    this script actually simulates a maximally naive policy — contact
    every one of the 200 episodes, in the same order Runner.run() would
    process them, no diagnosis, no gate, summing amount_paise
    unconditionally — and counts how many episodes past the breach point
    exist. This is real code (check_amount_cap's own comparison,
    src/gate/checks.py) run against real data, not modeled.

Writes evidence/efficiency_analysis.json.
"""

from __future__ import annotations

import json
from pathlib import Path

from src.config_models import load_all
from src.gate.checks import ReasonCode
from src.runner import load_episodes

ROOT = Path(__file__).resolve().parent.parent
SEALED_PATH = ROOT / "holdout" / "sealed.jsonl"
CUSTOMERS_PATH = ROOT / "data" / "customers.jsonl"
OUT_PATH = ROOT / "evidence" / "efficiency_analysis.json"


def _opted_out_customer_ids() -> frozenset[str]:
    ids = set()
    for line in CUSTOMERS_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        if row.get("opted_out"):
            ids.add(row["customer_id"])
    return frozenset(ids)


def main() -> int:
    bundle = load_all()
    g = bundle.guardrails
    episodes = load_episodes([SEALED_PATH])
    opted_out = _opted_out_customer_ids()

    n = len(episodes)

    # --- opt_out: stateless, independent per episode ---
    opt_out_blocked = [ep for ep in episodes if ep.customer_id in opted_out]

    # --- quiet_hours: independent per episode, "now" = the episode's own
    # received_at, matching Runner.run()'s own convention for a batch
    # replay (see that module's docstring on why) ---
    from datetime import time
    from zoneinfo import ZoneInfo

    def _parse_hhmm(value: str) -> time:
        hour, minute = value.split(":")
        return time(int(hour), int(minute))

    start = _parse_hhmm(g.quiet_hours.start)
    end = _parse_hhmm(g.quiet_hours.end)
    tz = ZoneInfo(g.quiet_hours.tz)

    def _in_quiet_hours(ep) -> bool:
        t = ep.received_at.astimezone(tz).time()
        return (t >= start or t < end) if start > end else (start <= t < end)

    quiet_hours_blocked = [ep for ep in episodes if _in_quiet_hours(ep)]

    # --- exposure cap: order-dependent, real simulation of "contact
    # everyone unconditionally" ---
    cumulative = 0
    cap_breached: list = []
    for ep in episodes:  # already sorted by episode_id, load_episodes()'s own order
        cumulative += ep.amount_paise
        if cumulative > g.per_run_exposure_ceiling_paise:
            cap_breached.append(ep)

    result = {
        "batch_size": n,
        "per_run_exposure_ceiling_paise": g.per_run_exposure_ceiling_paise,
        "method": (
            "opt_out and quiet_hours: independent per-episode check over the full "
            "200-episode batch, no other check or stopping rule applied. exposure "
            "cap: real simulation of contacting all 200 episodes unconditionally, "
            "in Runner.run()'s own processing order, summing amount_paise and "
            "applying check_amount_cap's real comparison at each step. This is not "
            "the FIXED_RETRY_AT_T30 baseline (which already runs the same gate) — "
            "it is the hypothetical this project never actually runs: no gate at all."
        ),
        "opt_out_prevented": {
            "count": len(opt_out_blocked),
            "fraction_of_batch": round(len(opt_out_blocked) / n, 4),
            "episode_ids": [ep.episode_id for ep in opt_out_blocked],
            "reason_code": ReasonCode.CUSTOMER_OPTED_OUT,
        },
        "quiet_hours_prevented": {
            "count": len(quiet_hours_blocked),
            "fraction_of_batch": round(len(quiet_hours_blocked) / n, 4),
            "reason_code": ReasonCode.QUIET_HOURS_BLOCK,
        },
        "cap_breach_prevented": {
            "count": len(cap_breached),
            "fraction_of_batch": round(len(cap_breached) / n, 4),
            "first_breaching_episode_id": cap_breached[0].episode_id if cap_breached else None,
            "reason_code": ReasonCode.EXPOSURE_CEILING_EXCEEDED,
        },
    }

    OUT_PATH.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")

    print(f"=== hypothetical: no gate at all, contact all {n} sealed episodes ===\n")
    print(
        f"opt-out contacts prevented: {result['opt_out_prevented']['count']} "
        f"({result['opt_out_prevented']['fraction_of_batch'] * 100:.1f}% of batch)"
    )
    print(
        f"quiet-hour contacts prevented: {result['quiet_hours_prevented']['count']} "
        f"({result['quiet_hours_prevented']['fraction_of_batch'] * 100:.1f}% of batch)"
    )
    print(
        f"cap-breaching contacts prevented: {result['cap_breach_prevented']['count']} "
        f"({result['cap_breach_prevented']['fraction_of_batch'] * 100:.1f}% of batch) "
        f"-- cap first breached at {result['cap_breach_prevented']['first_breaching_episode_id']}"
    )
    print(f"\nwrote {OUT_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
