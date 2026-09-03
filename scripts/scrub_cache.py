"""Asserts cache/*.json is safe to commit.

The LLM response cache (cache/*.json, content-addressed by prompt hash) is
committed deliberately -- see .gitignore's comment on why `make eval` needs
it to run key-free on a clean clone. Before every commit that touches
cache/, this script re-checks every file for the three things that would
make committing it a mistake: a live API key, an absolute filesystem path
from the machine that generated the cache, or a real (non-synthetic)
identifier such as this project's own email or username. Exits 1 and prints
every offending file:line on the first violation category found, rather
than stopping at the first file, so a single run tells you the whole scope
of a leak.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "cache"

# API key shapes for every provider named in CLAUDE.md / .env.example.
KEY_PATTERNS: dict[str, re.Pattern[str]] = {
    "razorpay key": re.compile(r"rzp_(live|test)_[A-Za-z0-9]+"),
    "openai-shaped key": re.compile(r"sk-[A-Za-z0-9]{20,}"),
    "google/gemini key": re.compile(r"AIza[A-Za-z0-9_-]{30,}"),
    "groq key": re.compile(r"gsk_[A-Za-z0-9]{20,}"),
}

# Absolute paths only make sense as a leak signal for *this* machine's own
# home directory / username -- a bare "C:\\" or "/home/" would false-positive
# on legitimate prose inside a cached rationale string.
ABS_PATH_PATTERNS: dict[str, re.Pattern[str]] = {
    "windows user path": re.compile(r"C:\\Users\\[A-Za-z0-9_.\-]+", re.IGNORECASE),
    "posix home path": re.compile(r"/home/[A-Za-z0-9_.\-]+"),
    "macos user path": re.compile(r"/Users/[A-Za-z0-9_.\-]+"),
}

# Real identifiers that must never appear in synthetic-data-only cache
# content: this project's committed author email/username (CLAUDE.md's
# userEmail context) and the generic "@gmail.com" / other real-looking
# mailbox shape that data/generator.py's synthetic customers should never
# produce.
IDENTIFIER_PATTERNS: dict[str, re.Pattern[str]] = {
    "real email address": re.compile(r"[A-Za-z0-9._%+-]+@(?!example\.(com|org|net))[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
}


def _scan(patterns: dict[str, re.Pattern[str]], text: str) -> list[str]:
    hits = []
    for label, pattern in patterns.items():
        for match in pattern.finditer(text):
            hits.append(f"{label}: {match.group(0)!r}")
    return hits


def main() -> int:
    if not CACHE_DIR.exists():
        print(f"no cache/ directory at {CACHE_DIR} -- nothing to scrub")
        return 0

    files = sorted(CACHE_DIR.glob("*.json"))
    if not files:
        print("cache/ has no *.json files -- nothing to scrub")
        return 0

    violations: list[str] = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        for hit in _scan(KEY_PATTERNS, text):
            violations.append(f"{path.relative_to(ROOT)}: {hit}")
        for hit in _scan(ABS_PATH_PATTERNS, text):
            violations.append(f"{path.relative_to(ROOT)}: {hit}")
        for hit in _scan(IDENTIFIER_PATTERNS, text):
            violations.append(f"{path.relative_to(ROOT)}: {hit}")

    if violations:
        print(f"scrub_cache: FOUND {len(violations)} violation(s) in {len(files)} cache files:")
        for v in violations:
            print(f"  {v}")
        return 1

    print(f"scrub_cache: OK -- {len(files)} cache files clean (no keys, no absolute paths, no real identifiers)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
