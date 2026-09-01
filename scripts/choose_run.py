"""`make choose-run SPLIT=train|sealed` — the action-selection evidence pass.

Runs the real production diagnose-then-choose cascade
(src/diagnose/classifier.py's Diagnoser.diagnose() -> src/choose/policy.py's
PolicyEngine.resolve() -> src/choose/selector.py's ActionSelector.select())
over every episode in SPLIT and reports, in this order:

  1. admissibility_rate — decisions inside the pre-registered admissible
     set / total decisions. Always 1.0 for any run that completes, by
     construction: ActionSelector.select() raises AdmissibilityError
     (which this script does NOT catch — see below) rather than ever
     returning a decision outside the set, so a completed run's rate can
     only ever be 1.0 or the script itself will have already stopped.
  2. the distribution of chosen actions across the split.
  3. how many episodes named a feature outside LLM_VISIBLE_FEATURES —
     logged and recorded, per src/choose/selector.py's RULE 4, not a
     crash — and the count is reported here, not buried.
  4. LLM spend (both diagnose and choose calls), cache hit rate.

AdmissibilityError is deliberately allowed to propagate and crash this
script, uncaught: "the agent chose outside its box" is the one failure
this project refuses to paper over even in an evidence-generation script,
per src/choose/selector.py's module docstring.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from src.choose.policy import PolicyEngine
from src.choose.selector import ActionSelector
from src.config import load_settings
from src.config_models import load_all
from src.diagnose.baseline import RegexBaseline
from src.diagnose.cache import DiskCache
from src.diagnose.classifier import Diagnoser
from src.diagnose.llm_client import build_llm_client
from src.gate.checks import Episode

ROOT = Path(__file__).resolve().parent.parent
TRAIN_PATH = ROOT / "data" / "train.jsonl"
SEALED_PATH = ROOT / "holdout" / "sealed.jsonl"
METRICS_OUT_PATH = ROOT / "evidence" / "choose_metrics.json"


def _load_episodes(path: Path) -> list[Episode]:
    episodes = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            episodes.append(Episode.model_validate_json(line))
    return episodes


def main() -> int:
    parser = argparse.ArgumentParser(description="Second Rail action-selection evidence pass")
    parser.add_argument("--split", required=True, choices=["train", "sealed"])
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Process only the first N episodes of the split (smoke-testing; the Makefile "
        "target never passes this, so `make choose-run` always covers the full split).",
    )
    args = parser.parse_args()

    settings = load_settings()
    bundle = load_all()
    taxonomy = bundle.taxonomy

    baseline = RegexBaseline(taxonomy)
    cache_dir = settings.cache_dir
    llm = build_llm_client(settings)
    diagnoser = Diagnoser(baseline, llm, DiskCache(cache_dir), taxonomy, settings)
    policy_engine = PolicyEngine(bundle.policy)
    selector = ActionSelector(llm, DiskCache(cache_dir), settings)

    path = TRAIN_PATH if args.split == "train" else SEALED_PATH
    episodes = _load_episodes(path)
    if args.limit is not None:
        episodes = episodes[: args.limit]

    action_counts: Counter[str] = Counter()
    escalation_counts: Counter[str] = Counter()
    features_outside_whitelist_episodes = 0
    llm_degraded_count = 0
    total_cost_paise = 0
    cache_hits = 0
    decisions_total = 0
    decisions_inside_set = 0
    worked_examples: list[dict[str, Any]] = []

    for ep in episodes:
        diagnosis = diagnoser.diagnose(ep)
        match = policy_engine.resolve(ep, diagnosis)
        selection = selector.select(ep, diagnosis, match)

        decisions_total += 1
        if selection.inside_admissible_set:
            decisions_inside_set += 1
        action_counts[selection.chosen_action] += 1
        escalation_counts[match.escalation_tier] += 1
        if selection.features_used_outside_whitelist:
            features_outside_whitelist_episodes += 1
        if selection.llm_degraded:
            llm_degraded_count += 1
        total_cost_paise += selection.cost_paise
        if selection.cache_hit:
            cache_hits += 1
        if len(worked_examples) < 3:
            worked_examples.append(
                {
                    "episode_id": ep.episode_id,
                    "cause_class": diagnosis.class_id,
                    "policy_rule_id": match.policy_rule_id,
                    "admissible_actions": match.admissible_actions,
                    "chosen_action": selection.chosen_action,
                    "features_used": selection.features_used,
                    "features_used_outside_whitelist": selection.features_used_outside_whitelist,
                }
            )

    admissibility_rate = decisions_inside_set / decisions_total if decisions_total else 0.0

    print(f"\n=== Second Rail action-selection metrics - SPLIT={args.split} ===\n")
    print(f"episodes: {decisions_total}")
    print(
        f"admissibility_rate: {admissibility_rate:.4f} "
        f"({decisions_inside_set}/{decisions_total})"
    )
    print(f"llm_degraded (fallback_priority used): {llm_degraded_count}")
    print(
        f"episodes naming a feature outside LLM_VISIBLE_FEATURES: "
        f"{features_outside_whitelist_episodes}"
    )
    print(
        f"llm cost this run: {total_cost_paise} paise "
        f"(cache hits: {cache_hits}/{decisions_total})"
    )

    print("\n--- distribution of chosen actions ---")
    for action, count in action_counts.most_common():
        pct = count / decisions_total * 100 if decisions_total else 0.0
        print(f"{action:<24}{count:>6}  ({pct:.1f}%)")

    print("\n--- distribution of escalation tiers ---")
    for tier, count in escalation_counts.most_common():
        pct = count / decisions_total * 100 if decisions_total else 0.0
        print(f"{tier:<24}{count:>6}  ({pct:.1f}%)")
    print()

    METRICS_OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    METRICS_OUT_PATH.write_text(
        json.dumps(
            {
                "split": args.split,
                "episodes": decisions_total,
                "admissibility_rate": round(admissibility_rate, 4),
                "decisions_inside_set": decisions_inside_set,
                "llm_degraded_count": llm_degraded_count,
                "features_used_outside_whitelist_episodes": features_outside_whitelist_episodes,
                "llm_cost_paise_this_run": total_cost_paise,
                "llm_cache_hits": cache_hits,
                "action_distribution": dict(action_counts),
                "escalation_tier_distribution": dict(escalation_counts),
                "worked_examples": worked_examples,
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    print(f"wrote {METRICS_OUT_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
