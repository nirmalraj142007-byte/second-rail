"""Final artifact scan — fails the build on drafting artifacts, placeholders,
and leftover assistant-voice in the documentation set.

This exists because a hackathon panel reads the repo and then interviews the
author; a `<cite index="5">` tag or a stray "As an AI" is the fastest way to
turn "did you build this" into "did you paste this." See CLAUDE.md's "Voice
and evidence discipline".

`scripts/judge_check.py`'s `jg17` already enforces the narrower authorship
checks (`<cite`, "As an AI", the `index="` citation-tag artifact, first-person
voice) as part of the 18-row submission gate — this script reuses those exact
helpers (`scan_phrase`, `QUOTED_RULE_FILES`, the backtick-quoted-code
exemption) rather than re-implementing weaker duplicates. What this script
adds on top: `TODO`, `lorem`, `Claude`, `ChatGPT`, and
`[WRITE THIS YOURSELF]` — the marker this project's docs get scaffolded with
so a human writes the voice sections, not a model — plus a best-effort
second-person-instruction check.

Scope for the additions: the documentation set a reviewer actually reads —
README.md, LIMITATIONS.md, KNOWN_ISSUES.md, BUILD_LOG.md, outcome_model.md,
docs/*.md — not source or test files. `TODO`, `Claude`, etc. are legitimate
tokens in code (a `TODO_MARKERS` constant, a keyword argument literally named
`...index=`) and in this project's own operating docs (CLAUDE.md and the
build blueprint discuss `<cite>` tags and TODOs by name, which is why
`QUOTED_RULE_FILES` exempts them the same way `jg17` does); a scanner that
flagged those would either need those files silently excluded or would never
pass, and a check nobody can make pass isn't a checklist.

`<cite`, `As an AI`, and `[WRITE THIS YOURSELF]` are checked across the full
repo (`jg17`'s existing scope) since a drafting artifact or an unwritten
marker is wrong wherever it appears, not just in the doc set.

Run: python scripts/artifact_scan.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SELF = Path(__file__).resolve()
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from judge_check import (  # noqa: E402
    CITATION_TAG_ARTIFACT,
    DRAFTING_ARTIFACTS,
    QUOTED_RULE_FILES,
    _occurrence_is_quoted_code,
    _fmt,
    all_repo_files,
    md_files,
    rel,
    scan_phrase,
)

# Additions on top of jg17's narrower authorship scope -- see module
# docstring for why each is doc-scoped rather than repo-wide.
DOC_SCOPED_LITERALS = ("TODO", "lorem", "Claude", "ChatGPT")
# The Phase 19 prompt names the ban as the bare "[WRITE THIS YOURSELF]" tag,
# but every marker it actually specifies is written
# "[WRITE THIS YOURSELF: <instructions>]" -- content and closing bracket
# after the colon, not immediately after YOURSELF. Matching on the prefix
# (no closing bracket required) catches the real marker shape rather than a
# string that would never appear literally in a correctly-formatted one.
# Split so this file never itself flags as unwritten.
WRITE_MARKER = "[WRITE" + " THIS YOURSELF"

DOC_SET_NAMES = (
    "README.md",
    "LIMITATIONS.md",
    "KNOWN_ISSUES.md",
    "BUILD_LOG.md",
    "outcome_model.md",
)

SECOND_PERSON_PATTERN = re.compile(
    r"\b(please [a-z]+ (this|your|it|the)|you should now\b|make sure to\b|"
    r"don't forget to\b|remember to\b|note to self\b)",
    re.IGNORECASE,
)


def doc_files() -> list[Path]:
    named = [ROOT / name for name in DOC_SET_NAMES if (ROOT / name).exists()]
    doc_dir = sorted((ROOT / "docs").glob("*.md")) if (ROOT / "docs").is_dir() else []
    return sorted(set(named + doc_dir))


FIRST_PERSON_LEADIN = re.compile(r"\bi\s*$", re.IGNORECASE)


def scan_second_person(paths: list[Path]) -> list[str]:
    """"remember to" / "don't forget to" read as an instruction to the
    reader in "Remember to configure X" but not in "I remember to add the
    field" -- skip a match immediately preceded by "I " so ordinary
    first-person narration (this project's own voice) doesn't false-positive."""
    hits: list[str] = []
    for path in paths:
        if rel(path) in QUOTED_RULE_FILES:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), start=1):
            m = SECOND_PERSON_PATTERN.search(line)
            if not m or _occurrence_is_quoted_code(line, m.start(), m.end()):
                continue
            if FIRST_PERSON_LEADIN.search(line[: m.start()]):
                continue
            hits.append(f"{rel(path)}:{lineno}: [second-person: \"{m.group(0)}\"] {line.strip()[:100]}")
    return hits


def main() -> int:
    total = 0
    patterns_hit = 0

    repo_files = [p for p in all_repo_files() if p.resolve() != SELF]
    for phrase in (*DRAFTING_ARTIFACTS, WRITE_MARKER):
        occ = scan_phrase(phrase, repo_files)
        if occ:
            patterns_hit += 1
            total += len(occ)
            for o in occ:
                print(f"{o.path}:{o.lineno}: [{phrase}] {o.line}")

    occ = scan_phrase(CITATION_TAG_ARTIFACT, md_files())
    if occ:
        patterns_hit += 1
        total += len(occ)
        for o in occ:
            print(f"{o.path}:{o.lineno}: [{CITATION_TAG_ARTIFACT}] {o.line}")

    targets = doc_files()
    # Claude/ChatGPT are case-sensitive: this project's own operating file is
    # CLAUDE.md (all caps), referenced constantly by filename in prose ("per
    # CLAUDE.md's non-negotiables") -- a case-insensitive match would flag
    # every one of those as if it named the assistant.
    case_sensitive_phrases = {"Claude", "ChatGPT"}
    for phrase in DOC_SCOPED_LITERALS:
        occ = scan_phrase(phrase, targets, case_sensitive=phrase in case_sensitive_phrases)
        if occ:
            patterns_hit += 1
            total += len(occ)
            for o in occ:
                print(f"{o.path}:{o.lineno}: [{phrase}] {o.line}")

    sp_hits = scan_second_person(targets)
    if sp_hits:
        patterns_hit += 1
        total += len(sp_hits)
        for line in sp_hits:
            print(line)

    if total:
        print(f"\nartifact-scan: FAILED — {total} hit(s) across {patterns_hit} pattern(s).")
        print(f"(exempt: {len(QUOTED_RULE_FILES)} spec/self files + backticked code spans — "
              f"{', '.join(sorted(QUOTED_RULE_FILES))})")
        return 1

    print("artifact-scan: clean — 0 hits.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
