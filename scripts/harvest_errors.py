"""One-time researcher script: harvest real Razorpay test-mode error strings.

Second Rail's single most important evidence requirement is at least one
input in the submission that was NOT authored by the builder. This script
is how that input gets captured. Every string that lands in
evidence/harvested_errors.jsonl comes from a real payment object Razorpay
returned for a real forced failure — nothing here is paraphrased,
normalised, or invented. The card numbers, the UPI VPA, and the amount ->
error-reason mapping below are copied verbatim from Razorpay's own current
documentation (URLs and fetch date below); none of it comes from memory.

Forcing a failure requires completing Razorpay's hosted checkout page for
each scenario — selecting a payment method, entering the test instrument,
and (for cards) clicking Failure on the mock bank screen. As of the fetch
date below, there is no documented server-to-server payment-creation
endpoint for either card or UPI on a standard test account, so that step
is not headlessly drivable through the REST API alone. This script
therefore runs in two passes:

  1. `python -m scripts.harvest_errors` (also `make harvest`) creates one
     Payment Link per documented test scenario — a real API call each —
     and writes evidence/harvest_manifest.json with every link's
     short_url. It also runs the fully headless steps: the reference_id
     duplicate-rejection probe (Step 5), and the documentation writeups
     (Steps 1 and 6).
  2. A human (or a browser-driving agent) opens each pending short_url and
     completes the checkout with the listed test instrument. No manual
     bookkeeping is required afterwards: each Payment Link response carries
     an `order_id` field (present on live responses but absent from
     Razorpay's own documented schema for this endpoint — a real, if minor,
     doc-drift finding of its own), and every payment attempt against that
     order — captured or failed — is discoverable via fetch_order_payments.
     Re-running `python -m scripts.harvest_errors` backfills order_id for
     any link created before this was discovered, then auto-discovers and
     fetches every completed checkout by order_id, folding the results into
     evidence/harvested_errors.jsonl. A manual fallback still exists for the
     rare case a scenario's order_id can't be recovered:
       python -m scripts.harvest_errors record <reference_id> <payment_id>

The manifest is resumable by design, and stays that way regardless of what
any account-level ceiling is doing. A test-mode account can start refusing
`POST /payment_links` mid-harvest for reasons outside this script's control
— Phase 8 hit `RATE_LIMIT_EXCEEDED "test mode limit of 30 reached for
payment_link"` on this very account (historical; that cap has not bound
since 30 Aug 2026 and re-verified clear on 2 Sep 2026 — see
LIMITATIONS.md). The manifest exists so a partial run resumes instead of
restarting, and so re-running never recreates links that already exist.
That property is what makes the script safe to re-run, not an assumption
about any particular limit being in force.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ulid import ULID

from src.config import load_settings, require_razorpay
from src.errors import ConfigError, ExecutorError
from src.razorpay_client import RazorpayClient

IST = timezone(timedelta(hours=5, minutes=30))

EVIDENCE_DIR = Path("evidence")
MANIFEST_PATH = EVIDENCE_DIR / "harvest_manifest.json"
PROBE_RESULT_PATH = EVIDENCE_DIR / "harvest_probe_result.json"
HARVESTED_PATH = EVIDENCE_DIR / "harvested_errors.jsonl"
FIELD_REPORT_PATH = EVIDENCE_DIR / "razorpay_field_report.md"
ERROR_SNAPSHOT_PATH = EVIDENCE_DIR / "razorpay_error_codes_snapshot.md"

FETCH_DATE = "2026-08-25"
CARD_DOC_URL = "https://razorpay.com/docs/payments/payments/test-card-details/"
UPI_DOC_URL = "https://razorpay.com/docs/payments/payments/test-upi-details/"
ERRORS_DOC_URL = "https://razorpay.com/docs/errors/"
ERRORS_COMMON_URL = "https://razorpay.com/docs/errors/common/"
PAYMENT_ENTITY_DOC_URL = "https://razorpay.com/docs/api/payments/entity/"
PAYMENT_LINK_CREATE_DOC_URL = "https://razorpay.com/docs/api/payments/payment-links/create-standard/"

# Verbatim from CARD_DOC_URL "Error Scenarios" tables, fetched FETCH_DATE.
# Any random CVV and any future expiry date, per the docs' own instruction.
CARD_SCENARIOS: list[dict[str, str]] = [
    {
        "reason": "payment_timed_out",
        "category": "BAD_REQUEST_ERROR",
        "card_number": "4100 2800 0009 0000",
        "description": "Your payment could not be completed due to a temporary issue. Try again later.",
    },
    {
        "reason": "insufficient_fund",
        "category": "BAD_REQUEST_ERROR",
        "card_number": "4100 2800 0008 0001",
        "description": "Your payment could not be completed due to insufficient account balance. Try another card or payment method.",
    },
    {
        "reason": "payment_cancelled",
        "category": "BAD_REQUEST_ERROR",
        "card_number": "4100 2800 0007 0002",
        "description": "Your payment has been cancelled. Try again or complete the payment later.",
    },
    {
        "reason": "card_declined",
        "category": "BAD_REQUEST_ERROR",
        "card_number": "4100 2800 0006 0003",
        "description": "Your payment did not go through as it was declined by the bank. Try another payment method or contact your bank.",
    },
    {
        "reason": "card_disabled_for_online_payments",
        "category": "BAD_REQUEST_ERROR",
        "card_number": "4100 2800 0003 0006",
        "description": "Your card is disabled for online payments. Please reach to your bank or try with another card.",
    },
    {
        "reason": "card_number_invalid",
        "category": "BAD_REQUEST_ERROR",
        "card_number": "4100 2800 0001 0008",
        "description": "You have entered an incorrect card number. Try again.",
    },
    {
        "reason": "gateway_technical_error",
        "category": "GATEWAY_ERROR",
        "card_number": "4100 2800 0002 0007",
        "description": "Your payment did not go through due to a temporary issue. Any debited amount will be refunded in 4-5 business days.",
    },
    {
        "reason": "authentication_failed",
        "category": "GATEWAY_ERROR",
        "card_number": "4100 2800 0000 0009",
        "description": "Your payment could not be completed due to incorrect OTP or verification details. Try another payment method or contact your bank for details.",
    },
]

# Verbatim from UPI_DOC_URL "UPI Collect" tables, fetched FETCH_DATE. VPA
# failure@razorpay; the payment amount (in paise) selects the error.
UPI_FAILURE_VPA = "failure@razorpay"
UPI_SCENARIOS: list[dict[str, Any]] = [
    {"amount_paise": 204, "reason": "incorrect_pin", "category": "BAD_REQUEST_ERROR",
     "description": "You have entered an incorrect PIN on the UPI app. Please retry with the correct PIN."},
    {"amount_paise": 205, "reason": "pin_not_set", "category": "BAD_REQUEST_ERROR",
     "description": "Payment was unsuccessful as you have not set the UPI PIN on the app. Try using another method."},
    {"amount_paise": 206, "reason": "pin_attempts_exceeded", "category": "BAD_REQUEST_ERROR",
     "description": "Payment was unsuccessful as you have breached the limit to enter UPI PIN incorrectly. Try using another method."},
    {"amount_paise": 208, "reason": "transaction_limit_exceeded", "category": "BAD_REQUEST_ERROR",
     "description": "Payment was unsuccessful as you exceeded the amount limit for the day with this bank account. Try using another account."},
    {"amount_paise": 209, "reason": "transaction_limit_exceeded", "category": "BAD_REQUEST_ERROR",
     "description": "Payment was unsuccessful as you exceeded the amount limit for the day with this bank account. Try using another account."},
    {"amount_paise": 210, "reason": "transaction_frequency_limit_exceeded", "category": "BAD_REQUEST_ERROR",
     "description": "Payment was unsuccessful as you exceeded the number of attempts on the bank account with this UPI ID. Try using another account."},
    {"amount_paise": 212, "reason": "debit_instrument_blocked", "category": "BAD_REQUEST_ERROR",
     "description": "Payment was unsuccessful as the account linked to this UPI ID is blocked. Try using another account."},
    {"amount_paise": 304, "reason": "payment_declined", "category": "BAD_REQUEST_ERROR",
     "description": "You have declined the payment request on the UPI app. Please retry when you are ready."},
    {"amount_paise": 407, "reason": "invalid_device", "category": "BAD_REQUEST_ERROR",
     "description": "Payment was unsuccessful as you may not be registered on the app you are trying to pay with. Try using another method."},
    {"amount_paise": 104, "reason": "bank_technical_error", "category": "GATEWAY_ERROR",
     "description": "Payment was unsuccessful due to a temporary issue at your bank. Any amount deducted will be refunded within 5-7 working days."},
    {"amount_paise": 105, "reason": "payment_timed_out", "category": "GATEWAY_ERROR",
     "description": "Payment was unsuccessful due to a temporary issue. Any amount deducted will be refunded within 5-7 working days."},
    {"amount_paise": 106, "reason": "bank_technical_error", "category": "GATEWAY_ERROR",
     "description": "Payment was unsuccessful due to a temporary issue. Any amount deducted will be refunded within 5-7 working days."},
    {"amount_paise": 107, "reason": "upi_app_not_available", "category": "GATEWAY_ERROR",
     "description": "Payment was unsuccessful as the UPI app is not reachable at this time."},
    {"amount_paise": 211, "reason": None, "category": "GATEWAY_ERROR",
     "description": "Beneficiary account is blocked."},
    {"amount_paise": 213, "reason": "beneficiary_account_does_not_exist", "category": "GATEWAY_ERROR",
     "description": "Payment was unsuccessful as the receiver's bank account is inactive. Any amount deducted will be refunded within 5-7 working days."},
    {"amount_paise": 404, "reason": "payment_risk_check_failed", "category": "GATEWAY_ERROR",
     "description": "Payment was unsuccessful as your account does not pass the risk checks done by your bank. Try using another account."},
    {"amount_paise": 405, "reason": "payment_risk_check_failed", "category": "GATEWAY_ERROR",
     "description": "Payment was unsuccessful as your account does not pass the risk checks done by your bank. Try using another account."},
    {"amount_paise": 406, "reason": "duplicate_request", "category": "GATEWAY_ERROR",
     "description": "Payment was unsuccessful due to a temporary issue. If amount got deducted, it will be refunded within 5-7 working days."},
]

# Common, endpoint-independent API errors — verbatim from ERRORS_COMMON_URL,
# fetched FETCH_DATE.
COMMON_API_ERRORS: list[dict[str, str]] = [
    {"code": "BAD_REQUEST_ERROR", "description": "The requested URL was not found on the server",
     "cause": "Incorrect API method, or the feature is not enabled on the account."},
    {"code": "BAD_REQUEST_ERROR", "description": "Access Denied",
     "cause": "Whitelisted IPs configured on the account."},
    {"code": "BAD_REQUEST_ERROR", "description": "The api key provided is invalid",
     "cause": "Wrong key/secret, or a test-mode key used in live mode (or vice versa)."},
    {"code": "BAD_REQUEST_ERROR", "description": "The id provided does not exist or access is unauthorised",
     "cause": "Entity ID doesn't exist, belongs to a different account, or mode mismatch."},
    {"code": "BAD_REQUEST_ERROR", "description": "The amount field is required.",
     "cause": "A mandatory request parameter was omitted."},
    {"code": "BAD_REQUEST_ERROR", "description": "The amount must be an integer.",
     "cause": "Wrong data type or format for a parameter."},
    {"code": "BAD_REQUEST_ERROR", "description": "Too many requests",
     "cause": "The account's undocumented rate limit was exceeded."},
    {"code": "SERVER_ERROR", "description": "We are facing some trouble completing your request at the moment. Please try again shortly.",
     "cause": "Transient server-side failure; retry after a short delay."},
]

# Endpoint-specific errors relevant to this project — verbatim from
# PAYMENT_LINK_CREATE_DOC_URL "Errors" section, fetched FETCH_DATE.
PAYMENT_LINK_CREATE_ERRORS: list[dict[str, str]] = [
    {"status": "400", "description": "payment link creation with reference ID already attempted",
     "cause": "An existing reference_id has been passed."},
    {"status": "400", "description": "amount: cannot be blank.",
     "cause": "The request body is missing the amount field, or the body is empty."},
    {"status": "400", "description": "amount: amount should be minimum 100 for INR.",
     "cause": "The amount is below the per-currency minimum (100 paise / Rs 1.00 for INR)."},
    {"status": "400", "description": "reference_id: the length must be no more than 40.",
     "cause": "reference_id exceeds the 40-character limit."},
    {"status": "400", "description": "UPI Payment Links is not supported in Test Mode. Please experience the product in Live Mode.",
     "cause": "upi_link=true was passed with test-mode API keys."},
]


def _fail(message: str) -> None:
    print(f"harvest_errors: {message}", file=sys.stderr)
    sys.exit(2)


def _load_manifest() -> list[dict[str, Any]]:
    if MANIFEST_PATH.exists():
        return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    return []


def _save_manifest(manifest: list[dict[str, Any]]) -> None:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _load_probe_result() -> dict[str, Any] | None:
    if PROBE_RESULT_PATH.exists():
        return json.loads(PROBE_RESULT_PATH.read_text(encoding="utf-8"))
    return None


def _save_probe_result(result: dict[str, Any]) -> None:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    PROBE_RESULT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _build_scenarios() -> list[dict[str, Any]]:
    scenarios: list[dict[str, Any]] = []
    for i, card in enumerate(CARD_SCENARIOS):
        scenarios.append(
            {
                "reference_id": f"sr-harvest-card-{i:02d}-{card['reason']}"[:40],
                "instrument": "card",
                "forced_by": card["card_number"],
                "amount_paise": 100,
                "expected_category": card["category"],
                "expected_error_reason": card["reason"],
                "expected_error_description": card["description"],
                "plink_id": None,
                "short_url": None,
                "order_id": None,
                "payment_id": None,
                "raw_payment": None,
            }
        )
    for i, upi in enumerate(UPI_SCENARIOS):
        scenarios.append(
            {
                "reference_id": f"sr-harvest-upi-{i:02d}-{upi['reason'] or 'unlabelled'}"[:40],
                "instrument": "upi",
                "forced_by": UPI_FAILURE_VPA,
                "amount_paise": upi["amount_paise"],
                "expected_category": upi["category"],
                "expected_error_reason": upi["reason"],
                "expected_error_description": upi["description"],
                "plink_id": None,
                "short_url": None,
                "order_id": None,
                "payment_id": None,
                "raw_payment": None,
            }
        )
    return scenarios


def _ensure_payment_links(client: RazorpayClient, manifest: list[dict[str, Any]]) -> None:
    for entry in manifest:
        if entry["plink_id"] is not None:
            continue
        payload = {
            "amount": entry["amount_paise"],
            "currency": "INR",
            "description": f"Second Rail harvest — {entry['instrument']} / {entry['expected_error_reason']}",
        }
        try:
            link = client.create_payment_link(payload, entry["reference_id"])
        except ExecutorError as exc:
            print(f"  ! failed to create link for {entry['reference_id']}: {exc}", file=sys.stderr)
            continue
        entry["plink_id"] = link["id"]
        entry["short_url"] = link["short_url"]
        entry["order_id"] = link.get("order_id")
        print(
            f"  created {link['id']} ({entry['instrument']}, {entry['expected_error_reason']}): "
            f"{link['short_url']}"
        )


def _backfill_order_ids(client: RazorpayClient, manifest: list[dict[str, Any]]) -> None:
    # order_id isn't in Razorpay's documented Payment Links response schema
    # (found by fetching a live link back and comparing against the docs —
    # see the field report), so entries created before this was discovered
    # need one GET each to pick it up.
    for entry in manifest:
        if entry["plink_id"] and not entry["order_id"]:
            try:
                link = client.fetch_payment_link(entry["plink_id"])
            except ExecutorError as exc:
                print(f"  ! failed to backfill order_id for {entry['plink_id']}: {exc}", file=sys.stderr)
                continue
            entry["order_id"] = link.get("order_id")


# Fields on the Payment entity that can carry a real customer's contact
# details, entered by whoever completes the checkout. This project commits
# evidence/harvested_errors.jsonl to a public repo, so no value entered
# during a real checkout — even a harvest researcher's own number, typed in
# because the field is required — ever gets stored past this point. Only
# the taxonomy-relevant error fields and instrument metadata this harvest
# exists to capture are exempt from redaction.
_PII_FIELDS = ("email", "contact", "vpa")


def _redact_pii(payment: dict[str, Any]) -> dict[str, Any]:
    redacted = dict(payment)
    for field in _PII_FIELDS:
        if redacted.get(field):
            redacted[field] = "[redacted]"
    upi = redacted.get("upi")
    if isinstance(upi, dict) and upi.get("vpa"):
        redacted["upi"] = {**upi, "vpa": "[redacted]"}
    return redacted


def _fetch_completed(client: RazorpayClient, manifest: list[dict[str, Any]]) -> None:
    for entry in manifest:
        if entry["raw_payment"] is not None:
            continue
        if entry["payment_id"]:
            try:
                entry["raw_payment"] = _redact_pii(client.fetch_payment(entry["payment_id"]))
            except ExecutorError as exc:
                print(f"  ! failed to fetch {entry['payment_id']}: {exc}", file=sys.stderr)
            continue
        if entry["order_id"]:
            try:
                payments = client.fetch_order_payments(entry["order_id"])
            except ExecutorError as exc:
                print(f"  ! failed to fetch payments for {entry['order_id']}: {exc}", file=sys.stderr)
                continue
            if payments:
                payment = _redact_pii(payments[-1])
                entry["payment_id"] = payment.get("id")
                entry["raw_payment"] = payment
                print(f"  discovered {payment.get('id')} for {entry['reference_id']} (auto, via order_id)")


# (token_iin, last4) -> the verbatim documented card number string, so a
# captured payment can be traced back to the exact card Razorpay's own docs
# list, rather than reconstructed by guessing digit grouping from last4.
_KNOWN_CARDS = {
    (c["card_number"].replace(" ", "")[:9], c["card_number"].replace(" ", "")[-4:]): c["card_number"]
    for c in CARD_SCENARIOS
}


def _real_forced_by(payment: dict[str, Any]) -> str | None:
    """The instrument string actually used, read back from the real payment
    object — not the scenario's plan. Plans and reality diverged in this
    harvest (UPI wasn't available on the account; several UPI-planned
    scenarios ended up completed via card or netbanking instead), so only
    the payment object itself can say what really forced the failure.
    """
    method = payment.get("method")
    if method == "card":
        card = payment.get("card") or {}
        key = (card.get("token_iin"), card.get("last4"))
        known = _KNOWN_CARDS.get(key)
        if known:
            return known
        return f"card ending {card.get('last4')} (iin {card.get('token_iin')}, not one of the documented scenario cards)"
    if method == "upi":
        return payment.get("vpa")
    if method == "netbanking":
        return payment.get("bank")
    if method == "wallet":
        return payment.get("wallet")
    return None


def _write_harvested(manifest: list[dict[str, Any]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for entry in manifest:
        payment = entry.get("raw_payment")
        if not payment:
            continue
        records.append(
            {
                "harvest_id": str(ULID()),
                "captured_at": datetime.now(IST).isoformat(timespec="seconds"),
                "forced_by": _real_forced_by(payment),
                "instrument": payment.get("method"),
                "payment_id": payment.get("id"),
                "amount_paise": payment.get("amount"),
                "error_code": payment.get("error_code"),
                "error_description": payment.get("error_description"),
                "error_source": payment.get("error_source"),
                "error_step": payment.get("error_step"),
                "error_reason": payment.get("error_reason"),
                "planned_instrument": entry["instrument"],
                "planned_forced_by": entry["forced_by"],
                "planned_error_reason": entry["expected_error_reason"],
                "raw_payment": payment,
            }
        )
    lines = [json.dumps(r, ensure_ascii=False) for r in records]
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    HARVESTED_PATH.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return records


def _reference_id_probe(client: RazorpayClient) -> dict[str, Any] | None:
    suffix = str(ULID())[-10:].lower()
    reference_id = f"secondrail-probe-{suffix}"[:40]
    payload = {"amount": 100, "currency": "INR", "description": "Second Rail reference_id probe"}

    try:
        first = client.create_payment_link(payload, reference_id)
    except ExecutorError as exc:
        print(f"  ! probe's first create_payment_link call failed, skipping probe this run: {exc}", file=sys.stderr)
        return None

    result: dict[str, Any] = {"reference_id": reference_id, "first_plink_id": first["id"]}

    second_plink_id = None
    try:
        second = client.create_payment_link(payload, reference_id)
        second_plink_id = second["id"]
        result["outcome"] = "SUCCEEDED — Razorpay created a second Payment Link with the same reference_id"
        result["second_plink_id"] = second_plink_id
    except ExecutorError as exc:
        result["outcome"] = "REJECTED — Razorpay refused the duplicate reference_id"
        result["status_code"] = getattr(exc, "status_code", None)
        result["response_body"] = getattr(exc, "response_body", None)

    cancelled = []
    for plink_id in (first["id"], second_plink_id):
        if plink_id is None:
            continue
        try:
            client.cancel_payment_link(plink_id)
            cancelled.append(plink_id)
        except ExecutorError as exc:
            print(f"  ! failed to cancel {plink_id}: {exc}", file=sys.stderr)
    result["cancelled"] = cancelled
    print(f"  cancelled: {cancelled}")
    return result


def _field_presence_counts(records: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    fields = ["error_code", "error_description", "error_source", "error_step", "error_reason"]
    counts = {f: {"present": 0, "null": 0, "absent": 0} for f in fields}
    for record in records:
        payment = record["raw_payment"]
        for f in fields:
            if f not in payment:
                counts[f]["absent"] += 1
            elif payment[f] is None:
                counts[f]["null"] += 1
            else:
                counts[f]["present"] += 1
    return counts


def _write_field_report(probe: dict[str, Any] | None, records: list[dict[str, Any]]) -> None:
    n = len(records)
    counts = _field_presence_counts(records)

    lines: list[str] = []
    lines.append("# Razorpay Field Report")
    lines.append("")
    lines.append(
        f"Generated by `scripts/harvest_errors.py` from {n} real captured payment objects "
        f"and documentation read on {FETCH_DATE}."
    )
    lines.append("")
    lines.append("## Step 1 — documentation read before writing any test data")
    lines.append("")
    lines.append(f"Fetched {FETCH_DATE} (fully rendered pages, not the static HTML — the docs site")
    lines.append("renders these tables client-side and a plain HTTP fetch misses them):")
    lines.append("")
    lines.append(f"- {CARD_DOC_URL} — Test Cards for Indian Payments + Error Scenarios tables")
    lines.append(f"- {UPI_DOC_URL} — UPI Collect / UPI Intent error-scenario tables")
    lines.append(f"- {ERRORS_DOC_URL} — the generic API error-response shape")
    lines.append(f"- {ERRORS_COMMON_URL} — endpoint-independent common errors")
    lines.append(f"- {PAYMENT_ENTITY_DOC_URL} — the Payment entity's own field list")
    lines.append(f"- {PAYMENT_LINK_CREATE_DOC_URL} — Payment Links create request/response/errors")
    lines.append("")
    lines.append("**Finding worth flagging:** the generic API error-response object documented at")
    lines.append(
        f"{ERRORS_DOC_URL} uses unprefixed field names — `code`, `description`, `field`, `source`, "
    )
    lines.append(
        "`step`, `reason` — returned when an API *call itself* fails (bad request body, etc.). That is a"
    )
    lines.append(
        "different, easily-confused schema from the Payment *entity's* own fields, which are prefixed —"
    )
    lines.append(
        "`error_code`, `error_description`, `error_source`, `error_step`, `error_reason` — and describe why"
    )
    lines.append(
        "a *payment* (not the API call that fetched it) failed. This project only ever reads the second"
    )
    lines.append(
        "one (via fetch_payment / the payment.failed webhook), confirmed present on the live Payment entity"
    )
    lines.append(f"docs page as of {FETCH_DATE}.")
    lines.append("")
    lines.append("## Steps 2 & 3 — forcing failures: what actually happened")
    lines.append("")
    if n == 0:
        lines.append("Not yet run.")
    else:
        lines.append(
            "**UPI was not available as a payment method on this test account.** Every UPI-planned "
            "scenario's checkout page offered Card, Netbanking, and Wallet only — confirmed by direct "
            "observation, not inferred from a missing field. This is the honest, documented-gap outcome "
            "the harvest plan allows for: rather than invent a UPI payment, every UPI-planned link that "
            "got completed was completed via whichever real method was actually offered, and the "
            "`instrument`/`forced_by` fields below record what really happened, not what was planned "
            "(see `planned_instrument`/`planned_forced_by` on each record for the original intent)."
        )
        lines.append("")
        instruments = sorted({r["instrument"] for r in records})
        reasons = sorted({r["error_reason"] for r in records if r["error_reason"]})
        lines.append(f"- Real instruments captured: {', '.join(instruments)}")
        lines.append(f"- Distinct `error_reason` values across all {n} records: {', '.join(reasons)}")
        lines.append("")
        card_records = [r for r in records if r["instrument"] == "card"]
        card_reasons = {r["error_reason"] for r in card_records}
        distinct_cards = sorted({r["forced_by"] for r in card_records if r["forced_by"]})
        if card_records and len(card_reasons) == 1:
            lines.append(
                f"**Finding that shrinks the harvest's own premise:** all {len(card_records)} card "
                f"payments were forced using one of the {len(distinct_cards)} distinct documented "
                "\"Error Scenario\" card numbers from Razorpay's test-card docs (each reused across "
                "multiple checkouts — see the `forced_by` field on each record for exactly which "
                "number produced which payment), completed by clicking Failure on the mock bank "
                f"screen as instructed. Every one of them came back with the same generic "
                f"`error_reason: \"{next(iter(card_reasons))}\"` rather than the specific documented "
                "reason its card number is supposed to trigger (e.g. `insufficient_fund`, "
                "`card_declined`, `authentication_failed`, ...). On this account, at least, the "
                "per-card-number error mapping documented on the test-card page does not reproduce — "
                "the mock bank's Failure button appears to always return the same generic gateway "
                "authorization failure regardless of which of the 8 cards initiated it. This was not "
                "the expected result going in."
            )
        lines.append("")
    lines.append("## Step 4 — field existence, from real captured payment objects")
    lines.append("")
    if n == 0:
        lines.append(
            "**No payment objects captured yet.** Every Payment Link this script created is documented in "
            "`evidence/harvest_manifest.json`, pending an interactive checkout completion for each "
            "scenario — see the module docstring in `scripts/harvest_errors.py` for the two-pass process. "
            "This section will report real present/null/absent counts once at least one payment has been "
            "captured; it is not populated from the documentation above, only from actual API responses."
        )
    else:
        lines.append("| field | present | null | absent | n |")
        lines.append("|---|---|---|---|---|")
        for field, c in counts.items():
            lines.append(f"| `{field}` | {c['present']} | {c['null']} | {c['absent']} | {n} |")
    lines.append("")
    lines.append("## Step 5 — reference_id duplicate-rejection probe")
    lines.append("")
    if probe is None:
        lines.append(
            "**Not completed on this run.** The probe's first `create_payment_link` call did not "
            "succeed, so there was nothing to duplicate against and the probe was skipped — see the "
            "stderr line this run emitted for the specific error. Razorpay's own Payment Links "
            "\"Create\" error list documents `payment link creation with reference ID already "
            "attempted` as a 400 response to a duplicate `reference_id`, and this project confirmed "
            "that empirically on 2 Sep 2026 (HTTP 400, `BAD_REQUEST_ERROR`) — but the documented "
            "answer and the confirmed one are not interchangeable, so this section reports only what "
            "*this* run observed. Re-run `make harvest` to complete it; the result is persisted to "
            "evidence/harvest_probe_result.json and reused on later runs."
        )
    else:
        lines.append(f"- reference_id used: `{probe['reference_id']}`")
        lines.append(f"- **Result: {probe['outcome']}**")
        if "status_code" in probe:
            lines.append(f"- HTTP status on the second attempt: {probe['status_code']}")
            lines.append(f"- response body: `{probe.get('response_body')}`")
        if "second_plink_id" in probe:
            lines.append(f"- second link id created: {probe['second_plink_id']}")
        lines.append(f"- both links cancelled: {probe['cancelled']}")
        lines.append("")
        if probe["outcome"].startswith("REJECTED"):
            lines.append(
                "**Implication for Phase 8:** Razorpay itself enforces reference_id uniqueness at "
                "creation time, so the idempotency key can be handed straight to `reference_id` and a "
                "second execution attempt with the same key is rejected server-side — no separate "
                "client-side dedup check is required to prevent a duplicate *link*, though the local "
                "SQLite UNIQUE constraint on `idempotency_key` is still needed to short-circuit before "
                "making the call at all."
            )
        else:
            lines.append(
                "**Implication for Phase 8:** server-side dedup on `reference_id` CANNOT be relied on. "
                "The idempotency guarantee must be enforced entirely client-side, via the SQLite UNIQUE "
                "constraint on `idempotency_key`, checked before every create_payment_link call."
            )
    lines.append("")

    FIELD_REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_error_snapshot() -> None:
    lines: list[str] = []
    lines.append("# Razorpay Error Codes Snapshot")
    lines.append("")
    lines.append(
        f"Source: {ERRORS_COMMON_URL} and the per-instrument error tables on {CARD_DOC_URL} and "
        f"{UPI_DOC_URL}. Fetched {FETCH_DATE}. This is a second, independent external label source — "
        "reproduced as tables of codes and short descriptions, not the page's prose."
    )
    lines.append("")
    lines.append("## Common API errors (endpoint-independent)")
    lines.append("")
    lines.append("| code | description | cause |")
    lines.append("|---|---|---|")
    for e in COMMON_API_ERRORS:
        lines.append(f"| `{e['code']}` | {e['description']} | {e['cause']} |")
    lines.append("")
    lines.append("## Payment Links — create endpoint errors relevant to this project")
    lines.append("")
    lines.append("| HTTP status | description | cause |")
    lines.append("|---|---|---|")
    for e in PAYMENT_LINK_CREATE_ERRORS:
        lines.append(f"| {e['status']} | {e['description']} | {e['cause']} |")
    lines.append("")
    lines.append("## Card payment failure reasons (test-mode, forced via test card number)")
    lines.append("")
    lines.append("| category | reason | description |")
    lines.append("|---|---|---|")
    for c in CARD_SCENARIOS:
        lines.append(f"| {c['category']} | `{c['reason']}` | {c['description']} |")
    lines.append("")
    lines.append("## UPI Collect failure reasons (test-mode, forced via amount against failure@razorpay)")
    lines.append("")
    lines.append("| amount (paise) | category | reason | description |")
    lines.append("|---|---|---|---|")
    for u in UPI_SCENARIOS:
        reason = f"`{u['reason']}`" if u["reason"] else "*(not documented — description only)*"
        lines.append(f"| {u['amount_paise']} | {u['category']} | {reason} | {u['description']} |")
    lines.append("")

    ERROR_SNAPSHOT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _cmd_record(manifest: list[dict[str, Any]], reference_id: str, payment_id: str) -> None:
    for entry in manifest:
        if entry["reference_id"] == reference_id:
            entry["payment_id"] = payment_id
            entry["raw_payment"] = None
            print(f"recorded {payment_id} for {reference_id}; run with no arguments to fetch and fold it in")
            return
    _fail(
        f"no manifest entry with reference_id {reference_id!r} — run with no arguments first "
        "to create the manifest"
    )


def main(argv: list[str]) -> None:
    settings = load_settings()
    try:
        key_id, key_secret = require_razorpay(settings)
    except ConfigError as exc:
        _fail(str(exc))
        return

    if not key_id.startswith("rzp_test_"):
        _fail(
            "RAZORPAY_KEY_ID does not start with rzp_test_ — this script refuses to run "
            "against anything but Razorpay test mode"
        )
        return

    manifest = _load_manifest()
    if not manifest:
        manifest = _build_scenarios()
    for entry in manifest:
        entry.setdefault("order_id", None)

    if len(argv) >= 4 and argv[1] == "record":
        _cmd_record(manifest, argv[2], argv[3])
        _save_manifest(manifest)
        return

    with RazorpayClient(key_id, key_secret) as client:
        print(f"Ensuring payment links exist for {len(manifest)} documented test scenarios...")
        _ensure_payment_links(client, manifest)
        _save_manifest(manifest)

        _backfill_order_ids(client, manifest)
        _save_manifest(manifest)

        print("Fetching payment objects for every completed checkout (auto-discovered via order_id)...")
        _fetch_completed(client, manifest)
        _save_manifest(manifest)

        records = _write_harvested(manifest)

        probe = _load_probe_result()
        if probe is not None:
            print("reference_id probe already recorded from a previous run — not re-running "
                  "(each attempt spends Payment Link quota).")
        else:
            print("Running the reference_id duplicate-rejection probe...")
            probe = _reference_id_probe(client)
            if probe is not None:
                _save_probe_result(probe)
            print(f"  {probe['outcome'] if probe else 'skipped this run — see stderr above'}")

    _write_field_report(probe, records)
    _write_error_snapshot()

    pending = [e for e in manifest if e["raw_payment"] is None]
    print(f"\n{len(records)} real failures captured in {HARVESTED_PATH}.")
    if pending:
        print(
            f"{len(pending)} scenario(s) still await an interactive checkout completion — "
            f"open each pending short_url in {MANIFEST_PATH}, complete the payment with the "
            "listed test instrument, then run:\n"
            "  python -m scripts.harvest_errors record <reference_id> <payment_id>"
        )
    if len(records) < 20:
        print(f"WARNING: only {len(records)}/20 required records captured so far.", file=sys.stderr)


if __name__ == "__main__":
    main(sys.argv)
