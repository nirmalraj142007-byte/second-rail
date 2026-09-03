#!/usr/bin/env bash
# make secrets-audit -- proves no secret ever entered git history or the
# working tree. Exits 1 on the first finding category, printing commit and
# file, and does NOT attempt to fix anything: a finding here means STOP and
# tell the operator before rewriting history, since that's a rotate-keys-and
# -force-push or fresh-repo decision, not a script's to make.
set -uo pipefail

cd "$(dirname "$0")/.."

FAIL=0

# Real Razorpay key ids are `rzp_(test|live)_` + 14 random alphanumeric
# chars -- requiring 14+ here is what lets this pattern skip placeholder
# fixtures like rzp_test_dummy / rzp_test_xxx / rzp_test_key (all under 14
# chars) without a separate allowlist. A genuine leaked key still matches;
# a short English-word placeholder does not. (Deliberately no realistic-
# looking example key in this comment -- one used to live here and this
# script's own history check flagged it.)
PATTERN='rzp_(test|live)_[A-Za-z0-9]{14,}|sk-[A-Za-z0-9]{20,}|AIza[A-Za-z0-9_-]{30,}|gsk_[A-Za-z0-9]{20,}'

echo "== secrets-audit: scanning full git history (all branches) for key-shaped strings =="
HISTORY_HITS="$(git log -p --all | grep -nE "$PATTERN" || true)"
if [ -n "$HISTORY_HITS" ]; then
  echo "FAIL: key-shaped string(s) found in git history:"
  echo "$HISTORY_HITS"
  echo ""
  echo "-- commits touching matching content --"
  git log --all -p | grep -B5 -E "$PATTERN" | grep '^commit ' || true
  FAIL=1
else
  echo "OK: no key-shaped strings in git history"
fi

echo ""
echo "== secrets-audit: scanning the working tree =="
WORKTREE_HITS="$(git grep -nE "$PATTERN" -- . ':!cache/**' 2>/dev/null || true)"
if [ -n "$WORKTREE_HITS" ]; then
  echo "FAIL: key-shaped string(s) found in the tracked working tree:"
  echo "$WORKTREE_HITS"
  FAIL=1
else
  echo "OK: no key-shaped strings in the tracked working tree"
fi

echo ""
echo "== secrets-audit: .env must be gitignored and never committed =="
if git check-ignore -q .env; then
  echo "OK: .env is gitignored"
else
  echo "FAIL: .env is NOT gitignored"
  FAIL=1
fi

ENV_HISTORY="$(git log --all -- .env)"
if [ -n "$ENV_HISTORY" ]; then
  echo "FAIL: .env was committed at some point in history:"
  git log --all --oneline -- .env
  FAIL=1
else
  echo "OK: .env was never committed"
fi

echo ""
if [ "$FAIL" -ne 0 ]; then
  echo "secrets-audit: FAILED -- do not rewrite history or force-push yet. Report this to the operator first."
  exit 1
fi

echo "secrets-audit: PASSED -- no secrets found in history or working tree, .env clean"
exit 0
