#!/usr/bin/env bash
# Resolves a real, absolute Python interpreter path for `make setup`.
# Two problems, not one -- fixing only the first still leaves `make setup`
# broken on this exact machine:
#
# 1. mingw32-make (the only make-family binary this project's own dev
#    machine has -- see README's Quickstart) launches recipe commands via a
#    raw CreateProcess() rather than going through a shell. On Windows,
#    `python3.11` and `python3` are commonly Windows App Execution Alias
#    stubs under WindowsApps\ -- a real shell resolves and launches them
#    fine, but CreateProcess() cannot: "process_begin: CreateProcess(NULL,
#    python3.11 -m venv .venv, ...) failed." Resolving each candidate down
#    to `sys.executable` (the real interpreter underneath the alias)
#    sidesteps this entirely -- the Makefile ends up with an absolute path,
#    never a bare command name.
# 2. Not every Python on PATH is 3.11, the version this project is pinned
#    and tested against (CLAUDE.md's stack list). On this exact machine,
#    `python` and `py` resolve to 3.13; only `python3.11`/`python3` are
#    3.11. Picking the first name found without checking its version would
#    silently run setup against an interpreter nothing here was tested on.
#
# Preference order: python3.11, python3, python, py -- each candidate is
# accepted only if it reports 3.11.x. If none does, falls back to the first
# working interpreter found at all (any Python beats none) and prints a
# warning to stderr, which `make setup` surfaces.
set -u

CANDIDATES=(python3.11 python3 python py)
first_found=""

for name in "${CANDIDATES[@]}"; do
  cmd=$(command -v "$name" 2>/dev/null) || continue
  real=$("$cmd" -c "import sys; print(sys.executable)" 2>/dev/null) || continue
  [ -n "$real" ] || continue
  [ -n "$first_found" ] || first_found="$real"
  if "$real" -c "import sys; exit(0 if sys.version_info[:2] == (3, 11) else 1)" 2>/dev/null; then
    echo "$real"
    exit 0
  fi
done

if [ -n "$first_found" ]; then
  echo "find_python.sh: no Python 3.11 found on PATH (tried: ${CANDIDATES[*]})," \
       "falling back to $first_found -- this project is pinned to 3.11," \
       "results may differ" >&2
  echo "$first_found"
  exit 0
fi

echo "find_python.sh: no Python interpreter found on PATH at all" \
     "(tried: ${CANDIDATES[*]})" >&2
exit 1
