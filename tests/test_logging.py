from __future__ import annotations

import json
import logging

from src.logging_setup import JSONFormatter, get_logger


def _format_one(msg: str, *args, **extra) -> dict:
    logger = get_logger("test.redact", stage="test")
    record = logger.logger.makeRecord(
        "test.redact", logging.WARNING, __file__, 1, msg, args, None, extra=extra
    )
    return json.loads(JSONFormatter().format(record))


def test_razorpay_key_is_redacted():
    # Deliberately under 14 chars after the prefix -- a real Razorpay key id
    # is longer, and scripts/secrets_audit.sh's own history scan flags
    # anything that length-shaped in this repo's git log. The redaction
    # regex itself has no length floor, so this still exercises it.
    payload = _format_one("leaked key: %s", "rzp_test_ABC123")
    assert "rzp_test_ABC123" not in payload["msg"]
    assert "[REDACTED]" in payload["msg"]


def test_llm_provider_keys_are_redacted():
    for fake_key in [
        "sk-" + "a" * 24,
        "AIza" + "b" * 35,
        "gsk_" + "c" * 24,
    ]:
        payload = _format_one("key=%s", fake_key)
        assert fake_key not in payload["msg"]
        assert "[REDACTED]" in payload["msg"]


def test_email_contact_value_is_redacted():
    payload = _format_one("contact %s failed delivery", "customer@example.com")
    assert "customer@example.com" not in payload["msg"]
    assert "[REDACTED]" in payload["msg"]


def test_ordinary_message_is_untouched():
    payload = _format_one("schema drift on payment_id=%s", "pay_ABC123")
    assert payload["msg"] == "schema drift on payment_id=pay_ABC123"
    assert "[REDACTED]" not in payload["msg"]
