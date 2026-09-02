"""LiveRunView — the `make demo` scrolling signal-log.

Deliberately NOT a full-screen Live redraw (rich.live.Live wrapping the
entire log): a full-screen app records badly on video and can't be scrolled
back during questioning. Instead, every finalized fact prints as a normal,
permanent `console.print()` line — Rich supports interleaving plain prints
with an active `Live` region (used only for the two genuinely animated
moments: the approval countdown and nothing else) — so the terminal's own
scrollback is the log, not a Rich-managed viewport.

Per-episode output is deliberately buffered, not printed call-by-call:
`episode_start()` only resets state; the header line (with the episode's
signature dot) prints on the *first* line that actually has content to
carry, via `_ensure_header()` / `_flush_checks()`. This exists because
`guardrail()` receives a bare `CheckResult` with no tier information — the
signal dot cannot be coloured by a tier it doesn't know yet — so the dot is
a constant brass "something happened here" marker, and the real tier colour
lives on the resolution line (`decision()`'s tier text, or the red
suppression line `guardrail()` prints on the first failing check). This is
a disclosed, deliberate narrowing of the "dot coloured by resolved tier"
design-plan language: retroactively recolouring an already-scrolled
terminal line isn't possible, so the honest version puts the colour where
it can actually land.

Duck-typed on purpose: `episode`, `d` (Diagnosis), `match` (PolicyMatch),
`sel` (Selection), `result` (ExecutionResult), `s` (RunSummary) are read via
getattr with defaults, never isinstance-checked. That keeps this module
free of an import dependency on src/diagnose, src/choose, src/execute —
this is a presentation layer, not a participant in the pipeline it renders.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.style import Style
from rich.table import Table
from rich.text import Text

from src.ui.theme import (
    ARROW,
    BRASS,
    BRASS_STYLE,
    CHALK_STYLE,
    DIM_STYLE,
    SIGNAL_AMBER,
    SIGNAL_DOT_GLYPH,
    SIGNAL_GREEN,
    SIGNAL_RED,
    TELEGRAPH_CYAN,
    TICKER_PREFIX,
    StatusName,
    TierName,
    display_heading,
    section_rule,
    status_text,
    tier_text,
)

IST = ZoneInfo("Asia/Kolkata")

_EXECUTION_STATUS_COLOR = {
    "created": SIGNAL_GREEN,
    "duplicate_suppressed": SIGNAL_AMBER,
    "failed": SIGNAL_RED,
    "cancelled": SIGNAL_RED,
}


def _format_ts(ep: object) -> str:
    received_at = getattr(ep, "received_at", None)
    if isinstance(received_at, datetime):
        dt = received_at if received_at.tzinfo else received_at.replace(tzinfo=IST)
        return dt.astimezone(IST).strftime("%H:%M:%S")
    return "--:--:--"


def _format_money(amount_paise: int) -> str:
    return f"{amount_paise / 100:,.0f}"


def _truncate(value: str | None, width: int) -> str:
    if not value:
        return "(none)"
    return value if len(value) <= width else value[: width - 1] + "."


@dataclass(frozen=True)
class ApprovalResult:
    """What `approval_prompt()` / `request_approval()` resolve to.

    `decision` is one of "approve" / "reject" / "skip" / "approval_timeout"
    / "queued" (the last two are never a human keystroke: "approval_timeout"
    is the 60s-elapsed auto-reject, "queued" is the non-interactive path —
    no tty attached, so the episode is left for `make approve`'s JSON queue
    instead of blocking the run). Callers should treat only "approve" as
    authorization to execute.
    """

    decision: str
    actor: str  # "human" | "system"
    reason: str | None
    elapsed_s: float


def render_approval_panel(
    episode: object,
    cause: str,
    chosen_action: str,
    admissible_set: list[str],
    gate_reason: str,
    remaining_s: float,
    timeout_s: float,
) -> Panel:
    """Pure rendering, no waiting — also used directly by `make demo-states`
    to capture the 'blocking' screenshot without a real 60s wait."""
    amount_rupees = getattr(episode, "amount_paise", 0) / 100
    episode_id = getattr(episode, "episode_id", "?")

    body = Table.grid(padding=(0, 1))
    # overflow="fold": wrap rather than truncate-with-ellipsis — see
    # src/ui/theme.py's ASCII-only glyph policy comment for why "…" must
    # never appear on this console.
    body.add_column(overflow="fold")
    body.add_row(Text(f"{episode_id} - Rs {amount_rupees:,.2f} - {cause}", style=CHALK_STYLE))
    body.add_row(Text(f"gate: {gate_reason}", style=DIM_STYLE))
    body.add_row(Text.assemble(("chosen: ", DIM_STYLE), (chosen_action, BRASS_STYLE)))
    body.add_row(Text(f"admissible: {' / '.join(admissible_set)}", style=CHALK_STYLE))
    body.add_row(Text(""))
    body.add_row(
        Text.assemble(
            ("[a]", Style(color=TELEGRAPH_CYAN, bold=True)),
            ("pprove  ", CHALK_STYLE),
            ("[r]", Style(color=SIGNAL_RED, bold=True)),
            ("eject  ", CHALK_STYLE),
            ("[s]", DIM_STYLE),
            ("kip", CHALK_STYLE),
        )
    )

    frac = max(0.0, min(1.0, remaining_s / timeout_s)) if timeout_s else 0.0
    width = 24
    filled = int(width * frac)
    mm, ss = divmod(max(0, int(remaining_s)), 60)
    bar = Text.assemble(
        ("#" * filled, Style(color=SIGNAL_AMBER)),
        ("." * (width - filled), DIM_STYLE),
        (f"  {mm:02d}:{ss:02d} remaining", DIM_STYLE),
    )
    body.add_row(bar)

    border = BRASS if frac > 0.5 else SIGNAL_AMBER
    return Panel(
        body,
        title="APPROVAL REQUIRED",
        title_align="left",
        border_style=Style(color=border, bold=True),
        box=box.ASCII,
    )


def _default_key_reader() -> Callable[[], str | None]:
    """A single non-blocking keypress, lower-cased, or None if nothing is
    waiting. Falls back to an always-None reader on any platform where raw
    keyboard access isn't available (no tty, piped stdin, CI) — this must
    never raise and never block, since a broken reader here would turn
    "never hang indefinitely" into exactly the opposite."""
    try:
        import msvcrt  # noqa: F401  (Windows only)

        def _read_windows() -> str | None:
            if msvcrt.kbhit():
                try:
                    return msvcrt.getwch().lower()
                except Exception:
                    return None
            return None

        return _read_windows
    except ImportError:
        pass

    try:
        import select
        import termios
        import tty

        def _read_posix() -> str | None:
            if not sys.stdin.isatty():
                return None
            fd = sys.stdin.fileno()
            old_settings = termios.tcgetattr(fd)
            try:
                tty.setcbreak(fd)
                ready, _, _ = select.select([sys.stdin], [], [], 0)
                if ready:
                    return sys.stdin.read(1).lower()
                return None
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

        return _read_posix
    except Exception:
        return lambda: None


class LiveRunView:
    def __init__(self, console: Console, total: int) -> None:
        self._console = console
        self._total = total
        self._count = 0
        self._current: dict[str, object] = {}

    # -- run-level ----------------------------------------------------

    def banner(self, run_id: str, mode: str, config_hash: str, seal_status: str = "OK") -> None:
        """The 'loading' state: run banner with run_id/mode/config_hash and a
        seal verification line, printed once before the first episode."""
        heading = display_heading("second rail")
        meta = Text(
            f"run {run_id}   mode:{mode}   cfg:{config_hash[:8]}   seal:{seal_status}",
            style=CHALK_STYLE,
        )
        panel = Panel(Group(heading, meta), border_style=BRASS_STYLE, box=box.ASCII)
        self._console.print(panel)

    # -- per-episode ----------------------------------------------------

    def _header_text(self, ep: object) -> Text:
        ts = _format_ts(ep)
        amount = _format_money(getattr(ep, "amount_paise", 0))
        instrument = getattr(ep, "instrument", None) or "(unknown)"
        t = Text(f"{SIGNAL_DOT_GLYPH} ", style=BRASS_STYLE)
        t.append(
            f"{ts}  {getattr(ep, 'episode_id', '?')}  "
            f"{_truncate(getattr(ep, 'payment_id', None), 14)}  "
            f"Rs {amount}  {instrument}",
            style=CHALK_STYLE,
        )
        return t

    def _ensure_header(self) -> None:
        if not self._current.get("header_printed"):
            self._console.print(self._header_text(self._current["ep"]))
            self._current["header_printed"] = True

    def _flush_checks(self) -> None:
        self._ensure_header()
        if self._current.get("checks_flushed"):
            return
        checks: list[Text] = self._current.get("checks", [])  # type: ignore[assignment]
        if checks:
            line = Text(f"  {TICKER_PREFIX} ", style=CHALK_STYLE)
            for i, c in enumerate(checks):
                if i:
                    line.append("  ")
                line.append_text(c)
            self._console.print(line)
        self._current["checks_flushed"] = True

    def episode_start(self, ep: object) -> None:
        self._count += 1
        self._current = {
            "ep": ep,
            "checks": [],
            "header_printed": False,
            "checks_flushed": False,
            "tier": "auto",
        }

    def guardrail(self, check: object) -> None:
        result = getattr(check, "result", "fail")
        status: StatusName = "pass" if result == "pass" else "fail"
        name = getattr(check, "name", "check")
        checks: list[Text] = self._current.setdefault("checks", [])  # type: ignore[assignment]
        checks.append(status_text(status, name=name))
        if status == "fail":
            self._flush_checks()
            reason = getattr(check, "reason", None) or "refused"
            line = Text(
                f"  {TICKER_PREFIX} {ARROW} suppressed ({reason})",
                style=Style(color=SIGNAL_RED, bold=True),
            )
            self._console.print(line)

    def diagnosis(self, d: object) -> None:
        self._flush_checks()
        class_id = getattr(d, "class_id", "unknown")
        confidence = getattr(d, "confidence", 0.0)
        method = getattr(d, "method", "?")
        line = Text(
            f"  {TICKER_PREFIX} {class_id} ({confidence:.2f}) via {method}", style=CHALK_STYLE
        )
        self._console.print(line)
        if getattr(d, "llm_degraded", False):
            note = Text(
                f"  {TICKER_PREFIX} llm_degraded {ARROW} regex baseline used",
                style=Style(color=SIGNAL_AMBER, bold=True),
            )
            self._console.print(note)

    def candidates(self, match: object) -> None:
        self._flush_checks()
        actions = getattr(match, "admissible_actions", [])
        line = Text(f"  {TICKER_PREFIX} candidates: {' / '.join(actions)}", style=DIM_STYLE)
        self._console.print(line)

    def decision(self, sel: object, tier: TierName) -> None:
        self._flush_checks()
        self._current["tier"] = tier
        action = getattr(sel, "chosen_action", str(sel))
        line = Text(f"  {TICKER_PREFIX} {ARROW} ", style=CHALK_STYLE)
        line.append(action, style=Style(color=BRASS, bold=True))
        line.append("  ")
        line.append_text(tier_text(tier))
        self._console.print(line)

    def execution(self, result: object) -> None:
        # Deliberately does NOT infer a retry cap from the last observed
        # retry() call here — that was tried and is wrong by one whenever
        # the cap was genuinely exhausted: with_backoff() never announces a
        # backoff before its own final, failing attempt, so the last seen
        # `attempt` is cap-1, not cap. Confirmed empirically against the
        # real fault-injection rig (src/execute/faults.py) at cap=3: only
        # attempts 1 and 2 are ever announced. The real cap is only known
        # to the executor, which calls retry_exhausted() itself via
        # RazorpayExecutor's on_retry_exhausted hook — see that module.
        self._flush_checks()
        status = getattr(result, "status", "unknown")
        plink = getattr(result, "plink_id", None) or "(none)"
        color = _EXECUTION_STATUS_COLOR.get(status, SIGNAL_AMBER)
        line = Text(f"  {TICKER_PREFIX} {plink}  ", style=CHALK_STYLE)
        line.append(status, style=Style(color=color, bold=True))
        self._console.print(line)

    def retry(self, attempt: int, delay_s: float, status: int) -> None:
        line = Text(
            f"  {TICKER_PREFIX} attempt {attempt} - backoff {delay_s:g}s - HTTP {status}",
            style=Style(color=SIGNAL_AMBER),
        )
        self._console.print(line)

    def retry_exhausted(self, cap: int) -> None:
        line = Text(
            f"  {TICKER_PREFIX} retry cap {cap} - not retrying",
            style=Style(color=SIGNAL_RED, bold=True),
        )
        self._console.print(line)

    def cluster_refusal(self, cause: str, count: int) -> None:
        """Exactly one line for the whole cluster — the caller (Runner) is
        responsible for calling this once per cluster, at the point it
        detects the cluster is fully processed, not once per member
        episode. See src/runner.py."""
        line = Text(
            f"cluster: {count} episodes share {cause} {ARROW} hard_refuse "
            f"(shared_cause_cluster) {ARROW} escalated to human",
            style=Style(color=SIGNAL_RED, bold=True),
        )
        self._console.print(line)

    # -- approval (human_keystroke tier) ---------------------------------

    def approval_prompt(
        self,
        episode: object,
        cause: str,
        chosen_action: str,
        admissible_set: list[str],
        gate_reason: str,
        *,
        timeout_s: float = 60.0,
        key_reader: Callable[[], str | None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] | None = None,
        poll_interval_s: float = 0.05,
    ) -> ApprovalResult:
        """Blocks for at most `timeout_s` waiting for a single keypress,
        rendering a countdown at 4/sec. Auto-rejects with
        reason="approval_timeout" on timeout — this must never hang, since a
        stuck prompt on stage is unrecoverable (see module docstring of the
        phase spec this implements).

        `key_reader`/`sleep`/`now` are injectable so tests can drive this
        deterministically (freezegun-friendly `now`, a `sleep` that
        fast-forwards a frozen clock instead of a real wait) without a real
        60-second test."""
        now_fn = now or (lambda: datetime.now(IST))
        reader = key_reader or _default_key_reader()
        decision_map = {"a": "approve", "r": "reject", "s": "skip"}

        start = now_fn()
        deadline = start + timedelta(seconds=timeout_s)

        with Live(
            render_approval_panel(
                episode, cause, chosen_action, admissible_set, gate_reason, timeout_s, timeout_s
            ),
            console=self._console,
            refresh_per_second=4,
            transient=True,
        ) as live:
            while True:
                current = now_fn()
                remaining = (deadline - current).total_seconds()
                if remaining <= 0:
                    break
                key = reader()
                if key in decision_map:
                    elapsed = (current - start).total_seconds()
                    result = ApprovalResult(
                        decision=decision_map[key], actor="human", reason=None, elapsed_s=elapsed
                    )
                    self._print_approval_resolution(result, timeout_s)
                    return result
                live.update(
                    render_approval_panel(
                        episode, cause, chosen_action, admissible_set, gate_reason,
                        remaining, timeout_s,
                    )
                )
                sleep(poll_interval_s)

        result = ApprovalResult(
            decision="approval_timeout", actor="system", reason="approval_timeout",
            elapsed_s=timeout_s,
        )
        self._print_approval_resolution(result, timeout_s)
        return result

    def request_approval(
        self,
        episode: object,
        cause: str,
        chosen_action: str,
        admissible_set: list[str],
        gate_reason: str,
    ) -> ApprovalResult:
        """Interactive prompt when a real tty is attached; otherwise never
        blocks the run — the episode is left "queued" for the caller to
        persist into demo/approval_queue.json (src/ui/approve.py), the
        non-interactive fallback path CLAUDE.md's priority discipline
        already named. Deciding whether to persist the queue entry is the
        caller's job (Runner), not this presentation-only module's."""
        if sys.stdin.isatty():
            return self.approval_prompt(episode, cause, chosen_action, admissible_set, gate_reason)
        line = Text(
            f"  {TICKER_PREFIX} human_keystroke - no interactive tty, queued for `make approve`",
            style=DIM_STYLE,
        )
        self._console.print(line)
        return ApprovalResult(decision="queued", actor="system", reason="no_interactive_tty",
                               elapsed_s=0.0)

    def _print_approval_resolution(self, result: ApprovalResult, timeout_s: float) -> None:
        if result.decision == "approve":
            text = f"  {TICKER_PREFIX} approved by {result.actor} ({result.elapsed_s:.0f}s)"
            style = Style(color=SIGNAL_GREEN, bold=True)
        elif result.decision == "reject":
            text = f"  {TICKER_PREFIX} rejected by {result.actor} ({result.elapsed_s:.0f}s)"
            style = Style(color=SIGNAL_RED, bold=True)
        elif result.decision == "skip":
            text = f"  {TICKER_PREFIX} skipped by {result.actor} ({result.elapsed_s:.0f}s)"
            style = DIM_STYLE
        else:
            text = (
                f"  {TICKER_PREFIX} approval_timeout ({timeout_s:.0f}s) - auto-rejected, "
                f"reason=approval_timeout"
            )
            style = Style(color=SIGNAL_RED, bold=True)
        self._console.print(Text(text, style=style))

    # -- run summary ------------------------------------------------------

    def summary(self, s: object) -> None:
        by_outcome: dict[str, int] = getattr(s, "by_outcome", {}) or {}
        actioned = by_outcome.get("actioned", 0)
        suppressed = by_outcome.get("suppressed", 0)
        pending = by_outcome.get("pending", 0)
        execution_failed = by_outcome.get("execution_failed", 0)
        episode_count = getattr(s, "episode_count", 0)

        self._console.print(section_rule())

        eligible = actioned + pending + execution_failed
        if eligible == 0:
            msg = Text(
                f"0 eligible episodes; {suppressed} suppressed. See exception list.",
                style=Style(color=SIGNAL_AMBER, bold=True),
            )
            self._console.print(msg)
            return

        grid = Table.grid(padding=(0, 2))
        grid.add_column(overflow="fold")
        grid.add_column(overflow="fold")
        grid.add_row(
            Text("actioned", style=DIM_STYLE),
            Text(str(actioned), style=Style(color=SIGNAL_GREEN, bold=True)),
        )
        grid.add_row(
            Text("suppressed", style=DIM_STYLE),
            Text(str(suppressed), style=Style(color=SIGNAL_RED, bold=True)),
        )
        grid.add_row(
            Text("pending", style=DIM_STYLE),
            Text(str(pending), style=Style(color=SIGNAL_AMBER, bold=True)),
        )
        grid.add_row(
            Text("execution_failed", style=DIM_STYLE),
            Text(str(execution_failed), style=Style(color=SIGNAL_RED, bold=True)),
        )
        admissibility_rate = getattr(s, "admissibility_rate", None)
        if admissibility_rate is not None:
            grid.add_row(
                Text("admissibility_rate", style=DIM_STYLE),
                Text(f"{admissibility_rate:.1%}", style=CHALK_STYLE),
            )
        net_paise = getattr(s, "net_paise", None)
        if net_paise is not None:
            grid.add_row(
                Text("net_recovered", style=DIM_STYLE),
                Text(f"Rs {net_paise / 100:,.2f}", style=Style(color=BRASS, bold=True)),
            )
        panel = Panel(
            grid,
            title=f"RUN SUMMARY - {episode_count} episodes",
            title_align="left",
            border_style=BRASS_STYLE,
            box=box.ASCII,
        )
        self._console.print(panel)
