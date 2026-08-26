"""Webhook HMAC-SHA256 signature verification.

Razorpay signs the raw request body — not the parsed-and-reserialised JSON —
with the webhook secret, HMAC-SHA256, hex-encoded, sent in the
`X-Razorpay-Signature` header. This module must therefore be handed the
exact bytes FastAPI received (`await request.body()`), read *before* the
body is parsed as JSON. Re-serialising a parsed dict back to JSON can
silently reorder keys, change float formatting, or change whitespace, which
changes the byte sequence and produces a different HMAC even though the
"content" looks identical — this is the single most common way this check
gets written wrong, so app.py reads and hashes the raw bytes first and only
parses JSON after this call succeeds.
"""

from __future__ import annotations

import hashlib
import hmac

from src.errors import SignatureError


def verify_signature(raw_body: bytes, header_signature: str, secret: str) -> bool:
    if not header_signature:
        raise SignatureError(
            "missing X-Razorpay-Signature header",
            remediation="Razorpay always sends this header — check the webhook config",
            code="SIGNATURE_HEADER_MISSING",
        )
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, header_signature):
        raise SignatureError(
            "X-Razorpay-Signature does not match the computed HMAC of the raw request body",
            remediation="verify RAZORPAY_WEBHOOK_SECRET matches the secret in the dashboard",
            code="SIGNATURE_MISMATCH",
        )
    return True
