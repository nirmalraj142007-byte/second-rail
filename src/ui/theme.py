"""Colours and glyphs for the terminal demo surface — the one place this
project's "signal cabin" identity lives, so a palette change never means
hunting through live.py/approve.py for stray hex codes.

Two colour vocabularies are kept deliberately separate, per the phase spec:
STATUS colours answer "did this check pass" (pass/fail/degraded — green/
red/amber). TIER colours answer "which escalation tier did this episode
land in" (auto/human_keystroke/hard_refuse — dim/cyan/red). Collapsing them
into one scheme would make a degraded-but-auto episode indistinguishable
from a passing-but-human_keystroke one, which is exactly the kind of
ambiguity a judge scanning the log at speed would trip over.

Every state also carries a plain-text label alongside its glyph and colour
— colour alone never signals anything, because video compression and
colourblind viewers both exist (this module's own docstring-enforced rule;
tests/test_ui.py checks it by grepping plain export_text() output).
"""

from __future__ import annotations

from typing import Literal

from rich.style import Style
from rich.text import Text

# ---------------------------------------------------------------------------
# palette — the six identity colours (see the design plan), plus two
# functional-only tier extensions the spec calls out separately.
# ---------------------------------------------------------------------------

CABIN = "#12161C"  # ground / panel fill
CHALK = "#E8E3D8"  # primary text — timetable paper, not pure white
BRASS = "#B8925A"  # dividers, borders, the run banner
SIGNAL_RED = "#C0392B"  # fail / hard_refuse
SIGNAL_AMBER = "#D99A2B"  # degraded / human_keystroke countdown
SIGNAL_GREEN = "#3FA66D"  # pass

# Tier-only extensions (not part of the core identity palette above):
TELEGRAPH_CYAN = "#4FB6C4"  # human_keystroke tier
SLATE_DIM = "#5C6773"  # auto tier / secondary text

# ---------------------------------------------------------------------------
# status: pass / fail / degraded — used for every gate check and for the
# diagnose-degraded inline notice.
# ---------------------------------------------------------------------------

StatusName = Literal["pass", "fail", "degraded"]

# Plain ASCII only — cmd.exe / a legacy Windows console on the cp1252
# codepage raises UnicodeEncodeError on box-drawing and symbol glyphs (the
# same constraint scripts/demo.py's mode banner already works around; see
# that module's comment). This is the screen a judge might record on
# exactly that terminal, so every glyph in this module must survive it.
STATUS_GLYPH: dict[StatusName, str] = {
    "pass": "+",
    "fail": "x",
    "degraded": "!",
}

STATUS_LABEL: dict[StatusName, str] = {
    "pass": "OK",
    "fail": "FAIL",
    "degraded": "DEGRADED",
}

STATUS_COLOR: dict[StatusName, str] = {
    "pass": SIGNAL_GREEN,
    "fail": SIGNAL_RED,
    "degraded": SIGNAL_AMBER,
}


def status_text(status: StatusName, name: str | None = None) -> Text:
    """`name OK` / `name FAIL` / `name DEGRADED`, glyph + label + colour,
    never colour alone. `name` is the check name (e.g. 'quiet_hours'); when
    omitted, only the glyph+label render (e.g. for a standalone degraded
    notice)."""
    glyph = STATUS_GLYPH[status]
    label = STATUS_LABEL[status]
    prefix = f"{name} " if name else ""
    return Text(f"{prefix}{glyph} {label}", style=Style(color=STATUS_COLOR[status]))


# ---------------------------------------------------------------------------
# tier: auto / human_keystroke / hard_refuse — spec-mandated colours.
# ---------------------------------------------------------------------------

TierName = Literal["auto", "human_keystroke", "hard_refuse"]

TIER_LABEL: dict[TierName, str] = {
    "auto": "auto",
    "human_keystroke": "human_keystroke",
    "hard_refuse": "hard_refuse",
}

TIER_STYLE: dict[TierName, Style] = {
    "auto": Style(color=SLATE_DIM, dim=True),
    "human_keystroke": Style(color=TELEGRAPH_CYAN, bold=True),
    "hard_refuse": Style(color=SIGNAL_RED, bold=True),
}

# The signature element: one coloured dot per episode, in the left gutter,
# coloured by that episode's resolved tier — the signal-aspect lamp a judge
# remembers from a single frame. Never rendered alone; every call site pairs
# it with the tier's text label somewhere on the same line.
SIGNAL_DOT_GLYPH = "*"


def tier_text(tier: TierName) -> Text:
    return Text(TIER_LABEL[tier], style=TIER_STYLE[tier])


def signal_dot(tier: TierName) -> Text:
    return Text(SIGNAL_DOT_GLYPH, style=TIER_STYLE[tier])


# ---------------------------------------------------------------------------
# structural glyphs — the "utility face": rhythm, not colour.
# ---------------------------------------------------------------------------

TICKER_PREFIX = ">"  # indented child-line marker
ARROW = "->"
SLEEPER_RULE_CHAR = "-"  # light divider between episodes
SECTION_RULE_CHAR = "="  # heavy divider between run sections

BRASS_STYLE = Style(color=BRASS, bold=True)
CHALK_STYLE = Style(color=CHALK)
DIM_STYLE = Style(color=SLATE_DIM, dim=True)


def display_heading(text: str) -> Text:
    """Bold brass, tracked ALL-CAPS — the display face, banner-only."""
    tracked = " ".join(text.upper())
    return Text(tracked, style=BRASS_STYLE)


def sleeper_rule(width: int = 72) -> Text:
    return Text(SLEEPER_RULE_CHAR * width, style=DIM_STYLE)


def section_rule(width: int = 72) -> Text:
    return Text(SECTION_RULE_CHAR * width, style=BRASS_STYLE)
