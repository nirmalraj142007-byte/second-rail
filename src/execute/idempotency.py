"""Idempotency key generation for Payment Link creation.

The idempotency key is derived from (payment_id, policy_rule_id), never from the
webhook event ID. This is critical: the same payment can arrive under multiple
event IDs if the webhook is replayed, and keying on the event ID would allow
a redelivery to create a second link. Keying on (payment_id, policy_rule_id)
ensures that the same (payment, policy decision) pair always maps to the same
link — the foundation of safe idempotency.
"""

from __future__ import annotations

import hashlib


def idempotency_key(payment_id: str, policy_rule_id: str) -> str:
    """Generate a stable 32-character idempotency key.

    This key is used as:
    - A UNIQUE constraint in the execution table (the dedup boundary)
    - The reference_id in the Payment Link (Razorpay-side dedup)
    - The link identifier in audit records and rollback commands
    """
    combined = f"{payment_id}:{policy_rule_id}"
    digest = hashlib.sha256(combined.encode()).hexdigest()
    return digest[:32]


def reference_id(key: str) -> str:
    """Format the idempotency key as a Razorpay reference_id.

    Razorpay's reference_id has a 40-character limit. We prefix with "sr-"
    (Second Rail) to make the source visible in the dashboard and leave room
    for the 32-char key. Total: "sr-" + 32 chars = 35 chars, well within limit.
    """
    return f"sr-{key}"
