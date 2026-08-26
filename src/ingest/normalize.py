"""Turns a raw `payment.failed` webhook body into a validated NormalizedEpisode.

evidence/razorpay_field_report.md (Phase 1, 20 real captured payments) found
`error_code` / `error_description` / `error_source` / `error_step` /
`error_reason` present on every one of them — none is documented as ever
absent on a real Payment entity. So a payload missing one of them is genuine
schema drift, not a value this project may default: we raise
SchemaDriftError naming the exact field and payload path rather than
substituting a string like "UNKNOWN", which would silently poison the
downstream classifier's metrics. Fields that are legitimately absent
depending on instrument (e.g. `card` when the method is netbanking, `bank`
when the method is card) are read with `.get()` and passed through as
explicit None — that is normal shape variation, not drift.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from pydantic import BaseModel

from src.errors import SchemaDriftError

IST = timezone(timedelta(hours=5, minutes=30))

_METHOD_TO_INSTRUMENT: dict[str, str] = {
    "upi": "upi",
    "card": "card",
    "netbanking": "netbanking",
    "wallet": "wallet",
}


class NormalizedEpisode(BaseModel):
    payment_id: str
    order_id: str | None
    amount_paise: int
    currency: str
    instrument: Literal["upi", "card", "netbanking", "wallet"]
    issuer_family: str | None
    error_code: str
    error_description: str
    error_source: str
    error_step: str
    error_reason: str
    failed_at: str


def _payment_entity(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        entity = payload["payload"]["payment"]["entity"]
    except (KeyError, TypeError) as exc:
        raise SchemaDriftError(
            "webhook payload missing payload.payment.entity",
            remediation="confirm this is a genuine payment.failed body, not another event type",
            code="SCHEMA_DRIFT_MISSING_PAYMENT_ENTITY",
        ) from exc
    if not isinstance(entity, dict):
        raise SchemaDriftError(
            "payload.payment.entity is not an object",
            code="SCHEMA_DRIFT_MISSING_PAYMENT_ENTITY",
        )
    return entity


def _required(entity: dict[str, Any], field: str, path_prefix: str) -> Any:
    if field not in entity or entity[field] is None:
        raise SchemaDriftError(
            f"required field absent: {path_prefix}.{field}",
            remediation=(
                "evidence/razorpay_field_report.md does not document this field as ever "
                "absent on a real payment object — treat this as schema drift, not a default"
            ),
            code="SCHEMA_DRIFT_FIELD_MISSING",
        )
    return entity[field]


def _issuer_family(entity: dict[str, Any]) -> str | None:
    card = entity.get("card") or {}
    if card.get("issuer"):
        return str(card["issuer"])
    if entity.get("bank"):
        return str(entity["bank"])
    if entity.get("wallet"):
        return str(entity["wallet"])
    vpa = entity.get("vpa")
    if vpa and "@" in vpa:
        return vpa.split("@", 1)[1]
    return None


def extract_payment_id(payload: dict[str, Any]) -> str | None:
    """Best-effort payment_id lookup for dedup/correlation across every
    webhook shape this ingest boundary sees (payment.failed,
    payment.captured, payment_link.paid, payment_link.expired). Returns None
    rather than raising — payment_link.expired in particular carries no
    payment entity at all, since nothing ever succeeded. For payment.failed
    specifically, normalize_payment_failed() below is what actually enforces
    presence, via SchemaDriftError."""
    pl = payload.get("payload")
    if not isinstance(pl, dict):
        return None
    payment = pl.get("payment")
    if not isinstance(payment, dict):
        return None
    entity = payment.get("entity")
    if not isinstance(entity, dict):
        return None
    payment_id = entity.get("id")
    return str(payment_id) if payment_id else None


def normalize_payment_failed(payload: dict[str, Any]) -> NormalizedEpisode:
    entity = _payment_entity(payload)
    path = "payload.payment.entity"

    payment_id = _required(entity, "id", path)
    amount = _required(entity, "amount", path)
    currency = _required(entity, "currency", path)
    method = _required(entity, "method", path)
    error_code = _required(entity, "error_code", path)
    error_description = _required(entity, "error_description", path)
    error_source = _required(entity, "error_source", path)
    error_step = _required(entity, "error_step", path)
    error_reason = _required(entity, "error_reason", path)

    instrument = _METHOD_TO_INSTRUMENT.get(method)
    if instrument is None:
        raise SchemaDriftError(
            f"unrecognised value at {path}.method: {method!r} "
            "(expected one of upi/card/netbanking/wallet)",
            code="SCHEMA_DRIFT_UNKNOWN_METHOD",
        )

    created_at_epoch = entity.get("created_at")
    if created_at_epoch is not None:
        # The Payment entity has no separate "failed_at" — created_at (the
        # payment's own creation time) is the closest real timestamp Razorpay
        # gives us for when this attempt happened.
        failed_at = datetime.fromtimestamp(
            int(created_at_epoch), tz=IST
        ).isoformat(timespec="seconds")
    else:
        failed_at = datetime.now(IST).isoformat(timespec="seconds")

    return NormalizedEpisode(
        payment_id=str(payment_id),
        order_id=entity.get("order_id"),
        amount_paise=int(amount),
        currency=str(currency),
        instrument=instrument,  # type: ignore[arg-type]
        issuer_family=_issuer_family(entity),
        error_code=str(error_code),
        error_description=str(error_description),
        error_source=str(error_source),
        error_step=str(error_step),
        error_reason=str(error_reason),
        failed_at=failed_at,
    )
