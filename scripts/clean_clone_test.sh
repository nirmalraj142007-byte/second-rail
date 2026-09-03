#!/usr/bin/env bash
# make clean-clone-test -- proves this repo is reproducible on a stranger's
# machine: clones itself into a throwaway temp dir (from the local .git dir,
# so this works with no network), builds a fresh venv, and runs exactly the
# path a judge runs (`make setup && make eval && make verify-audit &&
# make judge-check`) with HOME redirected and every Razorpay/LLM credential
# explicitly unset -- so it cannot pass by accident on this machine's own
# .env or cached pip/venv state. Fails loudly, with the real command output,
# on the first thing that doesn't work; a submission that only passes when
# run from the repo's own working tree is not actually reproducible.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

OVERALL_START=$(date +%s)
WORK="$(mktemp -d)"
CLONE_DIR="$WORK/clone"
FAKE_HOME="$WORK/home"
mkdir -p "$FAKE_HOME"

cleanup() {
  rm -rf "$WORK"
}
trap cleanup EXIT

fail() {
  echo ""
  echo "clean-clone-test: FAILED -- $1"
  exit 1
}

# Same discovery order as scripts/judge_check.py's find_make() -- this dev
# box ships mingw32-make and no plain `make`.
MAKE_BIN=""
for name in make gmake mingw32-make; do
  if command -v "$name" >/dev/null 2>&1; then
    MAKE_BIN="$name"
    break
  fi
done
[ -n "$MAKE_BIN" ] || fail "no make/gmake/mingw32-make on PATH"
echo "using make binary: $MAKE_BIN"

PYCMP_BIN=""
for name in python3.11 python3 python; do
  if command -v "$name" >/dev/null 2>&1; then
    PYCMP_BIN="$name"
    break
  fi
done
[ -n "$PYCMP_BIN" ] || fail "no python interpreter on PATH to run the metrics comparison"

echo "== clean-clone-test: cloning $REPO_ROOT into $CLONE_DIR (local .git, offline) =="
git clone --no-hardlinks "$REPO_ROOT" "$CLONE_DIR" || fail "git clone failed"

cd "$CLONE_DIR"

echo "== clean-clone-test: HOME redirected to $FAKE_HOME; RAZORPAY_*/LLM_API_KEY unset =="
export HOME="$FAKE_HOME"
unset RAZORPAY_KEY_ID
unset RAZORPAY_KEY_SECRET
unset RAZORPAY_WEBHOOK_SECRET
unset LLM_API_KEY

# The Makefile's setup target invokes `python3.11` by that literal name.
# On this dev box that name resolves in an interactive shell (a Windows App
# Execution Alias stub) but old mingw32-make's CreateProcess call can't
# launch it directly -- resolving to the real interpreter path and passing
# it as a make command-line override (highest-precedence in GNU Make,
# overriding the Makefile's own `PYTHON311 := python3.11`) works around that
# without touching the Makefile default every other environment relies on.
PYTHON311_RESOLVED=""
if command -v python3.11 >/dev/null 2>&1; then
  PYTHON311_RESOLVED="$(python3.11 -c 'import sys; print(sys.executable)' 2>/dev/null || true)"
fi

echo "== clean-clone-test: make setup =="
if [ -n "$PYTHON311_RESOLVED" ]; then
  "$MAKE_BIN" setup "PYTHON311=$PYTHON311_RESOLVED" || fail "make setup"
else
  "$MAKE_BIN" setup || fail "make setup"
fi

echo "== clean-clone-test: make eval (timed; must finish under 5 minutes) =="
EVAL_START=$(date +%s)
"$MAKE_BIN" eval
EVAL_STATUS=$?
EVAL_END=$(date +%s)
EVAL_ELAPSED=$((EVAL_END - EVAL_START))
echo "make eval took ${EVAL_ELAPSED}s"
[ "$EVAL_STATUS" -eq 0 ] || fail "make eval exited $EVAL_STATUS"
[ "$EVAL_ELAPSED" -le 300 ] || fail "make eval took ${EVAL_ELAPSED}s, exceeds the 5-minute (300s) budget"

echo "== clean-clone-test: make verify-audit =="
"$MAKE_BIN" verify-audit || fail "make verify-audit"

echo "== clean-clone-test: make judge-check =="
"$MAKE_BIN" judge-check || fail "make judge-check"

echo "== clean-clone-test: evidence/report.md was regenerated =="
[ -s "$CLONE_DIR/evidence/report.md" ] || fail "evidence/report.md missing or empty after make eval"

echo "== clean-clone-test: diffing deterministic eval metrics against the committed run =="
"$PYCMP_BIN" - "$REPO_ROOT/evidence/eval_metrics.json" "$CLONE_DIR/evidence/eval_metrics.json" <<'PYEOF'
import json
import sys

committed_path, fresh_path = sys.argv[1], sys.argv[2]

# Excluded because they are expected to differ on every run by design:
# run_id/baseline_run_id are fresh ULIDs per invocation, elapsed_s and
# throughput_epm are wall-clock timings. Every other key here is derived
# from seeded synthetic data + the committed LLM cache + config, so a
# clean-clone run must reproduce it exactly.
NON_DETERMINISTIC = {"run_id", "baseline_run_id", "elapsed_s", "throughput_epm"}

with open(committed_path, encoding="utf-8") as f:
    committed = json.load(f)
with open(fresh_path, encoding="utf-8") as f:
    fresh = json.load(f)


def strip(d):
    return {k: v for k, v in d.items() if k not in NON_DETERMINISTIC}


c, fr = strip(committed), strip(fresh)
if c != fr:
    print("FAIL: deterministic eval metrics differ between the committed run and this clean-clone run")
    for k in sorted(set(c) | set(fr)):
        if c.get(k) != fr.get(k):
            print(f"  {k}:")
            print(f"    committed: {c.get(k)!r}")
            print(f"    fresh:     {fr.get(k)!r}")
    sys.exit(1)

print(f"OK: {len(c)} deterministic metric(s) match exactly between committed and fresh clean-clone runs")
PYEOF
[ $? -eq 0 ] || fail "regenerated evidence/eval_metrics.json disagrees with the committed one"

OVERALL_END=$(date +%s)
OVERALL_ELAPSED=$((OVERALL_END - OVERALL_START))
echo ""
echo "clean-clone-test: PASSED in ${OVERALL_ELAPSED}s total (make eval alone: ${EVAL_ELAPSED}s)"
exit 0
