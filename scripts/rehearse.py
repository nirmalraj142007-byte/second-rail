"""`make rehearse` — drives the 5:00 demo script (the blueprint's 5:00
expansion, second-rail-build-blueprint.md section 8 — U-01 resolved to
five minutes, not the 3:00 spine) beat by beat, times each one against its
target duration, and flags overruns.

This is a rehearsal harness, not a new pipeline feature: every beat either
shells out to a real `make` target (`make demo`, `make verify-seal`, ...)
or reads a real committed file — nothing here is simulated output. Two
run modes:

  - **Interactive** (a real tty attached): narration beats (hook, seam
    diagram, boundary files, close) show a live countdown against the
    beat's target and wait for Enter to mark when the presenter actually
    finished talking, so the printed "actual" duration is a real timing
    of a real rehearsal, not a guess.
  - **Non-interactive** (piped/CI/this tool run from an automated session):
    narration beats cannot be timed — there is no human delivering a line
    — so they're marked `n/a (narration — rerun with a tty to time it)`
    rather than faked. Command beats are unaffected either way; they are
    always timed for real.

Two preflight assertions run before beat 4 (the unbroken take), reading
`demo/episode_order.json` against the *live* config and data rather than
trusting the committed file blindly — config or data drift must fail loud
here, before recording, not mid-take:
  1. the episode at `approval_index` is gate-eligible and its amount alone
     forces `escalation_tier == "human_keystroke"` (this reproduces
     `GateEngine`'s own tier logic, src/gate/engine.py — it does not
     duplicate it, it drives it the same way `Runner.run()` does, minus
     any DB writes or LLM/executor calls).
  2. `cluster_episode_ids` still land inside the same 30-minute sliding
     window `compute_cluster_membership` uses, and are still `>
     outage_cluster_threshold` in count, so the run really does halt on
     `REASON_CLUSTER_ESCALATION` right where the script says it will.

Usage:
  python -m scripts.rehearse                 # full rehearsal, real calls
  python -m scripts.rehearse --skip-slow      # skip the two long real-call
                                               # beats (unbroken take,
                                               # failure-demo) for fast
                                               # iteration on the harness
                                               # itself
  python -m scripts.rehearse --no-countdown   # interactive tty, but skip
                                               # the live countdown wait
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from src.config_models import load_all
from src.db.migrate import get_connection, migrate
from src.gate.checks import Episode, GateContext, RunState
from src.gate.engine import GateEngine, cluster_sizes, compute_cluster_membership
from src.runner import load_episodes

ROOT = Path(__file__).resolve().parent.parent
EPISODE_ORDER_PATH = ROOT / "demo" / "episode_order.json"
PINNED_TAKE_PATH = ROOT / "demo" / "_pinned_take.jsonl"

console = Console()

# The 5:00 expansion (second-rail-build-blueprint.md section 8), timed for
# real -- not the 3:00 spine. Targets below are the blueprint's own beats
# PLUS its four named insertions (make harvest, threshold-experiment plot,
# make rollback, one BUILD_LOG.md entry read aloud) plus its "+0:10
# breathing room", folded into unbroken_take's own target rather than a
# separate beat (the blueprint places that room "in the run", not between
# beats). One trim carries over from the 3:00 version: hook (10s) + seam
# diagram (20s) was written at 30s total against a hard <=25s slide budget
# (J§8) -- 5s is cut here, from the slide segment only, same reasoning as
# before -- see demo/script.md. Every other beat, expansions included,
# keeps its blueprint target exactly. Total: 295s (4:55), 5s under the
# 5:00 ceiling.
BEAT_TARGETS_S: dict[str, float] = {
    "hook": 8.0,
    "seam_diagram": 17.0,
    "head_report": 10.0,
    "harvest_expansion": 35.0,
    "unbroken_take": 65.0,
    "cluster_refusal": 10.0,
    "boundary_files": 15.0,
    "threshold_expansion": 30.0,
    "verify_seal_and_report": 20.0,
    "rollback_expansion": 25.0,
    "failure_demo": 22.0,
    "verify_audit": 13.0,
    "build_log_expansion": 20.0,
    "close": 5.0,
}


@dataclass
class BeatResult:
    name: str
    target_s: float
    actual_s: float | None  # None = not timed (narration beat, no tty)
    overrun: bool
    note: str = ""


@dataclass
class RehearsalState:
    results: list[BeatResult] = field(default_factory=list)
    run_id: str | None = None


def _is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _countdown(label: str, target_s: float, *, enabled: bool) -> None:
    if not enabled:
        return
    console.print(f"[dim]-- {label}: target {target_s:.0f}s. Press Enter when you finish.[/dim]")


def _run_narration_beat(name: str, label: str, *, countdown: bool) -> BeatResult:
    target = BEAT_TARGETS_S[name]
    interactive = _is_interactive()
    if not interactive:
        console.print(f"[dim]{label} -- narration beat, no tty, not timed[/dim]")
        note = "n/a (narration - rerun with a tty to time it)"
        return BeatResult(name, target, None, False, note=note)

    _countdown(label, target, enabled=countdown)
    t0 = time.monotonic()
    try:
        input()
    except EOFError:
        pass
    actual = time.monotonic() - t0
    overrun = actual > target * 1.1
    return BeatResult(name, target, actual, overrun)


def _run_command_beat(
    name: str, label: str, cmd: list[str], *, cwd: Path = ROOT, env: dict[str, str] | None = None,
    interactive_stdin: bool = False,
) -> tuple[BeatResult, str]:
    """`interactive_stdin=True` (the unbroken-take beat only) lets the
    child inherit this process's real stdin when a presenter is actually
    rehearsing at a tty, so the real approval keypress works. Every other
    beat — and the unbroken take itself when this harness is run
    non-interactively (CI, an automated pass) — feeds a real closed pipe
    instead: see scripts/judge_check.py's run_command() docstring for why
    that specific stdin shape, not DEVNULL or inherited, is what actually
    makes a child's isatty() report False on this platform. Without this,
    a non-interactive run blocks a real 60s per human_keystroke episode
    with nobody there to press a key."""
    console.print(f"[bold]-- {label}[/bold]  ({' '.join(cmd)})")
    target = BEAT_TARGETS_S[name]
    t0 = time.monotonic()
    if interactive_stdin and _is_interactive():
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=env)
    else:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=env, input="")
    actual = time.monotonic() - t0
    sys.stdout.write(proc.stdout)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        console.print(f"[bold red]{label} FAILED (exit {proc.returncode})[/bold red]")
    overrun = actual > target * 1.1
    return BeatResult(name, target, actual, overrun), proc.stdout + proc.stderr


# ---------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------


def _load_pinned() -> dict:
    return json.loads(EPISODE_ORDER_PATH.read_text(encoding="utf-8"))


def preflight() -> None:
    """Reproduces the real gate loop (no DB writes, no LLM/executor calls)
    over exactly the pinned episode set, and asserts the two properties the
    take depends on. Aborts loudly (non-zero exit, before anything else
    runs) on any mismatch -- see module docstring."""
    console.print("[bold]preflight: checking pinned episode set against live config/data...[/bold]")
    pinned = _load_pinned()
    bundle = load_all()
    g = bundle.guardrails

    all_episodes = load_episodes([ROOT / p for p in pinned["source_files"]])
    by_id = {e.episode_id: e for e in all_episodes}

    missing = [eid for eid in pinned["episode_ids"] if eid not in by_id]
    if missing:
        _abort(f"episode_order.json names episode_id(s) not found in source data: {missing}")

    pinned_episodes: list[Episode] = [by_id[eid] for eid in pinned["episode_ids"]]

    # --- assertion 1: the approval episode really forces human_keystroke ---
    idx = pinned["approval_index"] - 1
    approval_ep = pinned_episodes[idx]
    if approval_ep.episode_id != pinned["approval_episode_id"]:
        _abort(
            f"approval_index {pinned['approval_index']} points at "
            f"{approval_ep.episode_id!r}, not the declared "
            f"{pinned['approval_episode_id']!r} - episode_order.json is internally inconsistent"
        )
    if approval_ep.amount_paise <= g.auto_approve_ceiling_paise:
        _abort(
            f"approval episode {approval_ep.episode_id} amount_paise="
            f"{approval_ep.amount_paise} does not exceed "
            f"guardrails.auto_approve_ceiling_paise={g.auto_approve_ceiling_paise} - "
            "it will NOT gate to human_keystroke. Config or data has drifted "
            "since episode_order.json was pinned; re-pick the approval episode."
        )

    gate = GateEngine()
    state = RunState()
    cluster_membership = compute_cluster_membership(pinned_episodes, g.outage_cluster_threshold)
    cluster_size_by_key = cluster_sizes(cluster_membership)
    opted_out: frozenset[str] = frozenset()

    # check_duplicate / check_terminal_seen (src/gate/checks.py) query
    # ctx.conn directly - a throwaway, freshly migrated DB (never touched
    # by a real run) so both checks see "not found" and pass, exactly as
    # they would on episodes no run has ever processed. Deleted at the end
    # of this function either way.
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "preflight.db"
        migrate(db_path)
        conn = get_connection(db_path)
        try:
            approval_decision = None
            cluster_failed_ids: list[str] = []
            for ep in pinned_episodes:
                ctx = GateContext(
                    now=ep.received_at,
                    conn=conn,
                    state=state,
                    opted_out_customers=opted_out,
                    cluster_key_for_episode=cluster_membership,
                )
                decision = gate.evaluate(ep, ctx, g)
                if ep.episode_id == approval_ep.episode_id:
                    approval_decision = decision
                if decision.eligible:
                    state.exposure_committed_paise += ep.amount_paise
                    state.total_eligible_contacts_this_run += 1
                    if ep.customer_id:
                        state.contacts_by_customer.setdefault(ep.customer_id, []).append(
                            ep.failed_at
                        )
                elif decision.failed_check == "cluster":
                    cluster_failed_ids.append(ep.episode_id)
                    key = cluster_membership[ep.episode_id]
                    state.cluster_processed[key] = state.cluster_processed.get(key, 0) + 1
                    if state.cluster_processed[key] == cluster_size_by_key[key]:
                        state.cluster_escalated = True
        finally:
            conn.close()

    if approval_decision is None or not approval_decision.eligible:
        _abort(
            f"approval episode {approval_ep.episode_id} is not gate-eligible under the "
            f"live config ({approval_decision.failed_check if approval_decision else '?'} "
            "failed) - the human-gate beat has nothing to approve"
        )
    if approval_decision.escalation_tier != "human_keystroke":
        _abort(
            f"approval episode {approval_ep.episode_id} resolved escalation_tier="
            f"{approval_decision.escalation_tier!r}, expected 'human_keystroke'"
        )
    console.print(
        f"  [green]OK[/green] approval episode {approval_ep.episode_id} "
        f"(Rs {approval_ep.amount_paise / 100:,.2f}) -> human_keystroke"
    )

    # --- assertion 2: the cluster episodes still land inside the window ---
    declared_cluster = set(pinned["cluster_episode_ids"])
    cluster_key = pinned["cluster_key"]
    detected_cluster = {eid for eid, key in cluster_membership.items() if key == cluster_key}
    if detected_cluster != declared_cluster:
        only_declared = declared_cluster - detected_cluster
        only_detected = detected_cluster - declared_cluster
        _abort(
            "cluster membership has drifted from episode_order.json's declared set - "
            f"in declared but not detected: {sorted(only_declared)[:5]}... ; "
            f"in detected but not declared: {sorted(only_detected)[:5]}... "
            "(data/generator.py's seed or config/guardrails.yaml's outage_cluster_threshold "
            "has changed since this file was pinned)"
        )
    if len(detected_cluster) <= g.outage_cluster_threshold:
        _abort(
            f"cluster size {len(detected_cluster)} no longer exceeds "
            f"outage_cluster_threshold={g.outage_cluster_threshold} - it will not escalate"
        )
    if set(cluster_failed_ids) != declared_cluster:
        _abort(
            "not every declared cluster episode actually failed its gate check on "
            f"'cluster' this run: {sorted(declared_cluster - set(cluster_failed_ids))[:5]}..."
        )
    if not state.cluster_escalated:
        _abort(
            "cluster never reached escalation (state.cluster_escalated is False) - "
            "check ordering"
        )
    console.print(
        f"  [green]OK[/green] {len(detected_cluster)} episodes cluster on "
        f"{pinned['cluster_key']!r} inside the 30-minute window, escalates "
        f"(threshold={g.outage_cluster_threshold})"
    )

    # --- assertion 3 (R3): the approval episode's diagnose+choose calls
    # are actually cache-hit, not a live LLM dependency for the take ---
    # A cache MISS here doesn't just cost latency: ActionSelector.select()
    # raises ConfigError (not caught, not degraded — see BUILD_LOG.md's
    # D10 R3 entry and KNOWN_ISSUES.md) whenever the configured LLM client
    # can't be reached AND nothing is cached, which would crash the
    # unbroken take outright rather than showing the amber degraded line
    # CLAUDE.md's Risk 3 mitigation promises. Warns rather than aborts —
    # a genuine cache miss here still completes today (this box has real
    # credentials configured), it's just not demo-safe if that ever stops
    # being true.
    _check_cache_warm(bundle, approval_ep)
    console.print("[bold green]preflight passed.[/bold green]\n")


def _check_cache_warm(bundle, approval_ep: Episode) -> None:
    from src.choose.policy import PolicyEngine
    from src.choose.selector import ActionSelector
    from src.config import load_settings
    from src.diagnose.baseline import RegexBaseline
    from src.diagnose.cache import DiskCache
    from src.diagnose.classifier import Diagnoser
    from src.diagnose.llm_client import build_llm_client

    settings = load_settings()
    cache = DiskCache(settings.cache_dir)
    baseline = RegexBaseline(bundle.taxonomy)
    llm = build_llm_client(settings)
    diagnoser = Diagnoser(baseline, llm, cache, bundle.taxonomy, settings)
    policy_engine = PolicyEngine(bundle.policy)
    selector = ActionSelector(llm, cache, settings)

    diagnosis = diagnoser.diagnose(approval_ep)
    match = policy_engine.resolve(approval_ep, diagnosis)
    try:
        selection = selector.select(approval_ep, diagnosis, match)
    except Exception as exc:
        console.print(
            f"  [bold red]WARN[/bold red] choose call for {approval_ep.episode_id} "
            f"raised {type(exc).__name__}: {exc} — the unbroken take will crash here "
            "unless this is fixed before recording"
        )
        return

    if diagnosis.method == "regex":
        console.print(
            f"  [green]OK[/green] {approval_ep.episode_id} diagnose resolved by regex "
            "(no LLM call needed)"
        )
    elif diagnosis.cache_hit:
        console.print(f"  [green]OK[/green] {approval_ep.episode_id} diagnose cache-hit")
    else:
        console.print(
            f"  [yellow]WARN[/yellow] {approval_ep.episode_id} diagnose was NOT a "
            "cache hit — this take depends on a live LLM call"
        )

    if selection.cache_hit:
        console.print(f"  [green]OK[/green] {approval_ep.episode_id} choose cache-hit")
    elif selection.llm_degraded:
        console.print(
            f"  [yellow]WARN[/yellow] {approval_ep.episode_id} choose degraded to the "
            "deterministic fallback (llm_degraded=True) — not a crash, but not the "
            "cached path either"
        )
    else:
        console.print(
            f"  [yellow]WARN[/yellow] {approval_ep.episode_id} choose was NOT a cache "
            "hit — this take depends on a live LLM call"
        )


def _abort(message: str) -> None:
    console.print(f"[bold red]PREFLIGHT ABORT:[/bold red] {message}")
    console.print(
        "[bold red]Not recording. Fix episode_order.json or the drifted config/data "
        "first.[/bold red]"
    )
    raise typer.Exit(code=1)


def _write_pinned_take_jsonl() -> None:
    pinned = _load_pinned()
    all_episodes = load_episodes([ROOT / p for p in pinned["source_files"]])
    by_id = {e.episode_id: e for e in all_episodes}
    lines = [by_id[eid].model_dump_json() for eid in pinned["episode_ids"]]
    PINNED_TAKE_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# beats
# ---------------------------------------------------------------------------


# A dedicated, disposable DB for the unbroken take — reset before every
# run, same pattern scripts/failure_demo.py already uses and for the same
# reason: check_duplicate (src/gate/checks.py) looks episodes up by
# payment_id in whatever DB is passed in, and the pinned episode_ids are
# fixed on purpose so the beat lands on the same episodes every retake. Run
# them against the shared second_rail.db and the SECOND rehearsal (and
# every one after it) finds all 42 already inserted from the first —
# `duplicate_episode_this_run` suppresses the entire take, silently, with
# no real API calls made at all. Reproduced live while building this
# script; see BUILD_LOG.md's rehearsal entry. DB_PATH is an existing
# config knob (src/config.py's Settings.db_path / .env's DB_PATH) — this
# overrides it for the child process only, it does not touch second_rail.db.
DEMO_TAKE_DB_PATH = ROOT / "evidence" / "demo_take.db"


def _reset_demo_take_db() -> None:
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(DEMO_TAKE_DB_PATH) + suffix)
        if p.exists():
            p.unlink()


def _beat_harvest_expansion(state: RehearsalState) -> None:
    """5:00 expansion, +0:35 after 0:30 (blueprint section 8). Real
    `make harvest` — resumable and safe to re-run: it skips every scenario
    already in evidence/harvest_manifest.json (a real API call only for a
    genuinely new scenario) and never re-runs the reference_id duplicate
    probe once recorded, so a rehearsal pass costs seconds, not the
    original multi-hour harvest session."""
    cmd = [sys.executable, "-m", "scripts.harvest_errors"]
    result, _ = _run_command_beat("harvest_expansion", "make harvest", cmd)
    state.results.append(result)


def _beat_threshold_expansion(state: RehearsalState) -> None:
    """5:00 expansion, +0:30 after 1:45 — why Rs 5,000 and not Rs 2,000,
    on a real plot from a real threshold sweep (experiments/thresholds/),
    not a redrawn illustration."""
    console.print(
        "[bold]-- threshold experiment: experiments/thresholds/auto_approve.md "
        "+ charts/auto_approve.png[/bold]"
    )
    t0 = time.monotonic()
    text = (ROOT / "experiments" / "thresholds" / "auto_approve.md").read_text(encoding="utf-8")
    console.print(text)
    actual = time.monotonic() - t0
    target = BEAT_TARGETS_S["threshold_expansion"]
    state.results.append(BeatResult("threshold_expansion", target, actual, actual > target * 1.1))


def _beat_rollback_expansion(state: RehearsalState) -> None:
    """5:00 expansion, +0:25 after 2:20 — `make rollback` cancelling every
    link the unbroken take created, live. Uses the run_id the unbroken-take
    beat captured; if that beat was skipped or created no links (e.g. an
    automated non-interactive pass where the approval queued rather than
    executed), rollback still runs and correctly reports 0 links."""
    if state.run_id is None:
        console.print(
            "[yellow]no run_id captured from the unbroken take -- skipping "
            "make rollback (nothing to demonstrate against)[/yellow]"
        )
        target = BEAT_TARGETS_S["rollback_expansion"]
        state.results.append(BeatResult("rollback_expansion", target, None, False, note="skipped"))
        return
    env = dict(os.environ)
    env["DB_PATH"] = str(DEMO_TAKE_DB_PATH)
    cmd = [sys.executable, "-m", "src.execute.rollback", "--run-id", state.run_id]
    result, _ = _run_command_beat(
        "rollback_expansion", f"make rollback RUN_ID={state.run_id}", cmd, env=env
    )
    state.results.append(result)


def _beat_build_log_expansion(state: RehearsalState, *, countdown: bool) -> None:
    """5:00 expansion, +0:20 at 2:55 — one BUILD_LOG.md entry read aloud,
    the wrong first hypothesis. Prints the "Wrong turns" index so the
    presenter picks a real one on the spot rather than rehearsing a
    memorized paragraph — narration beat, timed the same way hook/seam/
    close are."""
    text = (ROOT / "BUILD_LOG.md").read_text(encoding="utf-8")
    start = text.index("## Wrong turns")
    end = text.index("\n## D1")
    console.print("[bold]-- BUILD_LOG.md wrong-turns index (pick one, read it aloud)[/bold]")
    console.print(text[start:end])
    label = "BUILD_LOG.md entry read aloud"
    state.results.append(_run_narration_beat("build_log_expansion", label, countdown=countdown))


def _beat_unbroken_take(state: RehearsalState, *, execute: bool, poll_timeout_s: int) -> None:
    _write_pinned_take_jsonl()
    _reset_demo_take_db()
    env = dict(os.environ)
    env["DB_PATH"] = str(DEMO_TAKE_DB_PATH)

    cmd = [sys.executable, "-m", "scripts.demo", "--source", str(PINNED_TAKE_PATH)]
    if execute:
        cmd.append("--execute")
    result, _output = _run_command_beat(
        "unbroken_take", "make demo (pinned take)", cmd, env=env, interactive_stdin=True
    )

    # run_id is printed inside the Rich banner, not as a bare "run_id=" line
    # -- read it back from the take DB instead, which is what
    # scripts/watch.py needs regardless.
    from src.db.migrate import get_connection

    conn = get_connection(DEMO_TAKE_DB_PATH)
    row = conn.execute("SELECT run_id FROM run ORDER BY started_at DESC LIMIT 1").fetchone()
    conn.close()
    run_id = row["run_id"] if row else None
    state.run_id = run_id
    result.note = f"run_id={run_id}"
    state.results.append(result)

    if not execute or run_id is None:
        console.print("[dim]dry-run or no run_id - skipping attribution poll[/dim]")
        return

    console.print(
        f"[bold]-- make watch RUN_ID={run_id} POLL=1[/bold] "
        "(attribution - see BUILD_LOG.md R1 entry)"
    )
    t0 = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-m", "scripts.watch", "--run-id", run_id, "--poll",
         "--interval-s", "5", "--timeout-s", str(poll_timeout_s)],
        cwd=ROOT, capture_output=True, text=True, env=env,
    )
    actual = time.monotonic() - t0
    sys.stdout.write(proc.stdout)
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    console.print(
        f"[dim]watch --poll finished in {actual:.1f}s (timeout was {poll_timeout_s}s)[/dim]"
    )


def _beat_cluster_refusal(state: RehearsalState, *, countdown: bool) -> None:
    console.print(
        "[dim]cluster refusal is the tail of the same `make demo` run above - "
        "no separate command; the run halted on REASON_CLUSTER_ESCALATION.[/dim]"
    )
    label = "outage cluster refusal (narrate over prior output)"
    state.results.append(_run_narration_beat("cluster_refusal", label, countdown=countdown))


def _beat_boundary_files(state: RehearsalState) -> None:
    result, _ = _run_command_beat(
        "boundary_files", "cat config/policy_table.yaml config/guardrails.yaml",
        ["cat", "config/policy_table.yaml", "config/guardrails.yaml"],
    )
    state.results.append(result)


def _beat_verify_seal_and_report(state: RehearsalState) -> None:
    cmd = [sys.executable, "-m", "scripts.seal", "verify"]
    r1, _ = _run_command_beat("verify_seal_and_report", "make verify-seal", cmd)
    console.print("[bold]-- report sections 2 and 3[/bold]")
    t0 = time.monotonic()
    text = (ROOT / "evidence" / "report.md").read_text(encoding="utf-8")
    sec2_start = text.index("## 2.")
    sec4_start = text.index("## 4.")
    console.print(text[sec2_start:sec4_start])
    r1.actual_s += time.monotonic() - t0
    state.results.append(r1)


def _beat_failure_demo(state: RehearsalState) -> None:
    cmd = [sys.executable, "-m", "scripts.failure_demo"]
    result, _ = _run_command_beat("failure_demo", "make failure-demo", cmd)
    state.results.append(result)


def _beat_verify_audit(state: RehearsalState) -> None:
    cmd = [sys.executable, "-m", "src.audit.verify", "--all"]
    result, _ = _run_command_beat("verify_audit", "make verify-audit", cmd)
    state.results.append(result)


def _beat_head_report(state: RehearsalState) -> None:
    cmd = ["head", "-30", "evidence/report.md"]
    result, _ = _run_command_beat("head_report", "head -30 evidence/report.md", cmd)
    state.results.append(result)


# ---------------------------------------------------------------------------
# summary
# ---------------------------------------------------------------------------


def _print_summary(state: RehearsalState) -> None:
    table = Table(title="rehearsal - per-beat timing")
    table.add_column("beat")
    table.add_column("target", justify="right")
    table.add_column("actual", justify="right")
    table.add_column("status")

    total_target = 0.0
    total_actual = 0.0
    any_overrun = False
    for r in state.results:
        total_target += r.target_s
        actual_str = f"{r.actual_s:.1f}s" if r.actual_s is not None else "n/a"
        if r.actual_s is not None:
            total_actual += r.actual_s
        if r.overrun:
            status = "[red]OVERRUN[/red]"
        elif r.actual_s is not None:
            status = "[green]ok[/green]"
        else:
            status = "[dim]untimed[/dim]"
        any_overrun = any_overrun or r.overrun
        note = f" ({r.note})" if r.note else ""
        table.add_row(r.name, f"{r.target_s:.1f}s", actual_str, status + note)

    console.print(table)
    console.print(f"\ntotal target: {total_target:.1f}s ({total_target/60:.2f} min)")
    console.print(
        f"total actual (timed beats only): {total_actual:.1f}s ({total_actual/60:.2f} min)"
    )
    if any_overrun:
        console.print("[bold red]one or more beats overran target by >10%[/bold red]")
    else:
        console.print("[bold green]no beat overran target by >10%[/bold green]")


def main(
    skip_slow: bool = typer.Option(
        False, "--skip-slow",
        help="Skip the unbroken-take and failure-demo beats (fast harness iteration).",
    ),
    no_countdown: bool = typer.Option(
        False, "--no-countdown", help="Interactive tty, but skip the live countdown display."
    ),
    execute: bool = typer.Option(
        True, "--execute/--dry-run",
        help="Real Razorpay calls for the unbroken take (default) vs dry-run.",
    ),
    poll_timeout_s: int = typer.Option(
        60, "--poll-timeout-s",
        help="make watch --poll timeout for the unbroken-take beat's attribution step.",
    ),
) -> None:
    state = RehearsalState()
    countdown = not no_countdown

    preflight()

    hook_result = _run_narration_beat("hook", "hook (static frame)", countdown=countdown)
    state.results.append(hook_result)
    seam_result = _run_narration_beat(
        "seam_diagram", "seam diagram (static frame)", countdown=countdown
    )
    state.results.append(seam_result)
    _beat_head_report(state)

    if skip_slow:
        msg = "unbroken_take, failure_demo, harvest_expansion"
        console.print(f"[yellow]--skip-slow: skipping {msg}[/yellow]")
        target = BEAT_TARGETS_S["harvest_expansion"]
        state.results.append(BeatResult("harvest_expansion", target, None, False, note="skipped"))
    else:
        _beat_harvest_expansion(state)

    if skip_slow:
        target = BEAT_TARGETS_S["unbroken_take"]
        state.results.append(BeatResult("unbroken_take", target, None, False, note="skipped"))
    else:
        _beat_unbroken_take(state, execute=execute, poll_timeout_s=poll_timeout_s)

    _beat_cluster_refusal(state, countdown=countdown)
    _beat_boundary_files(state)
    _beat_threshold_expansion(state)
    _beat_verify_seal_and_report(state)
    _beat_rollback_expansion(state)

    if skip_slow:
        target = BEAT_TARGETS_S["failure_demo"]
        state.results.append(BeatResult("failure_demo", target, None, False, note="skipped"))
    else:
        _beat_failure_demo(state)

    _beat_verify_audit(state)
    _beat_build_log_expansion(state, countdown=countdown)
    state.results.append(_run_narration_beat("close", "close", countdown=countdown))

    _print_summary(state)


if __name__ == "__main__":
    typer.run(main)
