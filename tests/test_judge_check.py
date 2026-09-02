"""Tests for scripts/judge_check.py's own logic.

The checks themselves are integration-shaped by design - they read the real
repo - so what is worth unit testing is the machinery underneath them: the
banned-phrase scanner and its two documented exemptions, the report section
parser, the blockquote detector, and the bounded subprocess wrapper. Those
are the parts that could silently pass a check that should have failed.

This file is in QUOTED_RULE_FILES because it writes the banned phrases into
temporary fixtures on purpose. judge_check.py itself is not exempt - it
assembles its needles from fragments instead.
"""

from __future__ import annotations

import sys

import pytest

from scripts.judge_check import (
    QUOTED_RULE_FILES,
    CheckError,
    CheckResult,
    _backtick_spans,
    _illustrative_blocks,
    _occurrence_is_quoted_code,
    find_make,
    read,
    report_sections,
    run_command,
    scan_phrase,
)

# ---------------------------------------------------------------------------
# code-span exemption
# ---------------------------------------------------------------------------


class TestBacktickSpans:
    def test_no_backticks_is_no_spans(self) -> None:
        assert _backtick_spans("plain prose with no code") == []

    def test_one_pair(self) -> None:
        assert _backtick_spans("a `code` b") == [(2, 7)]

    def test_two_pairs(self) -> None:
        assert _backtick_spans("`a` and `b`") == [(0, 2), (8, 10)]

    def test_unpaired_trailing_backtick_opens_nothing(self) -> None:
        # An odd backtick is not a code span, so it must not swallow the rest
        # of the line and exempt a real claim by accident.
        assert _backtick_spans("`a` and a stray ` here") == [(0, 2)]

    def test_occurrence_inside_span_is_quoted(self) -> None:
        line = 'ran `grep -rn "held-out test set" .` and got nothing'
        start = line.index("held-out")
        assert _occurrence_is_quoted_code(line, start, start + len("held-out test set"))

    def test_occurrence_outside_span_is_a_claim(self) -> None:
        line = "evaluated on a held-out test set of 200 episodes"
        start = line.index("held-out")
        assert not _occurrence_is_quoted_code(line, start, start + len("held-out test set"))


# ---------------------------------------------------------------------------
# scan_phrase
# ---------------------------------------------------------------------------


class TestScanPhrase:
    def test_finds_a_bare_claim(self, tmp_path) -> None:
        doc = tmp_path / "claim.md"
        doc.write_text("We report on a held-out test set.\n", encoding="utf-8")
        found = scan_phrase("held-out test set", [doc])
        assert len(found) == 1
        assert found[0].lineno == 1

    def test_is_case_insensitive_by_default(self, tmp_path) -> None:
        doc = tmp_path / "claim.md"
        doc.write_text("A HELD-OUT TEST SET.\n", encoding="utf-8")
        assert len(scan_phrase("held-out test set", [doc])) == 1

    def test_skips_quoted_code(self, tmp_path) -> None:
        doc = tmp_path / "log.md"
        doc.write_text('`grep "held-out test set" .` returns nothing\n', encoding="utf-8")
        assert scan_phrase("held-out test set", [doc]) == []

    def test_quoted_code_exemption_can_be_turned_off(self, tmp_path) -> None:
        doc = tmp_path / "log.md"
        doc.write_text("`33%` of things\n", encoding="utf-8")
        assert scan_phrase("33%", [doc]) == []
        assert len(scan_phrase("33%", [doc], allow_quoted_code=False)) == 1

    def test_counts_every_occurrence_on_one_line(self, tmp_path) -> None:
        doc = tmp_path / "twice.md"
        doc.write_text("nobody owns it and nobody owns that\n", encoding="utf-8")
        assert len(scan_phrase("nobody owns", [doc])) == 2

    def test_quoted_rule_files_are_skipped_by_path(self) -> None:
        # CLAUDE.md states the bans and therefore contains the banned phrases.
        # If this exemption ever stops applying, every banned-phrase row fails
        # for a reason that has nothing to do with the submission's claims.
        from scripts.judge_check import ROOT

        claude_md = ROOT / "CLAUDE.md"
        assert "CLAUDE.md" in QUOTED_RULE_FILES
        assert "30% of revenue" in read(claude_md)
        assert scan_phrase("30% of revenue", [claude_md]) == []

    def test_missing_file_is_a_check_error_not_a_silent_pass(self, tmp_path) -> None:
        with pytest.raises(CheckError):
            read(tmp_path / "does_not_exist.md")


# ---------------------------------------------------------------------------
# report section parsing
# ---------------------------------------------------------------------------


class TestReportSections:
    def test_parses_the_real_report(self) -> None:
        sections = report_sections()
        # The ordering discipline the judge expectations ask for: non-circular
        # first, externally anchored second, the humbling result third, and
        # only then the recovery figure.
        for n in ("1", "2", "3", "4"):
            assert n in sections, f"evidence/report.md is missing section {n}"
        assert sections["1"].startswith("## 1.")
        assert "design target" in sections["4"].splitlines()[0].lower()

    def test_section_body_stops_at_the_next_section(self) -> None:
        sections = report_sections()
        assert "## 4." not in sections["3"]


# ---------------------------------------------------------------------------
# illustrative blockquote detection
# ---------------------------------------------------------------------------


class TestIllustrativeBlocks:
    def test_blockquote_with_two_money_figures_is_a_block(self) -> None:
        text = "intro\n\n> A merchant at Rs 1,00,000 leaves Rs 7,000 failed.\n\nafter\n"
        blocks = _illustrative_blocks(text)
        assert len(blocks) == 1
        assert blocks[0][0] == 3

    def test_blockquote_with_one_money_figure_is_not(self) -> None:
        assert _illustrative_blocks("> costs Rs 500\n") == []

    def test_prose_with_money_is_not_a_block(self) -> None:
        assert _illustrative_blocks("Rs 1,000 and Rs 2,000 in a sentence.\n") == []

    def test_multi_line_blockquote_is_one_block(self) -> None:
        text = "> Rs 100\n> and Rs 200\n\n> Rs 300\n> and Rs 400\n"
        assert len(_illustrative_blocks(text)) == 2


# ---------------------------------------------------------------------------
# bounded subprocess
# ---------------------------------------------------------------------------


class TestRunCommand:
    def test_success(self) -> None:
        result = run_command([sys.executable, "-c", "print('hi')"], timeout_s=30.0)
        assert result.ok
        assert "hi" in result.stdout
        assert not result.timed_out

    def test_nonzero_exit_is_not_ok(self) -> None:
        result = run_command([sys.executable, "-c", "raise SystemExit(3)"], timeout_s=30.0)
        assert result.returncode == 3
        assert not result.ok

    def test_timeout_is_reported_not_raised(self) -> None:
        # A hung target must surface as a failing check, never as a
        # judge-check that never returns.
        result = run_command(
            [sys.executable, "-c", "import time; time.sleep(30)"], timeout_s=1.0
        )
        assert result.timed_out
        assert not result.ok

    def test_missing_executable_is_reported_not_raised(self) -> None:
        result = run_command(["definitely-not-a-real-binary-xyz"], timeout_s=5.0)
        assert result.returncode == 127
        assert not result.ok


def test_find_make_resolves_something_on_this_machine() -> None:
    # Not all boxes have `make` under that name - this one only has
    # mingw32-make - which is why find_make() tries three.
    assert find_make() is not None, "no make/gmake/mingw32-make on PATH"


class TestCheckResult:
    def test_pass_line(self) -> None:
        line = CheckResult("JG-01", "closed evidence loop", True, "20 records").line()
        assert line == "PASS JG-01 closed evidence loop - 20 records"

    def test_fail_line(self) -> None:
        assert CheckResult("JG-06", "guardrails", False, "n=108").line().startswith("FAIL JG-06")

    def test_output_is_ascii_encodable(self) -> None:
        # KNOWN_ISSUES.md Issue 2: this project's console raises
        # UnicodeEncodeError on non-ASCII, and a verifier that crashes while
        # printing its own results is worse than no verifier.
        from scripts.judge_check import TITLES

        for check_id, title in TITLES.items():
            CheckResult(check_id, title, True, "detail").line().encode("ascii")
