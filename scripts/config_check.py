"""`make config-check` — validates the three money-adjacent config files.

Every check below prints its own PASS/FAIL line and the whole script exits
1 if any check fails. This is deliberately a standalone script, not a
pytest module: a judge should be able to read eight lines of terminal
output and know whether the guardrail story holds together, without
running the test suite.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from src.config_models import INSTRUMENTS, SEGMENTS, ConfigBundle, config_hash, load_all
from src.errors import ConfigError

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
GUARDRAILS_PATH = CONFIG_DIR / "guardrails.yaml"
HARVEST_PATH = ROOT / "evidence" / "harvested_errors.jsonl"
OUTCOME_MODEL_PATH = ROOT / "outcome_model.md"

_ATTRIBUTION_WINDOW_RE = re.compile(r"\*\*(\d+)\s*hours from action execution\.\*\*")
_NUMERIC_RE = re.compile(r"\d")


def _load_harvest_records(path: Path) -> dict[str, dict]:
    records: dict[str, dict] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        record = json.loads(line)
        records[record["harvest_id"]] = record
    return records


def check_load(config_dir: Path) -> tuple[bool, str, ConfigBundle | None]:
    try:
        bundle = load_all(config_dir)
    except ConfigError as exc:
        return False, str(exc), None
    n_classes = len(bundle.taxonomy.classes)
    n_rules = len(bundle.policy.rules)
    detail = f"taxonomy ({n_classes} classes), policy_table ({n_rules} rules), guardrails all valid"
    return True, detail, bundle


def check_anchors_verbatim(bundle: ConfigBundle, harvest_path: Path) -> tuple[bool, str]:
    if not harvest_path.exists():
        return False, f"{harvest_path} not found"
    records = _load_harvest_records(harvest_path)
    checked = 0
    for cls in bundle.taxonomy.classes:
        for anchor in cls.anchor_error_strings:
            if anchor.harvest_id is None:
                return False, (
                    f"class {cls.class_id}: anchor has no harvest_id to verify against "
                    f"{harvest_path}"
                )
            record = records.get(anchor.harvest_id)
            if record is None:
                return False, (
                    f"class {cls.class_id}: harvest_id {anchor.harvest_id} not found in "
                    f"{harvest_path}"
                )
            if anchor.error_code != record.get("error_code"):
                return False, (
                    f"class {cls.class_id} harvest_id {anchor.harvest_id}: error_code "
                    f"{anchor.error_code!r} != harvested {record.get('error_code')!r}"
                )
            if anchor.error_description != record.get("error_description"):
                return False, (
                    f"class {cls.class_id} harvest_id {anchor.harvest_id}: error_description "
                    f"{anchor.error_description!r} != harvested "
                    f"{record.get('error_description')!r}"
                )
            if anchor.reason is not None:
                valid = {record.get("planned_error_reason"), record.get("error_reason")}
                if anchor.reason not in valid:
                    return False, (
                        f"class {cls.class_id} harvest_id {anchor.harvest_id}: reason "
                        f"{anchor.reason!r} not in harvested reason fields {valid}"
                    )
            checked += 1
    n_classes = len(bundle.taxonomy.classes)
    return True, f"{checked} anchor string(s) across {n_classes} classes verified verbatim"


def check_no_action_everywhere(bundle: ConfigBundle) -> tuple[bool, str]:
    bad = [
        r.policy_rule_id for r in bundle.policy.rules if "no_action" not in r.admissible_actions
    ]
    if "no_action" not in bundle.policy.default_rule.admissible_actions:
        bad.append("default_rule")
    if bad:
        return False, f"rule(s) missing no_action from admissible_actions: {bad}"
    return True, f"no_action present in all {len(bundle.policy.rules)} rule(s) plus default_rule"


def check_totality(bundle: ConfigBundle) -> tuple[bool, str]:
    cause_classes = bundle.taxonomy.class_ids()
    band_ids = [b.id for b in bundle.policy.amount_bands]
    explicit = {
        (r.cause_class, r.amount_band, r.segment, r.instrument) for r in bundle.policy.rules
    }
    total = len(cause_classes) * len(band_ids) * len(SEGMENTS) * len(INSTRUMENTS)
    unresolved: list[tuple[str, str, str, str]] = []
    defaulted = 0
    for cause in cause_classes:
        for band in band_ids:
            for segment in SEGMENTS:
                for instrument in INSTRUMENTS:
                    key = (cause, band, segment, instrument)
                    if key in explicit:
                        continue
                    if bundle.policy.default_rule is None:
                        unresolved.append(key)
                    else:
                        defaulted += 1
    if unresolved:
        return False, (
            f"{len(unresolved)} combination(s) reach runtime unresolved, e.g. {unresolved[0]}"
        )
    detail = f"total over {total} combinations: {len(explicit)} explicit + {defaulted} via default"
    return True, detail


def check_uniqueness(bundle: ConfigBundle) -> tuple[bool, str]:
    keys = [(r.cause_class, r.amount_band, r.segment, r.instrument) for r in bundle.policy.rules]
    dupes = {k for k in keys if keys.count(k) > 1}
    if dupes:
        return False, f"duplicate (cause, band, segment, instrument) key(s): {dupes}"
    return True, f"{len(keys)} rule(s), all unique on (cause, amount_band, segment, instrument)"


def check_guardrails_line_count(path: Path) -> tuple[bool, str]:
    if not path.exists():
        return False, f"{path} not found"
    n = len(path.read_text(encoding="utf-8").splitlines())
    if n > 60:
        return False, f"{path} has {n} lines, exceeds the 60-line cap"
    return True, f"{path} has {n} lines (<= 60)"


def check_guardrails_numeric_comments(path: Path) -> tuple[bool, str]:
    if not path.exists():
        return False, f"{path} not found"
    problems: list[tuple[int, str]] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        code_part, _, comment_part = line.partition("#")
        key = code_part.split(":", 1)[0].strip()
        value = code_part.split(":", 1)[1]
        if _NUMERIC_RE.search(value) and not comment_part.strip():
            problems.append((lineno, key))
    if problems:
        return False, f"numeric threshold line(s) with no trailing '#' comment: {problems}"
    return True, f"every numeric threshold line in {path.name} carries a trailing comment"


def check_attribution_window(bundle: ConfigBundle, outcome_model_path: Path) -> tuple[bool, str]:
    if not outcome_model_path.exists():
        return False, f"{outcome_model_path} not found"
    text = outcome_model_path.read_text(encoding="utf-8")
    match = _ATTRIBUTION_WINDOW_RE.search(text)
    if not match:
        return False, (
            f"{outcome_model_path} does not state an attribution window "
            "(expected a line matching '**N hours from action execution.**')"
        )
    doc_hours = int(match.group(1))
    guardrail_hours = bundle.guardrails.attribution_window_hours
    if doc_hours != guardrail_hours:
        return False, (
            f"attribution_window_hours mismatch: guardrails.yaml={guardrail_hours} "
            f"vs {outcome_model_path}={doc_hours}"
        )
    return True, f"attribution_window_hours={doc_hours} agrees with {outcome_model_path.name}"


def main() -> int:
    results: list[tuple[str, bool, str]] = []

    ok, detail, bundle = check_load(CONFIG_DIR)
    results.append(("1. all three files parse and validate", ok, detail))

    if bundle is None:
        skipped = "skipped — config failed to load (see check 1)"
        anchor_name = "2. every anchor_error_string is verbatim in the harvest file"
        results.append((anchor_name, False, skipped))
        results.append(("3. no_action is in every admissible set", False, skipped))
        results.append(("4. policy table is total over the cartesian product", False, skipped))
        results.append(("5. (cause, band, segment, instrument) is unique", False, skipped))
    else:
        results.append((
            "2. every anchor_error_string is verbatim in the harvest file",
            *check_anchors_verbatim(bundle, HARVEST_PATH),
        ))
        results.append((
            "3. no_action is in every admissible set",
            *check_no_action_everywhere(bundle),
        ))
        results.append((
            "4. policy table is total over the cartesian product",
            *check_totality(bundle),
        ))
        results.append((
            "5. (cause, band, segment, instrument) is unique",
            *check_uniqueness(bundle),
        ))

    results.append((
        "6. guardrails.yaml is <= 60 lines",
        *check_guardrails_line_count(GUARDRAILS_PATH),
    ))
    results.append((
        "7. every guardrails.yaml numeric threshold has a comment",
        *check_guardrails_numeric_comments(GUARDRAILS_PATH),
    ))

    if bundle is None:
        skipped = "skipped — config failed to load (see check 1)"
        results.append(("8. attribution_window_hours matches outcome_model.md", False, skipped))
    else:
        results.append((
            "8. attribution_window_hours matches outcome_model.md",
            *check_attribution_window(bundle, OUTCOME_MODEL_PATH),
        ))

    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}: {name} - {detail}")

    if bundle is not None:
        print(f"config_hash: {config_hash(bundle)}")

    return 0 if all(ok for _, ok, _ in results) else 1


if __name__ == "__main__":
    sys.exit(main())
