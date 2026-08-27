"""Executor — creates reversible recovery actions (Payment Links) on real Razorpay.

This module is responsible for executing recovery actions within the bounds set
by the policy engine and guardrails. The only external effect it can produce is a
cancellable Razorpay Payment Link, which the customer must authenticate themselves
to pay. No code path in this module moves money.

The executor follows the idempotency pattern: every call is keyed by a stable
(payment_id, policy_rule_id) pair, stored in a UNIQUE constraint. Re-running the
same episode produces zero new links and a "duplicate_suppressed" status, proving
the system is safe against webhook replays and retries.

Default mode is dry-run: `make demo` makes zero HTTP calls and says so.
`make demo EXECUTE=1` or `--execute` flag in code is required for real Razorpay calls.
"""
