"""`make classify SPLIT=train|sealed` — the diagnosis-layer evidence pass.

Five sections, printed in the order a skeptical reader should see them
(cheapest-to-trust first, not the order they were computed in):

  1. coverage + cost           — how much of SPLIT the regex baseline
                                  resolved for free, and what the LLM tail
                                  cost, on the real production cascade
                                  (Diagnoser.diagnose()).
  2. self-graded metrics       — precision/recall/F1 vs this project's own
                                  generator truth. Labelled "self-graded"
                                  because it is: SPLIT's `error_reason` was
                                  generated from the exact anchor token
                                  config/taxonomy.yaml's regex_patterns were
                                  written from (see baseline.py's module
                                  docstring), so a high score here is a
                                  property of the generator, not evidence
                                  about real Razorpay traffic.
  3. externally-anchored accuracy — regex and LLM run independently
                                  (Diagnoser.diagnose_llm_only(), bypassing
                                  the free-baseline-first cascade) against
                                  two sources this project did not author:
                                  the raw fields of the 20 real test-mode
                                  failures in evidence/harvested_errors.jsonl,
                                  and the prose descriptions Razorpay
                                  publishes in
                                  evidence/razorpay_error_codes_snapshot.md.
  4. THE HEAD-TO-HEAD           — regex vs LLM, independently, on the top
                                  five error families by volume in SPLIT.
                                  Whichever wins is printed plainly; if
                                  regex wins, that is the headline, not a
                                  footnote (this project's own non-negotiable).
  5. evidence/classification_metrics.json — everything above, machine-
                                  readable.

Every LLM call in sections 3-4 goes through the same DiskCache as
production. `LLM_PROVIDER=none` (no key) does not crash this script: each
independent LLM attempt is caught and that cell is reported as "not run —
no LLM configured" rather than fabricated. See CLAUDE.md: "if a key is
missing, fail loudly with a clear message — never silently return canned
data." Section 1's own production cascade only touches the LLM at all if
the regex baseline's coverage is below 100%, which is a real, disclosed
possibility this script reports rather than assumes.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from scripts.holdout_guard import open_labels
from src.config import load_settings
from src.config_models import Taxonomy, load_all
from src.diagnose.baseline import RegexBaseline
from src.diagnose.cache import DiskCache
from src.diagnose.classifier import Diagnoser, Diagnosis
from src.diagnose.llm_client import build_llm_client
from src.errors import ConfigError
from src.gate.checks import Episode

ROOT = Path(__file__).resolve().parent.parent

IST = timezone(timedelta(hours=5, minutes=30))
SYNTH_NOW = datetime(2026, 8, 26, 12, 0, 0, tzinfo=IST)

TRAIN_PATH = ROOT / "data" / "train.jsonl"
SEALED_PATH = ROOT / "holdout" / "sealed.jsonl"
HARVEST_PATH = ROOT / "evidence" / "harvested_errors.jsonl"
DOC_SNAPSHOT_PATH = ROOT / "evidence" / "razorpay_error_codes_snapshot.md"
METRICS_OUT_PATH = ROOT / "evidence" / "classification_metrics.json"

TOP_N_FAMILIES = 5
_DOC_TOKEN_CELL_RE = re.compile(r"^`([a-z_]+)`$")


class LLMUnavailable(Exception):
    """Raised when an independent evaluation call needed the LLM and none
    is configured. Caught at the section level, never lets the script crash."""


# ---------------------------------------------------------------------------
# loading episodes
# ---------------------------------------------------------------------------


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _load_train() -> list[tuple[Episode, str]]:
    rows = _load_jsonl(TRAIN_PATH)
    return [(Episode.model_validate(row), row["cause_class"]) for row in rows]


def _load_sealed_with_labels() -> list[tuple[Episode, str]]:
    """Ground truth for the sealed split lives only in holdout/labels.jsonl.
    open_labels() refuses to run from inside src/ (scripts/holdout_guard.py)
    — this call is fine because it originates in scripts/, and this is
    exactly the kind of evaluation-only use that guard exists to allow."""
    rows = _load_jsonl(SEALED_PATH)
    labels = {row["episode_id"]: row["cause_class"] for row in open_labels()}
    out = []
    for row in rows:
        true_class = labels.get(row["episode_id"])
        if true_class is None:
            continue
        out.append((Episode.model_validate(row), true_class))
    return out


def _build_taxonomy_maps(taxonomy: Taxonomy) -> tuple[dict[str, str], dict[str, str]]:
    """(harvest_id -> class_id), (reason_token -> class_id). A reason token
    that maps to more than one class is dropped from the second map and
    logged — "where the mapping is unambiguous" per the phase spec."""
    harvest_to_class: dict[str, str] = {}
    reason_to_classes: dict[str, set[str]] = defaultdict(set)
    for cls in taxonomy.classes:
        for anchor in cls.anchor_error_strings:
            if anchor.harvest_id:
                harvest_to_class[anchor.harvest_id] = cls.class_id
            if anchor.reason:
                reason_to_classes[anchor.reason].add(cls.class_id)
    reason_to_class = {
        token: next(iter(classes))
        for token, classes in reason_to_classes.items()
        if len(classes) == 1
    }
    ambiguous = {
        token: classes for token, classes in reason_to_classes.items() if len(classes) > 1
    }
    if ambiguous:
        print(f"NOTE: dropping {len(ambiguous)} ambiguous reason token(s) from the doc-anchored "
              f"ground truth (maps to more than one class): {ambiguous}")
    return harvest_to_class, reason_to_class


def _load_harvested_raw(taxonomy: Taxonomy) -> list[tuple[Episode, str]]:
    """The 20 real test-mode failures, using their ACTUAL raw fields (not
    the harvest harness's `planned_error_reason`) — this is the honest,
    externally-anchored input: what a real webhook would actually contain."""
    harvest_to_class, _ = _build_taxonomy_maps(taxonomy)
    out = []
    for row in _load_jsonl(HARVEST_PATH):
        class_id = harvest_to_class.get(row["harvest_id"])
        if class_id is None:
            continue
        ep = Episode(
            episode_id=f"harvest_{row['harvest_id']}",
            payment_id=row.get("payment_id") or f"pay_harvest_{row['harvest_id']}",
            amount_paise=row.get("amount_paise", 100),
            instrument=row.get("instrument"),
            error_code=row.get("error_code"),
            error_description=row.get("error_description"),
            error_source=row.get("error_source"),
            error_step=row.get("error_step"),
            error_reason=row.get("error_reason"),
            failed_at=SYNTH_NOW,
            received_at=SYNTH_NOW,
        )
        out.append((ep, class_id))
    return out


def _parse_doc_snapshot(path: Path) -> dict[str, str]:
    """token -> Razorpay's own published description. Table rows only;
    header/separator rows have no backtick-quoted cell and are skipped."""
    mapping: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 2:
            continue
        token = None
        for cell in cells:
            m = _DOC_TOKEN_CELL_RE.match(cell)
            if m:
                token = m.group(1)
                break
        if token is None:
            continue
        description = cells[-1].strip()
        if description:
            mapping[token] = description
    return mapping


def _load_doc_anchored(taxonomy: Taxonomy) -> list[tuple[Episode, str]]:
    """Razorpay's own doc prose as the only signal — deliberately without
    `error_reason` set, so the baseline must actually parse the sentence
    rather than exact-match the token it was written from. Independent of
    this project's harvest run: a second, separately-sourced label."""
    _, reason_to_class = _build_taxonomy_maps(taxonomy)
    token_to_description = _parse_doc_snapshot(DOC_SNAPSHOT_PATH)
    out = []
    for token, description in token_to_description.items():
        class_id = reason_to_class.get(token)
        if class_id is None:
            continue
        ep = Episode(
            episode_id=f"doc_{token}",
            payment_id=f"pay_doc_{token}",
            amount_paise=100,
            error_code="BAD_REQUEST_ERROR",
            error_description=description,
            failed_at=SYNTH_NOW,
            received_at=SYNTH_NOW,
        )
        out.append((ep, class_id))
    return out


# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------


def _accuracy(pairs: list[tuple[str, str]]) -> float:
    if not pairs:
        return 0.0
    return sum(1 for pred, true in pairs if pred == true) / len(pairs)


def _precision_recall_f1(
    pairs: list[tuple[str, str]], classes: list[str]
) -> dict[str, dict[str, float]]:
    tp: Counter[str] = Counter()
    fp: Counter[str] = Counter()
    fn: Counter[str] = Counter()
    support: Counter[str] = Counter()
    for pred, true in pairs:
        support[true] += 1
        if pred == true:
            tp[true] += 1
        else:
            fp[pred] += 1
            fn[true] += 1
    out: dict[str, dict[str, float]] = {}
    for c in classes:
        if support[c] == 0:
            continue
        p = tp[c] / (tp[c] + fp[c]) if (tp[c] + fp[c]) else 0.0
        r = tp[c] / (tp[c] + fn[c]) if (tp[c] + fn[c]) else 0.0
        f1 = 2 * p * r / (p + r) if (p + r) else 0.0
        out[c] = {
            "precision": round(p, 3), "recall": round(r, 3),
            "f1": round(f1, 3), "support": support[c],
        }
    return out


def _run_regex(baseline: RegexBaseline, ep: Episode) -> str | None:
    result = baseline.classify(ep)
    return result.class_id if result is not None else None


def _run_llm_only(diagnoser: Diagnoser, ep: Episode) -> Diagnosis:
    try:
        return diagnoser.diagnose_llm_only(ep)
    except ConfigError as exc:
        raise LLMUnavailable(str(exc)) from exc


# ---------------------------------------------------------------------------
# sections
# ---------------------------------------------------------------------------


def _section_production_run(
    diagnoser: Diagnoser, episodes: list[tuple[Episode, str]], class_ids: list[str]
) -> dict[str, Any]:
    regex_count = 0
    llm_count = 0
    llm_unavailable_count = 0
    total_cost_paise = 0
    cache_hits = 0
    llm_calls_attempted = 0
    pairs: list[tuple[str, str]] = []

    for ep, true_class in episodes:
        try:
            diagnosis = diagnoser.diagnose(ep)
        except ConfigError:
            llm_unavailable_count += 1
            continue
        if diagnosis.method == "regex":
            regex_count += 1
        else:
            llm_count += 1
            llm_calls_attempted += 1
            total_cost_paise += diagnosis.cost_paise
            if diagnosis.cache_hit:
                cache_hits += 1
        pairs.append((diagnosis.class_id, true_class))

    n = len(episodes)
    prf1 = _precision_recall_f1(pairs, class_ids)
    cost_paise_per_100 = round(total_cost_paise / n * 100, 2) if n else 0.0
    return {
        "n_episodes": n,
        "regex_resolved": regex_count,
        "llm_resolved": llm_count,
        "llm_unavailable_skipped": llm_unavailable_count,
        "coverage_regex_only": round(regex_count / n, 4) if n else 0.0,
        "llm_calls_attempted": llm_calls_attempted,
        "llm_cache_hits": cache_hits,
        "total_cost_paise": total_cost_paise,
        "cost_paise_per_100_episodes": cost_paise_per_100,
        "cost_rupees_per_100_episodes": round(cost_paise_per_100 / 100, 4),
        "accuracy_self_graded": round(_accuracy(pairs), 4),
        "precision_recall_f1_self_graded": prf1,
    }


def _section_independent(
    baseline: RegexBaseline, diagnoser: Diagnoser, episodes: list[tuple[Episode, str]], label: str
) -> dict[str, Any]:
    # An unresolved regex prediction (None) is never treated as excluded —
    # it can never equal a real class_id, so _accuracy() below counts it as
    # a miss, exactly like a wrong guess. Unmatched-tail episodes count
    # against the baseline's accuracy here, not out of it.
    regex_pairs = [(_run_regex(baseline, ep), true) for ep, true in episodes]
    regex_accuracy = _accuracy(regex_pairs)

    llm_pairs: list[tuple[str, str]] = []
    llm_skipped = False
    llm_skip_reason = ""
    for ep, true in episodes:
        try:
            diagnosis = _run_llm_only(diagnoser, ep)
        except LLMUnavailable as exc:
            llm_skipped = True
            llm_skip_reason = str(exc)
            break
        llm_pairs.append((diagnosis.class_id, true))

    result: dict[str, Any] = {
        "label": label,
        "n_episodes": len(episodes),
        "regex_accuracy": round(regex_accuracy, 4),
    }
    if llm_skipped:
        result["llm_accuracy"] = None
        result["llm_skipped_reason"] = llm_skip_reason
    else:
        result["llm_accuracy"] = round(_accuracy(llm_pairs), 4)
    return result


def _section_head_to_head(
    baseline: RegexBaseline, diagnoser: Diagnoser, episodes: list[tuple[Episode, str]]
) -> dict[str, Any]:
    by_family: dict[str, list[tuple[Episode, str]]] = defaultdict(list)
    for ep, true in episodes:
        family = ep.error_reason or "(none)"
        by_family[family].append((ep, true))

    volumes = sorted(by_family.items(), key=lambda kv: len(kv[1]), reverse=True)
    top_families = volumes[:TOP_N_FAMILIES]
    tail_families = volumes[TOP_N_FAMILIES:]

    def _family_row(name: str, members: list[tuple[Episode, str]]) -> dict[str, Any]:
        regex_pairs = [(_run_regex(baseline, ep), true) for ep, true in members]
        regex_acc = _accuracy(regex_pairs)
        llm_pairs: list[tuple[str, str]] = []
        skipped = False
        skip_reason = ""
        for ep, true in members:
            try:
                diagnosis = _run_llm_only(diagnoser, ep)
            except LLMUnavailable as exc:
                skipped = True
                skip_reason = str(exc)
                break
            llm_pairs.append((diagnosis.class_id, true))
        llm_acc = None if skipped else _accuracy(llm_pairs)
        if skipped:
            winner = "n/a (LLM not run)"
        elif regex_acc > llm_acc:
            winner = "regex"
        elif llm_acc > regex_acc:
            winner = "llm"
        else:
            winner = "tie"
        return {
            "family": name,
            "volume": len(members),
            "regex_accuracy": round(regex_acc, 4),
            "llm_accuracy": None if skipped else round(llm_acc, 4),
            "llm_skipped_reason": skip_reason if skipped else None,
            "winner": winner,
        }

    rows = [_family_row(name, members) for name, members in top_families]

    tail_members = [pair for _, members in tail_families for pair in members]
    tail_row = (
        _family_row("(tail — remaining lower-volume families)", tail_members)
        if tail_members
        else None
    )

    regex_wins = sum(1 for r in rows if r["winner"] == "regex")
    llm_wins = sum(1 for r in rows if r["winner"] == "llm")
    if any(r["winner"] == "n/a (LLM not run)" for r in rows):
        summary = (
            "LLM comparison not run for one or more top families (no LLM configured) — "
            "see llm_skipped_reason on each row; regex-only accuracy is reported above."
        )
    elif regex_wins > llm_wins:
        summary = (
            f"regex beat the LLM on {regex_wins}/{len(rows)} of the top {len(rows)} error "
            "families by volume — the headline, not a footnote, per this project's own rule."
        )
    elif llm_wins > regex_wins:
        summary = (
            f"the LLM beat regex on {llm_wins}/{len(rows)} of the top {len(rows)} "
            "error families by volume."
        )
    else:
        summary = f"regex and the LLM tied across the top {len(rows)} error families by volume."

    return {"top_families": rows, "tail": tail_row, "summary": summary}


# ---------------------------------------------------------------------------
# printing
# ---------------------------------------------------------------------------


def _print_report(
    split: str,
    production: dict[str, Any],
    externally_anchored: list[dict[str, Any]],
    head_to_head: dict[str, Any],
) -> None:
    print(f"\n=== Second Rail diagnosis metrics — SPLIT={split} ===\n")

    print("--- 1. coverage + cost (production cascade, Diagnoser.diagnose()) ---")
    print(f"episodes: {production['n_episodes']}")
    print(
        f"regex-resolved (no LLM call): {production['regex_resolved']} "
        f"({production['coverage_regex_only'] * 100:.1f}% coverage)"
    )
    print(
        f"LLM-resolved: {production['llm_resolved']} "
        f"(cache hits: {production['llm_cache_hits']})"
    )
    if production["llm_unavailable_skipped"]:
        print(
            f"WARNING: {production['llm_unavailable_skipped']} episode(s) needed the LLM but "
            "none is configured — excluded from accuracy below, not fabricated."
        )
    print(
        f"cost: {production['total_cost_paise']} paise total -> "
        f"{production['cost_paise_per_100_episodes']:.2f} paise "
        f"(Rs {production['cost_rupees_per_100_episodes']:.2f}) per 100 episodes"
    )

    print("\n--- 2. self-graded metrics (vs this project's own generator truth) ---")
    print(f"accuracy: {production['accuracy_self_graded'] * 100:.1f}%")
    print(f"{'class':<6}{'precision':>10}{'recall':>10}{'f1':>10}{'support':>10}")
    for class_id, m in sorted(production["precision_recall_f1_self_graded"].items()):
        print(
            f"{class_id:<6}{m['precision']:>10.3f}{m['recall']:>10.3f}"
            f"{m['f1']:>10.3f}{m['support']:>10}"
        )

    print("\n--- 3. externally-anchored accuracy (not routed through the generator) ---")
    for section in externally_anchored:
        if section["llm_accuracy"] is None:
            llm_str = "not run"
        else:
            llm_str = f"{section['llm_accuracy'] * 100:.1f}%"
        print(
            f"{section['label']} (n={section['n_episodes']}): "
            f"regex={section['regex_accuracy'] * 100:.1f}%  llm={llm_str}"
        )
        if section.get("llm_skipped_reason"):
            print(f"    llm skipped: {section['llm_skipped_reason']}")

    print("\n--- 4. THE HEAD-TO-HEAD: top error families by volume, regex vs LLM ---")
    def _format_row(row: dict[str, Any]) -> str:
        llm_str = "n/a" if row["llm_accuracy"] is None else f"{row['llm_accuracy'] * 100:.1f}%"
        regex_str = f"{row['regex_accuracy'] * 100:.1f}%"
        return (
            f"{row['family']:<32}{row['volume']:>8}{regex_str:>10}{llm_str:>10}  {row['winner']}"
        )

    print(f"{'family':<32}{'volume':>8}{'regex':>10}{'llm':>10}  winner")
    for row in head_to_head["top_families"]:
        print(_format_row(row))
    if head_to_head["tail"]:
        print(_format_row(head_to_head["tail"]))
    print(f"\n{head_to_head['summary']}\n")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="Second Rail diagnosis-layer evidence pass")
    parser.add_argument("--split", required=True, choices=["train", "sealed"])
    args = parser.parse_args()

    settings = load_settings()
    bundle = load_all()
    taxonomy = bundle.taxonomy
    class_ids = taxonomy.class_ids()

    baseline = RegexBaseline(taxonomy)
    cache = DiskCache(settings.cache_dir)
    llm = build_llm_client(settings)
    diagnoser = Diagnoser(baseline, llm, cache, taxonomy, settings)

    episodes = _load_train() if args.split == "train" else _load_sealed_with_labels()

    production = _section_production_run(diagnoser, episodes, class_ids)

    externally_anchored = [
        _section_independent(
            baseline, diagnoser, _load_harvested_raw(taxonomy),
            "harvested strings (raw, real fields)",
        ),
        _section_independent(
            baseline, diagnoser, _load_doc_anchored(taxonomy),
            "Razorpay doc snapshot (independent label source)",
        ),
    ]

    head_to_head = _section_head_to_head(baseline, diagnoser, episodes)

    _print_report(args.split, production, externally_anchored, head_to_head)

    METRICS_OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    METRICS_OUT_PATH.write_text(
        json.dumps(
            {
                "split": args.split,
                "production_run": production,
                "externally_anchored": externally_anchored,
                "head_to_head": head_to_head,
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
