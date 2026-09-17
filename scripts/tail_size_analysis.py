"""Sizes the LLM's regex-unmatched tail — the question `evidence/report.md`
raises ("the model only earns its place on the unmatched tail") but never
puts a number on.

Reuses `scripts/classify.py`'s own production-cascade section
(`_section_production_run`) directly, unmodified, against three sources —
train, sealed, and the 20 raw harvested strings — rather than duplicating
its logic. This is deliberately narrower than `make classify`: it never
touches classify.py's externally-anchored or head-to-head sections, which
independently re-run the LLM outside the cascade (bypassing regex
entirely) and are far more expensive — reproduced live while building
this script, a full `make classify SPLIT=train` run drove ~90 fresh,
uncached LLM calls into real Groq rate-limiting. This script only ever
calls the LLM for episodes regex genuinely can't resolve, which is 0 for
train, ~5 for sealed, and (already fully cached from classify.py's own
prior run against the same 20 episodes) effectively 0 new calls for the
harvested set.

For each source, reports: how many episodes the regex baseline leaves
unmatched (a pure coverage question, independent of whether the eventual
answer was correct), the LLM's self-graded accuracy on exactly that
unmatched subset (not blended with the regex-resolved majority), and the
rupee cost per 100 episodes of the source attributable to running the LLM
at all.
"""

from __future__ import annotations

import json
from pathlib import Path

from scripts.classify import (
    _load_harvested_raw,
    _load_sealed_with_labels,
    _load_train,
    _section_production_run,
)
from src.config import load_settings
from src.config_models import load_all
from src.diagnose.baseline import RegexBaseline
from src.diagnose.cache import DiskCache
from src.diagnose.classifier import Diagnoser
from src.diagnose.llm_client import build_llm_client, load_pricing

ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = ROOT / "evidence" / "tail_size_analysis.json"


def main() -> int:
    settings = load_settings()
    bundle = load_all()
    taxonomy = bundle.taxonomy
    class_ids = taxonomy.class_ids()
    pricing = load_pricing()

    baseline = RegexBaseline(taxonomy)
    cache = DiskCache(settings.cache_dir)
    llm = build_llm_client(settings)
    diagnoser = Diagnoser(baseline, llm, cache, taxonomy, settings)

    sources = {
        "train": _load_train(),
        "sealed": _load_sealed_with_labels(),
        "harvested_20": _load_harvested_raw(taxonomy),
    }

    results: dict[str, dict] = {}
    print("=== LLM tail size, per source (production cascade, unmodified) ===\n")
    for name, episodes in sources.items():
        section = _section_production_run(diagnoser, episodes, class_ids, pricing)
        results[name] = section
        tail_acc = section["accuracy_llm_only_on_tail_self_graded"]
        tail_acc_str = "n/a (tail is empty)" if tail_acc is None else f"{tail_acc * 100:.1f}%"
        print(f"--- {name} (n={section['n_episodes']}) ---")
        print(
            f"regex leaves unmatched: {section['llm_resolved']} "
            f"({section['regex_unmatched_fraction'] * 100:.1f}%)"
        )
        print(f"LLM-only accuracy on that unmatched tail: {tail_acc_str}")
        print(
            f"cost attributable to the LLM tail: {section['cost_paise_per_100_episodes']:.2f} "
            f"paise (Rs {section['cost_rupees_per_100_episodes']:.2f}) per 100 {name} episodes "
            f"(0 on a cache hit; real historical cost: {section['total_historical_cost_paise']} "
            "paise total)"
        )
        print()

    OUT_PATH.write_text(json.dumps(results, indent=2, sort_keys=True), encoding="utf-8")
    print(f"wrote {OUT_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
