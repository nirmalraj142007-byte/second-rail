# Build Log

Entries are written at the end of each working session and are never
backfilled. If a session isn't logged the day it happened, it doesn't get
added later — that's the point of this file existing at all.

## D1 — 25 Aug 2026

Phase 0 only: repo skeleton, typed settings, the closed error taxonomy,
structured logging, and the pre-registered `outcome_model.md`. No domain
logic — nothing under `src/gate`, `src/diagnose`, `src/choose`,
`src/execute`, `src/attribute`, or `src/audit` exists yet, by design.

**What I set up:** `src/config.py` (pydantic-settings `Settings`, every
credential field `Optional`, `load_settings()` succeeds with zero env vars
and no `.env`), `src/errors.py` (the closed `SecondRailError` taxonomy with
the expected-control-flow-vs-abort split documented in its module
docstring), `src/logging_setup.py` (JSON logs to stderr, IST timestamps, a
`LoggerAdapter` for run_id/episode_id context), and the Makefile with every
target from the spec — real ones for `setup`/`doctor`/`lint`/`test`/`clean`,
and "not yet built" placeholders that exit 0 for everything downstream of
Phase 0.

**What I assumed:** the attribution window (48h) and the false-positive
goodwill proxy (₹15/contact) are both written into `outcome_model.md` as
named assumptions with their justification, per the phase spec's explicit
default. Neither source document states these numbers, and the blueprint
lists both as open questions (U-08, U-10) — I'm treating the phase-0 prompt's
"use 48 hours unless told otherwise" as the resolution for U-08 specifically,
since it's an explicit instruction rather than a silent guess. U-02 (student
eligibility), U-03 (test-key application status), and U-13 (rubric framing)
are still genuinely open and don't get touched by anything in this phase —
they don't block foundation code, but they block treating this blueprint as
committed to before real build hours go in past D1.

**What didn't work as expected:** two things, both process rather than
logic bugs.

First, I wrote `src/config.py`'s `doctor` command as a single
`@app.command()` on a bare `typer.Typer()`, on the assumption that
`python -m src.config doctor` would just work. It didn't — Typer collapses
an app with exactly one registered command into a single-command CLI, so
`doctor` was parsed as an unexpected positional argument, not a subcommand
name (`Got unexpected extra argument (doctor)`, exit 2). I only caught this
because I ran the acceptance test myself instead of trusting the code as
written. Fix: added an explicit no-op `@app.callback()`, which tells Typer
this is a command group even with one member, so `doctor` resolves as a
named subcommand. Worth remembering for Phase 8's `approve` CLI and any
other single-command Typer entry points later in the build.

Second, this dev machine has no real `make` — only `mingw32-make.exe`
(GNU Make 3.82) under `C:\MinGW\bin`, and Windows' `python3.11` is a
WindowsApps app-execution-alias rather than a normal executable.
`mingw32-make`'s `CreateProcess` call can't resolve that alias the way bash
can, so `mingw32-make setup` failed with "the system cannot find the file
specified" even though `python3.11 --version` works fine directly in bash.
I made the Makefile detect `$(OS)` and switch between `.venv/bin` and
`.venv/Scripts` so it's still correct for a Windows contributor, but I
verified `setup`/`doctor`/`lint`/`test` by running each underlying command
directly against `.venv/Scripts/python.exe` rather than through `make` on
this machine — the Makefile's `python3.11 -m venv` line is unverified by an
actual `make setup` run here and should get a real check on a Linux/Mac box
(or inside a judge's environment) before I lean on it during the demo.
I also briefly copied `mingw32-make.exe` to `C:\MinGW\bin\make.exe` to get a
`make` alias for testing, realized that's a system-wide change nobody asked
for, and deleted it again — noting it here so the git history doesn't have
to explain why a random binary showed up and vanished in a system directory
that isn't part of this repo.

Ruff also flagged `tests/test_config.py`'s import block as unsorted on the
first run, because ruff's isort didn't know `src` was first-party and
grouped it with third-party imports. Added `known-first-party = ["src"]`
to `pyproject.toml`; that's now `make lint`-clean.
