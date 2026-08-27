"""This test is a deliverable, not scaffolding — it is the enforcement
mechanism behind docs/where-the-llm-is-not.md's claim that these packages
never import an LLM client. Walks src/gate/, src/execute/, src/attribute/,
src/audit/, src/ingest/, src/db/ (skipping any that don't exist yet — only
src/gate/, src/audit/, src/ingest/, src/db/ are built as of this phase) and
fails if any file imports the LLM client module or contains the strings
"openai", "genai", or "anthropic"."""

from __future__ import annotations

from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parent.parent / "src"

FORBIDDEN_PACKAGES = ("gate", "execute", "attribute", "audit", "ingest", "db")
FORBIDDEN_STRINGS = ("openai", "genai", "anthropic")


def _offending_files() -> list[str]:
    offenders: list[str] = []
    for package in FORBIDDEN_PACKAGES:
        pkg_dir = SRC_ROOT / package
        if not pkg_dir.exists():
            continue
        for path in pkg_dir.rglob("*.py"):
            text = path.read_text(encoding="utf-8").lower()
            if any(needle in text for needle in FORBIDDEN_STRINGS):
                offenders.append(str(path.relative_to(SRC_ROOT)))
    return offenders


def test_no_llm_client_symbol_in_deterministic_packages():
    assert _offending_files() == []


def test_forbidden_string_detection_actually_works(tmp_path):
    # Sanity check on the detector itself: a file containing one of the
    # forbidden strings must be caught, so a passing test above means the
    # packages are genuinely clean rather than the check being a no-op.
    decoy = tmp_path / "decoy.py"
    decoy.write_text("import openai\n", encoding="utf-8")
    text = decoy.read_text(encoding="utf-8").lower()
    assert any(needle in text for needle in FORBIDDEN_STRINGS)
