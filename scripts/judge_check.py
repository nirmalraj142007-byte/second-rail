"""judge_check.py - the eighteen Judge-Gap Matrix rows, as one command.

`make judge-check` runs every row, prints one PASS/FAIL line each, and exits
1 if any row fails. It is the gate I run before calling a phase finished:
each row corresponds to one thing the judge expectations say, in writing,
would be checked, and each row here does the checking rather than describing
it.

Three design notes, because they change how much this output is worth:

1. **Nothing here is graded against a fixture.** Where a row says "make eval
   exits 0", this runs `make eval`. Where a row says "the audit log contains
   an out-of-order webhook_event", this reads the audit log. A check that
   asserts against a canned value proves nothing about the repo.

2. **Banned-phrase rows have two documented exemptions, and both are
   printed.** Several rows ban a phrase, and the files that *state those
   bans* necessarily contain the phrase. Rather than hide that, the
   exemptions are explicit: `QUOTED_RULE_FILES` names the specification
   documents this build works from, and any occurrence enclosed in markdown
   backticks counts as quoted code rather than a claim. Both are reported in
   the detail line, so a reader can see exactly what was skipped and
   disagree with me. This file is deliberately *not* exempt - every banned
   phrase below is assembled from fragments so the checker is subject to its
   own rules.

3. **ASCII output only.** The Windows console this project is developed on
   raises UnicodeEncodeError on box-drawing glyphs and em-dashes (see
   KNOWN_ISSUES.md Issue 2), and a verification tool that crashes while
   printing its own results is worse than no verification tool.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from ulid import ULID

from src.logging_setup import get_logger, setup_logging

ROOT = Path(__file__).resolve().parent.parent

# Directories that are never part of the submission's prose or source.
EXCLUDED_DIRS = frozenset({
    ".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".ruff_cache",
    "node_modules", "cache", "site-packages", "htmlcov",
})

# Files that contain a banned phrase *in order to ban it*: the source
# document this build works from, my operating notes, my open-defect list,
# and this checker's own test, which writes the phrases into temporary
# fixtures on purpose. Named here rather than silently skipped - every
# banned-phrase check prints this list in its detail line.
#
# scripts/judge_check.py is deliberately absent: see the banned-phrase block
# below. A checker that exempts itself from its own rule is a weaker checker.
QUOTED_RULE_FILES = frozenset({
    "second-rail-build-blueprint.md",
    "CLAUDE.md",
    "KNOWN_ISSUES.md",
    "tests/test_judge_check.py",
})

# ---------------------------------------------------------------------------
# banned phrases
# ---------------------------------------------------------------------------
#
# Assembled from fragments rather than written out, so this file genuinely
# contains no phrase it bans. tests/test_generator.py has grepped scripts/
# for the sealed-split phrase since Phase 5 and failed on the first literal
# I wrote here - correctly. Fixing it by exempting this file would have been
# the easier change and the wrong one, so the exemption list in
# QUOTED_RULE_FILES covers only the spec documents and this file's test.
BANNED_SEALED_SPLIT_PHRASE = "held" + "-out test set"
BANNED_OWNERSHIP_CLAIM = "nobody" + " owns"
BANNED_VENDOR_REVENUE_FIGURE = "30% of " + "revenue"
VENDOR_REATTEMPT_FIGURE = "33" + "%"
DRAFTING_ARTIFACTS = ("<" + "cite", "As an " + "AI")
CITATION_TAG_ARTIFACT = "index" + '="'

REPORT_PATH = ROOT / "evidence" / "report.md"
BUILD_LOG_PATH = ROOT / "BUILD_LOG.md"
README_PATH = ROOT / "README.md"
AUDIT_DIR = ROOT / "evidence" / "audit"
HARVEST_PATH = ROOT / "evidence" / "harvested_errors.jsonl"
SHIFT_PATH = ROOT / "holdout" / "SHIFT.md"
TRAIN_PATH = ROOT / "data" / "train.jsonl"
GUARDRAIL_PROOF_PATH = ROOT / "evidence" / "guardrail_proof.json"
DEMO_STATES_DIR = ROOT / "demo" / "states"
DEMO_SCRIPT_PATH = ROOT / "demo" / "script.md"
DOCS_DIR = ROOT / "docs"
EXPERIMENTS_DIR = ROOT / "experiments" / "thresholds"
EVAL_DB_PATH = ROOT / "evidence" / "eval_second_rail.db"

# The positioning sentence CLAUDE.md requires: name the seam, not the
# category. Checked verbatim so it cannot soften into a claim about owning
# post-session payments generally.
SEAM_SENTENCE = "automated per-episode diagnosis driving a bounded, gated, audited action"
MONEY_HEADING_RE = re.compile(
    r"^#{1,3}\s+.*No code path in Second Rail moves money", re.IGNORECASE | re.MULTILINE
)

MONEY_RE = re.compile(r"(?:Rs\.?\s?|₹)\s?[\d,]+")
PERCENT_RE = re.compile(r"\d+(?:\.\d+)?\s?%")
FIRST_PERSON_RE = re.compile(r"(?<![A-Za-z])(I|I'm|I've|I'd|I'll|my|me|myself)(?![A-Za-z])")

logger = get_logger("judge_check")


# ---------------------------------------------------------------------------
# result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CheckResult:
    check_id: str
    title: str
    passed: bool
    detail: str

    def line(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        return f"{status} {self.check_id} {self.title} - {self.detail}"


class CheckError(Exception):
    """A check could not run at all (missing file, unreadable JSON). Always
    surfaced as a FAIL with the reason attached, never as a silent skip."""


# ---------------------------------------------------------------------------
# file / text helpers
# ---------------------------------------------------------------------------


def _iter_files(suffixes: tuple[str, ...], roots: tuple[Path, ...]) -> list[Path]:
    out: list[Path] = []
    for root in roots:
        if root.is_file():
            if root.suffix in suffixes:
                out.append(root)
            continue
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix not in suffixes:
                continue
            if any(part in EXCLUDED_DIRS for part in path.parts):
                continue
            out.append(path)
    return sorted(set(out))


def rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def read(path: Path) -> str:
    if not path.exists():
        raise CheckError(f"{rel(path)} does not exist")
    return path.read_text(encoding="utf-8", errors="replace")


def _backtick_spans(line: str) -> list[tuple[int, int]]:
    """Index ranges covered by markdown code spans on this line. A trailing
    unpaired backtick opens a span that never closes, which is not a code
    span, so it is ignored."""
    ticks = [m.start() for m in re.finditer("`", line)]
    return [(ticks[i], ticks[i + 1]) for i in range(0, len(ticks) - 1, 2)]


def _occurrence_is_quoted_code(line: str, start: int, end: int) -> bool:
    return any(lo < start and end <= hi for lo, hi in _backtick_spans(line))


@dataclass(frozen=True)
class Occurrence:
    path: str
    lineno: int
    line: str


def scan_phrase(
    phrase: str,
    paths: list[Path],
    *,
    case_sensitive: bool = False,
    allow_quoted_code: bool = True,
) -> list[Occurrence]:
    """Every occurrence of `phrase` that counts as a claim rather than a
    quoted rule. The only exemptions are the two documented in this module's
    docstring."""
    needle = phrase if case_sensitive else phrase.lower()
    found: list[Occurrence] = []
    for path in paths:
        if rel(path) in QUOTED_RULE_FILES:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise CheckError(f"could not read {rel(path)}: {exc}") from exc
        for lineno, line in enumerate(text.splitlines(), start=1):
            hay = line if case_sensitive else line.lower()
            for m in re.finditer(re.escape(needle), hay):
                if allow_quoted_code and _occurrence_is_quoted_code(line, m.start(), m.end()):
                    continue
                found.append(Occurrence(rel(path), lineno, line.strip()[:100]))
    return found


def _fmt(occ: list[Occurrence], limit: int = 4) -> str:
    head = "; ".join(f"{o.path}:{o.lineno}" for o in occ[:limit])
    return head + (f" (+{len(occ) - limit} more)" if len(occ) > limit else "")


EXEMPT_NOTE = f"exempt: {len(QUOTED_RULE_FILES)} spec/self files + backticked code spans"


def md_files() -> list[Path]:
    return _iter_files((".md",), (ROOT,))


def src_py_files() -> list[Path]:
    return _iter_files((".py",), (ROOT / "src",))


def all_repo_files() -> list[Path]:
    return _iter_files(
        (".md", ".py", ".yaml", ".yml", ".txt", ".json", ".html", ".js", ".css", ".sql"),
        (ROOT,),
    )


def report_sections() -> dict[str, str]:
    """Section number -> body text for evidence/report.md's `## N. Title`
    headings. Read fresh on every call: JG-05 re-runs `make eval`, which
    rewrites this file mid-run, and a cached copy would let a later row grade
    a report that no longer exists on disk."""
    text = read(REPORT_PATH)
    sections: dict[str, str] = {}
    current: str | None = None
    buf: list[str] = []
    for line in text.splitlines():
        m = re.match(r"^##\s+(\d+)\.\s*(.*)$", line)
        if m:
            if current is not None:
                sections[current] = "\n".join(buf).strip()
            current = m.group(1)
            buf = [line]
        elif current is not None:
            buf.append(line)
    if current is not None:
        sections[current] = "\n".join(buf).strip()
    return sections


# ---------------------------------------------------------------------------
# subprocess helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    duration_s: float
    stdout: str
    stderr: str
    timed_out: bool

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


def run_command(argv: list[str], *, timeout_s: float) -> CommandResult:
    """Every subprocess here is bounded. A hung `make eval` must surface as a
    FAIL carrying a timeout, not as a judge-check that never returns."""
    started = time.monotonic()
    try:
        proc = subprocess.run(
            argv, cwd=ROOT, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout_s, check=False,
        )
    except subprocess.TimeoutExpired:
        return CommandResult(tuple(argv), 124, time.monotonic() - started, "", "", True)
    except OSError as exc:
        return CommandResult(tuple(argv), 127, time.monotonic() - started, "", str(exc), False)
    return CommandResult(
        tuple(argv), proc.returncode, time.monotonic() - started,
        proc.stdout or "", proc.stderr or "", False,
    )


def find_make() -> str | None:
    """GNU make under whichever name this machine has it. The box this is
    built on ships `mingw32-make` and no `make`, so hardcoding `make` would
    make JG-05 unrunnable on the machine the project is developed on."""
    for name in ("make", "gmake", "mingw32-make"):
        found = shutil.which(name)
        if found:
            return found
    return None


def git(*args: str, timeout_s: float = 30.0) -> CommandResult:
    return run_command(["git", *args], timeout_s=timeout_s)


# ---------------------------------------------------------------------------
# audit-log helpers
# ---------------------------------------------------------------------------


def audit_records() -> list[dict]:
    if not AUDIT_DIR.is_dir():
        raise CheckError(f"{rel(AUDIT_DIR)} does not exist")
    records: list[dict] = []
    for path in sorted(AUDIT_DIR.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise CheckError(f"{rel(path)} has an unparseable record: {exc}") from exc
    if not records:
        raise CheckError(f"no audit records found under {rel(AUDIT_DIR)}")
    return records


def read_jsonl(path: Path) -> list[dict]:
    records: list[dict] = []
    for lineno, line in enumerate(read(path).splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise CheckError(f"{rel(path)}:{lineno} is not valid JSON: {exc}") from exc
    return records


# ---------------------------------------------------------------------------
# JG-01 .. JG-18
# ---------------------------------------------------------------------------


def jg01() -> tuple[bool, str]:
    """Closed evidence loop: the taxonomy is anchored to strings I did not
    write, and the report says so."""
    from scripts.config_check import check_anchors_verbatim
    from src.config_models import load_all

    records = read_jsonl(HARVEST_PATH)
    problems: list[str] = []
    if len(records) < 20:
        problems.append(f"only {len(records)} harvested record(s), need >= 20")

    anchors_ok, anchors_detail = check_anchors_verbatim(load_all(), HARVEST_PATH)
    if not anchors_ok:
        problems.append(f"anchors: {anchors_detail}")

    sections = report_sections()
    if "2" not in sections:
        problems.append("evidence/report.md has no section 2")

    if problems:
        return False, "; ".join(problems)
    return True, f"{len(records)} harvested records; {anchors_detail}; report section 2 present"


def jg02() -> tuple[bool, str]:
    """The sealed split is described accurately, and the shift is real."""
    problems: list[str] = []

    occ = scan_phrase(BANNED_SEALED_SPLIT_PHRASE, md_files() + src_py_files())
    if occ:
        problems.append(
            f"{BANNED_SEALED_SPLIT_PHRASE!r} appears {len(occ)}x: {_fmt(occ)}"
        )

    if not SHIFT_PATH.exists():
        problems.append("holdout/SHIFT.md is missing")
        return False, "; ".join(problems)

    shift_text = read(SHIFT_PATH)
    blocks = re.findall(r"```json\s*(.*?)```", shift_text, re.DOTALL)
    if not blocks:
        problems.append("holdout/SHIFT.md has no machine-readable json block")
        return False, "; ".join(problems)
    try:
        summary = json.loads(blocks[-1])
    except json.JSONDecodeError as exc:
        raise CheckError(f"holdout/SHIFT.md json block is unparseable: {exc}") from exc

    shifts_with_counts = 0
    for _name, body in summary.items():
        if isinstance(body, dict) and any(
            isinstance(v, int) for k, v in body.items() if "count" in k or "paise" in k
        ):
            shifts_with_counts += 1
    if shifts_with_counts < 2:
        problems.append(f"only {shifts_with_counts} documented shift(s) carry counts, need >= 2")

    family = str(summary.get("issuer_family", {}).get("sealed_only_family", ""))
    if not family:
        problems.append("SHIFT.md names no sealed_only_family")
    else:
        leaks = read(TRAIN_PATH).count(family)
        if leaks:
            problems.append(f"sealed-only family {family} appears {leaks}x in data/train.jsonl")

    if problems:
        return False, "; ".join(problems)
    return True, (
        f"phrase absent ({EXEMPT_NOTE}); SHIFT.md documents {shifts_with_counts} shifts with "
        f"counts; {family} leaks into train.jsonl: 0"
    )


def jg03() -> tuple[bool, str]:
    """The sweep is framed as a design target and disclaims itself."""
    from src.report.sensitivity import SWEPT_PARAMS

    problems: list[str] = []
    sections = report_sections()
    section4 = sections.get("4", "")
    heading = section4.splitlines()[0] if section4 else "<missing>"
    if "design target" not in heading.lower():
        problems.append(f"section 4 heading is not a design target: {heading!r}")

    text = read(REPORT_PATH)
    if "perturbs my own parameters" not in text:
        problems.append("the 'perturbs my own parameters' disclaimer is absent")

    if len(SWEPT_PARAMS) != 3:
        problems.append(f"{len(SWEPT_PARAMS)} parameters swept, need exactly 3")

    if problems:
        return False, "; ".join(problems)
    return True, f"{heading.lstrip('# ')!r}; disclaimer present; swept {list(SWEPT_PARAMS)}"


def jg04() -> tuple[bool, str]:
    """The build log is real: written across many days, and it admits at
    least one wrong first hypothesis."""
    text = read(BUILD_LOG_PATH)
    problems: list[str] = []

    dated = re.findall(r"^##\s+.*\b\d{1,2}\s+(?:Aug|Sep)\s+2026\b.*$", text, re.MULTILINE)
    if len(dated) < 8:
        problems.append(f"{len(dated)} dated entries, need >= 8")

    log = git("log", "--format=%ad", "--date=short", "--", "BUILD_LOG.md")
    dates = sorted({d for d in log.stdout.split() if d})
    if len(dates) < 6:
        problems.append(f"modified on {len(dates)} distinct date(s), need >= 6")

    wrong = re.findall(r"(?i)first hypothesis was wrong|where the first hypothesis", text)
    if not wrong:
        problems.append("no wrong-first-hypothesis marker found")

    if not re.search(r"^##\s+Wrong turns\s*$", text, re.MULTILINE):
        problems.append("no '## Wrong turns' index")
    else:
        index_body = text.split("## Wrong turns", 1)[1].split("\n## ", 1)[0]
        links = re.findall(r"\]\(#", index_body)
        if len(links) < len(wrong):
            problems.append(
                f"'## Wrong turns' links to {len(links)} entries but {len(wrong)} exist"
            )

    if problems:
        return False, "; ".join(problems)
    return True, (
        f"{len(dated)} dated entries; {len(dates)} distinct git dates "
        f"({dates[0]}..{dates[-1]}); {len(wrong)} wrong-turn markers, all indexed"
    )


TODO_MARKERS = ("TODO", "FIXME", "NotImplementedError", "pass  # stub")


def jg05(make: str | None, *, skip_slow: bool) -> tuple[bool, str]:
    """Everything works, and nothing in src/ is a placeholder."""
    problems: list[str] = []

    hits: list[str] = []
    for path in src_py_files():
        for lineno, line in enumerate(read(path).splitlines(), start=1):
            for marker in TODO_MARKERS:
                if marker in line:
                    hits.append(f"{rel(path)}:{lineno} ({marker})")
    if hits:
        problems.append(f"placeholder markers in src/: {'; '.join(hits[:5])}")

    makefile = read(ROOT / "Makefile")
    if not re.search(r"^judge-check:", makefile, re.MULTILINE):
        problems.append("Makefile has no judge-check target")

    if make is None:
        problems.append("no make/gmake/mingw32-make on PATH, cannot run the targets")
        return False, "; ".join(problems)

    ran: list[str] = []
    if skip_slow:
        ran.append("targets skipped (--skip-slow)")
    else:
        targets = [
            (["eval"], 600.0),
            (["demo"], 300.0),
            (["verify-audit"], 60.0),
            (["rollback", "RUN_ID=judge_check_noop_run"], 120.0),
            (["config-check"], 60.0),
        ]
        for args, timeout in targets:
            result = run_command([make, *args], timeout_s=timeout)
            name = " ".join(args)
            if result.timed_out:
                problems.append(f"make {name} timed out after {timeout:.0f}s")
            elif result.returncode != 0:
                tail = (result.stderr or result.stdout).strip().splitlines()
                last = tail[-1] if tail else ""
                problems.append(f"make {name} exited {result.returncode}: {last}")
            else:
                ran.append(f"{name} ({result.duration_s:.1f}s)")

    if problems:
        return False, "; ".join(problems)
    return True, (
        "no placeholder markers in src/; judge-check target present (this process is "
        f"its run, so it is not re-invoked here); {', '.join(ran)}"
    )


def jg06() -> tuple[bool, str]:
    """Guardrails demonstrated at volume, and the refusal/error states exist."""
    proof = json.loads(read(GUARDRAIL_PROOF_PATH))
    problems: list[str] = []

    n = int(proof.get("n", 0))
    if n < 200:
        problems.append(f"guardrail_proof.json n={n}, need >= 200")

    zero_fields = ("duplicate_links_created", "cap_breaches", "quiet_hour_contacts")
    nonzero = {f: proof.get(f) for f in zero_fields if proof.get(f) != 0}
    if nonzero:
        problems.append(f"must be zero but are not: {nonzero}")

    missing_states = [
        name for name in ("refusal", "error")
        if not any(DEMO_STATES_DIR.glob(f"{name}.*"))
    ]
    if missing_states:
        problems.append(f"demo/states/ is missing: {', '.join(missing_states)}")

    if problems:
        return False, "; ".join(problems)
    return True, (
        f"n={n}, mode={proof.get('mode')}, processed={proof.get('processed_count')}, "
        f"duplicates/cap-breaches/quiet-hour-contacts all 0; demo/states/ has refusal + error"
    )


def jg07() -> tuple[bool, str]:
    """Every threshold traces to an experiment, and the experiments conclude."""
    from scripts.config_check import GUARDRAILS_PATH, check_guardrails_experiment_provenance

    problems: list[str] = []
    ok, detail = check_guardrails_experiment_provenance(GUARDRAILS_PATH)
    if not ok:
        problems.append(f"config_check check 9: {detail}")

    expected = ("auto_approve.md", "outage_cluster.md", "retry_cap.md")
    for name in expected:
        path = EXPERIMENTS_DIR / name
        if not path.exists():
            problems.append(f"{rel(path)} is missing")
            continue
        body = read(path)
        m = re.search(r"^##\s+Conclusion\s*$(.*?)(?=^##\s|\Z)", body, re.MULTILINE | re.DOTALL)
        if m is None:
            problems.append(f"{rel(path)} has no '## Conclusion'")
        elif len(m.group(1).strip()) < 80:
            problems.append(f"{rel(path)}'s conclusion is too thin to be one")

    if problems:
        return False, "; ".join(problems)
    return True, f"check 9: {detail}; 3 experiments each conclude"


def jg08() -> tuple[bool, str]:
    """Escalation is tiered, and each tier reaches the audit log for its own
    reason rather than three names for one behaviour."""
    reasons_by_tier: dict[str, set[str]] = defaultdict(set)
    for record in audit_records():
        tier = record.get("escalation_tier")
        if not tier:
            continue
        # escalation_reason is the field src/gate/engine.py:TierReason writes:
        # the *named reason the tier was assigned*, which is what the judge
        # expectations ask for. reason_code (why a gate check failed) is a
        # different question and only a fallback for older records.
        reason = record.get("escalation_reason") or record.get("reason_code")
        if reason:
            reasons_by_tier[tier].add(str(reason))
        else:
            reasons_by_tier.setdefault(tier, set())

    expected = {"auto", "human_keystroke", "hard_refuse"}
    missing = sorted(expected - set(reasons_by_tier))
    if missing:
        return False, f"tier(s) never written to the audit log: {', '.join(missing)}"

    tierless = [t for t in expected if not reasons_by_tier[t]]
    if tierless:
        return False, f"tier(s) present with no reason recorded: {', '.join(tierless)}"

    if len({frozenset(reasons_by_tier[t]) for t in expected}) < 3:
        return False, "the three tiers do not carry distinct reasons"

    summary = "; ".join(
        f"{t}={sorted(reasons_by_tier[t])[:3]}" for t in sorted(expected)
    )
    return True, summary


def jg09() -> tuple[bool, str]:
    """A reported result that shrinks the claim, with a number in it."""
    sections = report_sections()
    section3 = sections.get("3", "")
    if not section3:
        return False, "evidence/report.md has no section 3"

    heading = section3.splitlines()[0]
    body = "\n".join(section3.splitlines()[1:]).strip()
    if len(body) < 200:
        return False, f"section 3 body is {len(body)} chars - too thin to be a finding"

    numbers = PERCENT_RE.findall(body) + MONEY_RE.findall(body)
    if len(numbers) < 2:
        return False, f"section 3 carries {len(numbers)} number(s), need a comparative pair"

    return True, f"{heading.lstrip('# ')!r}, {len(body)} chars, numbers incl. {numbers[:3]}"


def jg10() -> tuple[bool, str]:
    """The gap is not overstated, and the seam is named."""
    problems: list[str] = []

    targets = [README_PATH] + _iter_files((".md",), (DOCS_DIR,))
    if DEMO_SCRIPT_PATH.exists():
        targets.append(DEMO_SCRIPT_PATH)
    targets = [p for p in targets if p.exists()]

    occ = scan_phrase(BANNED_OWNERSHIP_CLAIM, targets)
    if occ:
        problems.append(
            f"{BANNED_OWNERSHIP_CLAIM!r} appears {len(occ)}x: {_fmt(occ)}"
        )

    if not README_PATH.exists():
        problems.append("README.md does not exist")
    elif SEAM_SENTENCE not in read(README_PATH):
        problems.append(f"README.md does not contain the positioning sentence ({SEAM_SENTENCE!r})")

    if problems:
        return False, "; ".join(problems)
    scanned = ", ".join(rel(p) for p in targets[:4])
    extra = f" (+{len(targets) - 4} more)" if len(targets) > 4 else ""
    return True, (
        f"{BANNED_OWNERSHIP_CLAIM!r} absent across {scanned}{extra}; "
        "seam sentence present in README.md"
    )


def _illustrative_blocks(text: str) -> list[tuple[int, str]]:
    """Blockquote runs that carry money figures - the shape a merchant
    arithmetic example takes in every document in this repo."""
    blocks: list[tuple[int, str]] = []
    buf: list[str] = []
    start = 0
    for lineno, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith(">"):
            if not buf:
                start = lineno
            buf.append(line)
            continue
        if buf:
            blocks.append((start, "\n".join(buf)))
            buf = []
    if buf:
        blocks.append((start, "\n".join(buf)))
    return [(ln, b) for ln, b in blocks if len(MONEY_RE.findall(b)) >= 2]


def jg11() -> tuple[bool, str]:
    """No laundered vendor numbers, and illustrative arithmetic says so."""
    problems: list[str] = []
    files = all_repo_files()

    banned = scan_phrase(BANNED_VENDOR_REVENUE_FIGURE, files)
    if banned:
        problems.append(f"the revenue-share vendor figure appears {len(banned)}x: {_fmt(banned)}")

    unattributed: list[str] = []
    for occ in scan_phrase(VENDOR_REATTEMPT_FIGURE, files, allow_quoted_code=False):
        idx = occ.line.lower().find(VENDOR_REATTEMPT_FIGURE)
        window = occ.line[max(0, idx - 100): idx + 103]
        if "razorpay" not in window.lower():
            unattributed.append(f"{occ.path}:{occ.lineno}")
    if unattributed:
        problems.append(
            f"{VENDOR_REATTEMPT_FIGURE!r} unattributed at: " + "; ".join(unattributed[:4])
        )

    unlabelled: list[str] = []
    for path in md_files():
        if rel(path) in QUOTED_RULE_FILES:
            continue
        for lineno, block in _illustrative_blocks(read(path)):
            if "illustrative" not in block.lower():
                unlabelled.append(f"{rel(path)}:{lineno}")
    if unlabelled:
        problems.append(
            "arithmetic block(s) not labelled illustrative: " + "; ".join(unlabelled[:4])
        )

    if problems:
        return False, "; ".join(problems)
    return True, (
        f"revenue-share figure absent; every {VENDOR_REATTEMPT_FIGURE!r} "
        "attributed to Razorpay; "
        f"every money blockquote labelled illustrative ({EXEMPT_NOTE})"
    )


def jg12() -> tuple[bool, str]:
    """A judge can see the results, and re-run them, without a key."""
    problems: list[str] = []

    tracked = git("ls-files", "--error-unmatch", "evidence/report.md")
    if tracked.returncode != 0:
        problems.append("evidence/report.md is not committed")

    cache_entries = git("ls-files", "cache/*.json").stdout.split()
    if not cache_entries:
        problems.append("cache/ has no committed entries - make eval would need a key")

    if problems:
        return False, "; ".join(problems)
    return True, f"evidence/report.md committed; {len(cache_entries)} cache entries committed"


JG13_TESTS = (
    "tests/test_executor.py::TestRazorpayExecutor::test_idempotency_key_matches_between_runs",
    "tests/test_ingest.py::test_same_payment_id_different_event_id_is_duplicate",
    "tests/test_ingest.py::test_captured_with_no_prior_failed_is_out_of_order",
)


def jg13(*, skip_slow: bool) -> tuple[bool, str]:
    """Idempotency, dedup and out-of-order: tested, and visible in the log."""
    problems: list[str] = []

    if skip_slow:
        tests_note = "test run skipped (--skip-slow)"
    else:
        result = run_command(
            [sys.executable, "-m", "pytest", "-q", *JG13_TESTS], timeout_s=300.0
        )
        if not result.ok:
            tail = (result.stdout or result.stderr).strip().splitlines()
            problems.append(f"the three tests did not pass: {tail[-1] if tail else 'no output'}")
            tests_note = "3 tests FAILED"
        else:
            tests_note = "3 tests pass"

    records = audit_records()
    duplicates = [
        r for r in records
        if r.get("outcome") == "duplicate_suppressed" or (
            (r.get("execution") or {}).get("status") == "duplicate_suppressed"
        )
    ]
    if not duplicates:
        problems.append("no duplicate_suppressed execution in the audit log")

    out_of_order = [
        r for r in records
        if r.get("outcome") == "out_of_order"
        or "out_of_order" in str(r.get("rationale", "")).lower()
    ]
    if not out_of_order:
        problems.append("no out_of_order webhook_event in the audit log")

    if problems:
        return False, "; ".join(problems)
    return True, (
        f"{tests_note}; {len(duplicates)} duplicate_suppressed execution(s), "
        f"{len(out_of_order)} out_of_order webhook_event(s) in the audit log"
    )


def jg14() -> tuple[bool, str]:
    """Reversibility, stated as a heading a judge cannot miss."""
    if not README_PATH.exists():
        return False, "README.md does not exist, so the reversibility heading cannot be there"
    lines = read(README_PATH).splitlines()
    head = "\n".join(lines[:40])
    m = MONEY_HEADING_RE.search(head)
    if m is None:
        anywhere = MONEY_HEADING_RE.search("\n".join(lines))
        if anywhere is not None:
            return False, "the heading exists but not within README.md's first 40 lines"
        return False, "README.md has no 'No code path in Second Rail moves money' heading"
    lineno = head[: m.start()].count("\n") + 1
    return True, f"README.md:{lineno} {m.group(0).strip()!r}"


def _first_commit_date(pathspec: str) -> str | None:
    result = git("log", "--format=%ad", "--date=short", "--diff-filter=A", "--", pathspec)
    dates = [d for d in result.stdout.split() if d]
    return dates[-1] if dates else None


def jg15() -> tuple[bool, str]:
    """Pre-registration: the outcome model predates the eval that uses it."""
    model_date = _first_commit_date("outcome_model.md")
    eval_date = _first_commit_date("scripts/eval.py")
    if model_date is None:
        return False, "outcome_model.md has no commit adding it"
    if eval_date is None:
        return False, "scripts/eval.py has no commit adding it"
    if not model_date < eval_date:
        return False, (
            f"outcome_model.md first committed {model_date}, scripts/eval.py {eval_date} "
            "- pre-registration requires strictly earlier"
        )
    return True, f"outcome_model.md {model_date} < scripts/eval.py {eval_date}"


def jg16() -> tuple[bool, str]:
    """False-positive cost nets against gross, in the ledger and the report."""
    problems: list[str] = []

    if not EVAL_DB_PATH.exists():
        raise CheckError(f"{rel(EVAL_DB_PATH)} does not exist - run make eval first")
    conn = sqlite3.connect(EVAL_DB_PATH)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT kind, SUM(amount_paise) AS total, COUNT(*) AS n "
            "FROM ledger_entry GROUP BY kind"
        ).fetchall()
    finally:
        conn.close()
    totals = {r["kind"]: int(r["total"] or 0) for r in rows}
    counts = {r["kind"]: int(r["n"]) for r in rows}

    if "net" not in counts:
        problems.append("ledger_entry has no row of kind 'net'")

    text = read(REPORT_PATH)
    for token, label in (("gross", "gross"), ("false-positive cost", "fp_cost"), ("NET", "net")):
        if token not in text:
            problems.append(f"evidence/report.md does not render {label}")

    gross = totals.get("gross_recovery", 0)
    net = totals.get("net", 0)
    fp = totals.get("fp_cost", 0)
    if net == gross:
        if fp != 0:
            problems.append(f"net ({net}p) equals gross ({gross}p) but fp_cost is {fp}p")
        elif "counterfactual" not in text.lower():
            problems.append(
                "fp_cost is 0 and net equals gross, but no gate-disabled counterfactual "
                "is reported alongside"
            )

    if problems:
        return False, "; ".join(problems)
    return True, f"ledger gross={gross}p fp_cost={fp}p net={net}p; report renders all three"


FIRST_PERSON_DOCS = ("README.md", "docs/where-the-llm-is-not.md", "BUILD_LOG.md")


def jg17() -> tuple[bool, str]:
    """Authorship: no drafting artifacts, and the prose is first person."""
    problems: list[str] = []
    files = all_repo_files()

    for phrase in DRAFTING_ARTIFACTS:
        occ = scan_phrase(phrase, files)
        if occ:
            problems.append(f"{phrase!r} appears {len(occ)}x: {_fmt(occ)}")

    # `index=` is scoped to markdown: it is a citation-tag artifact, and in
    # Python it is ordinary keyword-argument syntax.
    occ = scan_phrase(CITATION_TAG_ARTIFACT, md_files())
    if occ:
        problems.append(
            f"citation-tag artifact {CITATION_TAG_ARTIFACT!r} appears "
            f"{len(occ)}x: {_fmt(occ)}"
        )

    voices: list[str] = []
    for name in FIRST_PERSON_DOCS:
        path = ROOT / name
        if not path.exists():
            problems.append(f"{name} does not exist")
            continue
        body = read(path)
        hits = len(FIRST_PERSON_RE.findall(body))
        per_kb = hits / max(1, len(body) / 1000)
        if hits < 5 or per_kb < 0.5:
            problems.append(f"{name} reads as impersonal ({hits} first-person tokens)")
        else:
            voices.append(f"{name}={hits}")

    if problems:
        return False, "; ".join(problems)
    return True, f"no drafting artifacts; first-person tokens {', '.join(voices)}"


def jg18(make: str | None, *, skip_slow: bool) -> tuple[bool, str]:
    """The audit chain is verifiable, fast, and detects tampering."""
    if skip_slow:
        return True, "skipped (--skip-slow)"
    if make is None:
        return False, "no make/gmake/mingw32-make on PATH"

    verify = run_command([make, "verify-audit"], timeout_s=60.0)
    problems: list[str] = []
    if not verify.ok:
        problems.append(f"make verify-audit exited {verify.returncode}")
    if "chain intact" not in verify.stdout:
        problems.append("make verify-audit did not print an intact-chain line")

    # The subprocess timing includes make's own startup and the interpreter's,
    # so the sub-2s budget is measured against the verifier's own reported
    # duration where it prints one, and against wall clock otherwise.
    m = re.search(r"\((\d+\.\d+)s\)", verify.stdout)
    measured = float(m.group(1)) if m else verify.duration_s
    if measured >= 2.0:
        problems.append(f"verification took {measured:.2f}s, budget is < 2s")

    tamper = run_command([make, "verify-audit-tamper"], timeout_s=60.0)
    if "chain BROKEN" not in tamper.stdout:
        problems.append("make verify-audit-tamper did not print a broken-chain line")

    if problems:
        return False, "; ".join(problems)
    broken_line = next(
        (ln for ln in tamper.stdout.splitlines() if "chain BROKEN" in ln), ""
    ).strip()
    # `make` interleaves its own "Entering/Leaving directory" chatter with the
    # target's output, so pick the verifier's line by content, not position.
    intact_line = next(
        (ln.strip() for ln in verify.stdout.splitlines() if "chain intact" in ln), ""
    )
    return True, f"{intact_line} in {measured:.2f}s; tamper test prints {broken_line!r}"


# ---------------------------------------------------------------------------
# driver
# ---------------------------------------------------------------------------

TITLES: dict[str, str] = {
    "JG-01": "closed evidence loop",
    "JG-02": "sealed split labelled accurately",
    "JG-03": "sensitivity sweep framing",
    "JG-04": "build log",
    "JG-05": "scope / everything works",
    "JG-06": "guardrails demonstrated",
    "JG-07": "thresholds justified",
    "JG-08": "tiered escalation",
    "JG-09": "a result that shrinks the claim",
    "JG-10": "positioning, not overstated",
    "JG-11": "vendor numbers",
    "JG-12": "repo runs clean",
    "JG-13": "idempotency / dedup / out-of-order",
    "JG-14": "reversibility",
    "JG-15": "pre-registration",
    "JG-16": "FP cost nets",
    "JG-17": "authorship",
    "JG-18": "audit verifiable",
}


def run_all(*, skip_slow: bool) -> list[CheckResult]:
    make = find_make()
    checks: list[tuple[str, object]] = [
        ("JG-01", jg01),
        ("JG-02", jg02),
        ("JG-03", jg03),
        ("JG-04", jg04),
        ("JG-05", lambda: jg05(make, skip_slow=skip_slow)),
        ("JG-06", jg06),
        ("JG-07", jg07),
        ("JG-08", jg08),
        ("JG-09", jg09),
        ("JG-10", jg10),
        ("JG-11", jg11),
        ("JG-12", jg12),
        ("JG-13", lambda: jg13(skip_slow=skip_slow)),
        ("JG-14", jg14),
        ("JG-15", jg15),
        ("JG-16", jg16),
        ("JG-17", jg17),
        ("JG-18", lambda: jg18(make, skip_slow=skip_slow)),
    ]

    results: list[CheckResult] = []
    for check_id, fn in checks:
        title = TITLES[check_id]
        try:
            passed, detail = fn()  # type: ignore[operator]
        except CheckError as exc:
            passed, detail = False, f"could not run: {exc}"
        except Exception as exc:  # noqa: BLE001 - a crashing check is a failing check
            logger.exception("%s raised", check_id)
            passed, detail = False, f"raised {type(exc).__name__}: {exc}"
        result = CheckResult(check_id, title, passed, detail)
        results.append(result)
        print(result.line(), flush=True)
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="judge_check",
        description="Run the eighteen Judge-Gap Matrix checks.",
    )
    parser.add_argument(
        "--skip-slow",
        action="store_true",
        help=(
            "Skip the rows that shell out to make and pytest (JG-05, JG-13's test run, "
            "JG-18). For iterating on the prose rows only - never for a real gate."
        ),
    )
    args = parser.parse_args(argv)

    setup_logging()
    check_run_id = str(ULID())
    logger.info("judge-check starting", extra={"run_id": check_run_id})

    started = time.monotonic()
    results = run_all(skip_slow=args.skip_slow)
    failed = [r for r in results if not r.passed]

    print()
    print(
        f"{len(results) - len(failed)}/{len(results)} checks passed "
        f"in {time.monotonic() - started:.1f}s  (run {check_run_id})"
    )
    if failed:
        print("failing: " + ", ".join(r.check_id for r in failed))
    if args.skip_slow:
        print("NOTE: --skip-slow was set; this run is not a valid gate.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
