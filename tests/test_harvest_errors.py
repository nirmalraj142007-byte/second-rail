from __future__ import annotations

from scripts.harvest_errors import _redact_pii


def test_redact_pii_strips_email_and_contact():
    payment = {
        "id": "pay_1",
        "email": "someone@example.com",
        "contact": "+919999999999",
        "error_code": "BAD_REQUEST_ERROR",
    }

    redacted = _redact_pii(payment)

    assert redacted["email"] == "[redacted]"
    assert redacted["contact"] == "[redacted]"
    assert redacted["error_code"] == "BAD_REQUEST_ERROR"


def test_redact_pii_strips_vpa_top_level_and_nested():
    payment = {
        "id": "pay_1",
        "vpa": "someone@okicici",
        "upi": {"vpa": "someone@okicici", "flow": "collect"},
    }

    redacted = _redact_pii(payment)

    assert redacted["vpa"] == "[redacted]"
    assert redacted["upi"]["vpa"] == "[redacted]"
    assert redacted["upi"]["flow"] == "collect"


def test_redact_pii_leaves_null_fields_alone():
    payment = {"id": "pay_1", "email": None, "contact": None, "vpa": None, "upi": None}

    redacted = _redact_pii(payment)

    assert redacted["email"] is None
    assert redacted["contact"] is None
    assert redacted["vpa"] is None
    assert redacted["upi"] is None


def test_redact_pii_does_not_mutate_input():
    payment = {"id": "pay_1", "email": "someone@example.com"}

    _redact_pii(payment)

    assert payment["email"] == "someone@example.com"
