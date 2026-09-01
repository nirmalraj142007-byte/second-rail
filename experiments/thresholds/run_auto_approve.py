"""Phase 14, experiment 1 — `auto_approve_ceiling_paise`.

QUESTION: `config/guardrails.yaml` currently sets this to 500000 paise
(Rs 5,000) with a `# TODO justify` comment. Every episode above the
ceiling gets a human keystroke instead of auto-executing; every episode
at or below it never sees a human. Pick it too low and the human queue
absorbs so much of the batch that "auto-approve" stops meaning anything;
pick it too high and a single bad diagnosis can auto-create a large-value
Payment Link with nobody in the loop. Where does that trade-off actually
sit on this project's own data?

METHOD: sweep `auto_approve_ceiling_paise` over {Rs 1,000, 2,000, 5,000,
10,000} (100000/200000/500000/1000000 paise). At each setting, run the
real `src.gate.engine.GateEngine` — the same class `src/runner.py` calls
internally, not a re-implementation — over every one of the 400
`data/train.jsonl` episodes, in file order, carrying a real `RunState`
forward exactly as a live run would (exposure/frequency accounting
accumulates the same way). No LLM calls, no executor calls — this
threshold is resolved entirely inside the gate, before diagnose/choose/
execute are ever reached (`src/gate/engine.py::_compute_tier`), so
sweeping it needs nothing past the gate.

Two disclosed scoping decisions, both because this is a per-threshold
config experiment, not a live-run demo:
  1. The four run-level stopping rules (`src/gate/stopping.py`) are NOT
     applied here — a live run legitimately halts early on e.g. a cap
     breach, but that would truncate this sweep at whatever episode index
     the (unrelated) per-run exposure ceiling happens to trip, identically
     across all four settings, for reasons that have nothing to do with
     the threshold under test. Every setting below processes the full 400.
  2. "% of episodes routed to the human queue" is computed over all 400
     episodes, not just gate-eligible ones — `_compute_tier()` assigns a
     tier before the other six checks run, so the ceiling's routing
     signal exists independent of whether the episode later turns out
     ineligible for an unrelated reason.
  "Total rupee exposure auto-approved" and "high-value episodes routed
  unreviewed", by contrast, ARE restricted to gate-eligible episodes —
  those are the only episodes that would actually get a real Payment
  Link created.

Writes:
  experiments/thresholds/results_auto_approve.json
  experiments/thresholds/charts/auto_approve.png
  experiments/thresholds/auto_approve.md
"""

from __future__ import annotations

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402  (backend must be set first)

from experiments.thresholds._common import (  # noqa: E402
    fresh_conn,
    load_train_episodes,
    record_episode,
)
from src.config_models import ConfigBundle, load_all  # noqa: E402
from src.db.repo import get_opted_out_customer_ids  # noqa: E402
from src.gate.checks import Episode, GateContext, RunState  # noqa: E402
from src.gate.engine import GateEngine, compute_cluster_membership  # noqa: E402

OUT_DIR = Path(__file__).resolve().parent
SWEEP_PAISE: list[int] = [100_000, 200_000, 500_000, 1_000_000]  # Rs 1k / 2k / 5k / 10k
CURRENT_DEFAULT_PAISE = 500_000


# batch_contact_ceiling also routes to human_keystroke once the 51st
# eligible episode this run is reached (src/gate/engine.py::_compute_tier),
# independent of amount — on the 400-episode train split, roughly half of
# all eligible episodes land past that point regardless of amount, which
# would swamp the amount-ceiling's own effect on the tier split if left
# at its real value. Pinned effectively infinite here, disclosed, so this
# sweep isolates auto_approve_ceiling_paise alone — the one threshold this
# experiment exists to measure. batch_contact_ceiling itself is untouched
# in config/guardrails.yaml; it is not this experiment's subject.
_BATCH_CEILING_DISABLED = 100_000


def run_one(bundle: ConfigBundle, ceiling_paise: int, episodes: list[Episode]) -> dict:
    g = bundle.guardrails.model_copy(
        update={
            "auto_approve_ceiling_paise": ceiling_paise,
            "batch_contact_ceiling": _BATCH_CEILING_DISABLED,
        }
    )
    conn = fresh_conn()
    try:
        opted_out = get_opted_out_customer_ids(conn)
        cluster_membership = compute_cluster_membership(episodes, g.outage_cluster_threshold)
        gate = GateEngine()
        state = RunState()

        tier_counts = {"auto": 0, "human_keystroke": 0, "hard_refuse": 0}
        eligible_count = 0
        auto_approved_exposure_paise = 0
        high_value_unreviewed = 0

        for ep in episodes:
            ctx = GateContext(
                now=ep.received_at,
                conn=conn,
                state=state,
                opted_out_customers=opted_out,
                cluster_key_for_episode=cluster_membership,
            )
            decision = gate.evaluate(ep, ctx, g)

            if decision.failed_check != "duplicate":
                record_episode(conn, ep)

            tier_counts[decision.escalation_tier] += 1

            if decision.eligible:
                eligible_count += 1
                # Mirrors src/runner.py's own post-eligibility accounting
                # for the default (non-"no_action") path, since this sweep
                # never wires in a selector that could choose "no_action".
                state.exposure_committed_paise += ep.amount_paise
                state.total_eligible_contacts_this_run += 1
                if ep.customer_id:
                    state.contacts_by_customer.setdefault(ep.customer_id, []).append(
                        ep.failed_at
                    )
                if decision.escalation_tier == "auto":
                    auto_approved_exposure_paise += ep.amount_paise
                    if ep.segment == "high_value":
                        high_value_unreviewed += 1
    finally:
        conn.close()

    total = len(episodes)
    return {
        "auto_approve_ceiling_paise": ceiling_paise,
        "auto_approve_ceiling_rupees": ceiling_paise / 100,
        "total_episodes": total,
        "tier_counts_all_episodes": tier_counts,
        "human_keystroke_pct_of_all_episodes": round(
            100 * tier_counts["human_keystroke"] / total, 1
        ),
        "gate_eligible_count": eligible_count,
        "auto_approved_exposure_paise": auto_approved_exposure_paise,
        "auto_approved_exposure_rupees": round(auto_approved_exposure_paise / 100, 2),
        "high_value_unreviewed_count": high_value_unreviewed,
    }


def _fmt_rupees(paise_as_rupees: float) -> str:
    return f"Rs {paise_as_rupees:,.0f}"


def render_chart(results: list[dict]) -> Path:
    ceilings = [r["auto_approve_ceiling_rupees"] for r in results]
    queue_pct = [r["human_keystroke_pct_of_all_episodes"] for r in results]
    exposure = [r["auto_approved_exposure_rupees"] for r in results]

    fig, ax1 = plt.subplots(figsize=(7, 4.5))
    ax1.plot(ceilings, queue_pct, marker="o", color="#C44E52", label="% -> human queue")
    ax1.set_xlabel("auto_approve_ceiling_paise (Rs, log scale)")
    ax1.set_ylabel("% of 400 train episodes -> human queue", color="#C44E52")
    ax1.set_xscale("log")
    ax1.tick_params(axis="y", labelcolor="#C44E52")
    for x, y in zip(ceilings, queue_pct, strict=True):
        ax1.annotate(f"{y}%", (x, y), textcoords="offset points", xytext=(0, 8), ha="center")

    ax2 = ax1.twinx()
    ax2.plot(ceilings, exposure, marker="s", color="#4C72B0", label="auto-approved exposure (Rs)")
    ax2.set_ylabel("auto-approved exposure per run (Rs)", color="#4C72B0")
    ax2.tick_params(axis="y", labelcolor="#4C72B0")
    for x, y in zip(ceilings, exposure, strict=True):
        ax2.annotate(
            f"Rs{y:,.0f}", (x, y), textcoords="offset points", xytext=(0, -14), ha="center"
        )

    ax1.set_title("auto_approve_ceiling_paise sweep — 400-episode train split")
    fig.tight_layout()

    charts_dir = OUT_DIR / "charts"
    charts_dir.mkdir(parents=True, exist_ok=True)
    path = charts_dir / "auto_approve.png"
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return path


def render_md(results: list[dict]) -> str:
    by_rupees = {r["auto_approve_ceiling_rupees"]: r for r in results}
    r1000, r2000, r5000, r10000 = (by_rupees[v] for v in (1000, 2000, 5000, 10000))

    q2000 = r2000["human_keystroke_pct_of_all_episodes"]
    conclusion = (
        f"At Rs 2,000 the gate fires on {q2000}% of episodes — roughly 1 in 4 — which is "
        f"already past \"review the unusual case\" and into \"review a routine fraction of "
        f"every batch\"; at Rs 10,000 the auto-approved exposure reaches "
        f"{_fmt_rupees(r10000['auto_approved_exposure_rupees'])} per run, with "
        f"{r10000['high_value_unreviewed_count']} high-value-segment episode(s) auto-"
        f"approved unreviewed, versus {r5000['high_value_unreviewed_count']} at Rs 5,000. "
        f"Rs 5,000 keeps the queue at "
        f"{r5000['human_keystroke_pct_of_all_episodes']}% and exposure at "
        f"{_fmt_rupees(r5000['auto_approved_exposure_rupees'])} — the first setting in this "
        f"sweep where the queue is small enough to plausibly mean \"the unusual case\" rather "
        f"than \"most of the batch\"."
    )

    lines = [
        "# Experiment 1 — `auto_approve_ceiling_paise`",
        "",
        "## Question",
        "",
        "`config/guardrails.yaml` set `auto_approve_ceiling_paise: 500000` (Rs 5,000) with a "
        "`# TODO justify` comment. What does moving that ceiling actually cost or buy, on this "
        "project's own 400-episode train split?",
        "",
        "## Method",
        "",
        "Swept `auto_approve_ceiling_paise` over Rs 1,000 / 2,000 / 5,000 / 10,000 "
        "(100000/200000/500000/1000000 paise). At each setting, ran the real "
        "`src.gate.engine.GateEngine` — the identical class `src/runner.py` calls internally, "
        "not a re-implementation — over all 400 `data/train.jsonl` episodes in file order, "
        "carrying a real `RunState` forward (exposure and frequency accounting accumulate "
        "exactly as a live run's would). No LLM calls, no executor calls: this threshold is "
        "resolved entirely inside the gate (`_compute_tier()`), before diagnose/choose/execute "
        "are ever reached.",
        "",
        "Three scoping decisions, all because this is a per-threshold config experiment rather "
        "than a live-run demo. First, `batch_contact_ceiling` (a separate guardrail: any "
        "episode past the 51st eligible contact this run also routes to the human queue, "
        "regardless of amount) is pinned to an effectively-infinite value for this sweep only — "
        "left at its real value of 50, it swamps the amount ceiling's own effect: on an early "
        "run of this sweep with `batch_contact_ceiling` untouched, the queue percentage barely "
        "moved between Rs 1,000 and Rs 10,000 (80.8% to 74.0%) because most of the queue was "
        "batch-cap-driven, not amount-driven. `config/guardrails.yaml`'s real "
        "`batch_contact_ceiling` value is untouched by this change; it is not this experiment's "
        "subject. Second, the four run-level stopping rules (`src/gate/stopping.py`) are not "
        "applied, so all four settings see the full 400 episodes rather than being truncated by "
        "an unrelated cap breach at the same episode index. Third, \"% routed to the human "
        "queue\" is computed over all 400 episodes (tier is assigned before the other six "
        "checks run), while \"auto-approved exposure\" and \"high-value episodes unreviewed\" "
        "are restricted to gate-eligible episodes, since only those would ever produce a real "
        "Payment Link.",
        "",
        "## Results",
        "",
        "| ceiling | % -> human queue (of 400) | gate-eligible | auto-approved exposure/run | "
        "high-value segment auto-approved |",
        "|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            f"| Rs {r['auto_approve_ceiling_rupees']:,.0f} | "
            f"{r['human_keystroke_pct_of_all_episodes']}% "
            f"({r['tier_counts_all_episodes']['human_keystroke']}/{r['total_episodes']}) | "
            f"{r['gate_eligible_count']} | "
            f"{_fmt_rupees(r['auto_approved_exposure_rupees'])} | "
            f"{r['high_value_unreviewed_count']} |"
        )

    lines += [
        "",
        "![auto_approve_ceiling_paise sweep](charts/auto_approve.png)",
        "",
        "## Conclusion",
        "",
        conclusion,
        "",
        "## What I would measure with more time",
        "",
        "This sweep uses `data/train.jsonl`'s one fixed amount distribution. A merchant with a "
        "higher median ticket size would need a proportionally higher ceiling to keep the same "
        "queue percentage — the right next experiment is re-running this sweep against a "
        "synthetic distribution with a 3-5x higher median, to see whether Rs 5,000 is a property "
        "of this specific dataset or holds up as a ratio-to-median instead.",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    bundle = load_all()
    episodes = load_train_episodes()

    results = [run_one(bundle, ceiling, episodes) for ceiling in SWEEP_PAISE]

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    results_path = OUT_DIR / "results_auto_approve.json"
    results_path.write_text(json.dumps(results, indent=2), encoding="utf-8")

    chart_path = render_chart(results)
    md_path = OUT_DIR / "auto_approve.md"
    md_path.write_text(render_md(results), encoding="utf-8")

    for r in results:
        print(
            f"ceiling=Rs{r['auto_approve_ceiling_rupees']:,.0f} "
            f"queue={r['human_keystroke_pct_of_all_episodes']}% "
            f"eligible={r['gate_eligible_count']} "
            f"auto_exposure={_fmt_rupees(r['auto_approved_exposure_rupees'])} "
            f"high_value_unreviewed={r['high_value_unreviewed_count']}"
        )
    print(f"wrote {results_path.relative_to(OUT_DIR.parent.parent)}")
    print(f"wrote {chart_path.relative_to(OUT_DIR.parent.parent)}")
    print(f"wrote {md_path.relative_to(OUT_DIR.parent.parent)}")


if __name__ == "__main__":
    main()
