"""Dedup and ordering boundary for incoming Razorpay webhooks.

Three rules, applied in order, and every branch is expected control flow —
none of them is an error to be logged and swallowed, and every branch writes
at least one audit record so nothing is ever silently dropped:

1. Replayed delivery (`webhook_event.event_id` already on file) -> suppressed,
   no episode. Razorpay retries any webhook that doesn't get a 2xx, so this
   is normal traffic, not a bug. `event_id` is Razorpay's own
   `X-Razorpay-Event-Id` header value and is this table's PRIMARY KEY, so a
   literal redelivery of the same event cannot produce a second row by
   construction — the row from the first delivery stands, and this branch
   returns without attempting a second INSERT. See BUILD_LOG.md for how this
   was actually confirmed against Razorpay's docs (it is not derived from the
   webhook body, which carries no event id of its own).

2. `payment.failed` for a `payment_id` that already has an episode ->
   suppressed, no new episode. The dedup key here is deliberately
   `payment_id`, NOT `event_id`: the same payment can generate multiple
   webhook deliveries under different event ids (e.g. a genuine second
   `payment.failed` fired by an upstream retry), and the episode table's
   `UNIQUE(payment_id)` is the actual dedup boundary this project promises.

3. A terminal event (`payment.captured`, `payment_link.paid`,
   `payment_link.expired`) for a `payment_id` with no prior episode ->
   `out_of_order`. Recorded, but no recovery episode is created — there is
   nothing to recover once the payment already succeeded or the link it
   would have used already died.

Anything else — a new `payment.failed`, or a terminal event correlating to
an existing episode — is `dedup_result="new"`.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel
from ulid import ULID

from src.audit.writer import AuditWriter
from src.config import Settings
from src.db.repo import (
    get_episode_by_payment_id,
    get_webhook_event,
    insert_episode,
    insert_exception_entry,
    insert_webhook_event,
)
from src.errors import SchemaDriftError
from src.ingest.normalize import (
    extract_payment_id,
    extract_terminal_event_fields,
    normalize_payment_failed,
)
from src.logging_setup import get_logger

IST = timezone(timedelta(hours=5, minutes=30))

TERMINAL_EVENT_TYPES = frozenset({"payment.captured", "payment_link.paid", "payment_link.expired"})


def _now_iso() -> str:
    return datetime.now(IST).isoformat(timespec="seconds")


class IngestResult(BaseModel):
    event_id: str
    event_type: str
    dedup_result: str
    episode_id: str | None = None
    exception_id: str | None = None


class IngestService:
    def __init__(self, conn: sqlite3.Connection, audit: AuditWriter, settings: Settings) -> None:
        self._conn = conn
        self._audit = audit
        self._settings = settings
        self._logger = get_logger("ingest", stage="gate")

    def handle_event(
        self,
        *,
        event_id: str,
        event_type: str,
        payload: dict,
        raw_body_hash: str,
    ) -> IngestResult:
        received_at = _now_iso()

        if get_webhook_event(self._conn, event_id) is not None:
            self._audit.append(
                stage="gate",
                actor="system",
                outcome="suppressed",
                rationale=f"dedup: replayed event_id {event_id!r} — row already exists",
            )
            return IngestResult(
                event_id=event_id, event_type=event_type, dedup_result="duplicate"
            )

        payment_id = extract_payment_id(payload)

        if event_type == "payment.failed":
            return self._handle_payment_failed(
                event_id, event_type, payload, payment_id, raw_body_hash, received_at
            )

        if event_type in TERMINAL_EVENT_TYPES:
            return self._handle_terminal_event(
                event_id, event_type, payload, payment_id, raw_body_hash, received_at
            )

        insert_webhook_event(
            self._conn,
            event_id=event_id,
            event_type=event_type,
            payment_id=payment_id,
            plink_id=None,
            raw_body_hash=raw_body_hash,
            signature_valid=True,
            received_at=received_at,
            processed=False,
            dedup_result="new",
        )
        self._audit.append(
            stage="gate",
            actor="system",
            payment_id=payment_id,
            outcome="ignored",
            rationale=f"event_type {event_type!r} is not handled by this ingest boundary",
        )
        return IngestResult(event_id=event_id, event_type=event_type, dedup_result="new")

    def _handle_payment_failed(
        self,
        event_id: str,
        event_type: str,
        payload: dict,
        payment_id: str | None,
        raw_body_hash: str,
        received_at: str,
    ) -> IngestResult:
        existing = get_episode_by_payment_id(self._conn, payment_id) if payment_id else None
        if existing is not None:
            insert_webhook_event(
                self._conn,
                event_id=event_id,
                event_type=event_type,
                payment_id=payment_id,
                plink_id=None,
                raw_body_hash=raw_body_hash,
                signature_valid=True,
                received_at=received_at,
                processed=True,
                dedup_result="duplicate",
            )
            self._audit.append(
                stage="gate",
                actor="system",
                episode_id=existing["episode_id"],
                payment_id=payment_id,
                outcome="suppressed",
                rationale=(
                    "dedup: payment_id already has an episode — the dedup key is "
                    "payment_id, not event_id, because the same payment can generate "
                    "multiple webhook deliveries under different event ids"
                ),
            )
            return IngestResult(
                event_id=event_id,
                event_type=event_type,
                dedup_result="duplicate",
                episode_id=existing["episode_id"],
            )

        try:
            normalized = normalize_payment_failed(payload)
        except SchemaDriftError as exc:
            insert_webhook_event(
                self._conn,
                event_id=event_id,
                event_type=event_type,
                payment_id=payment_id,
                plink_id=None,
                raw_body_hash=raw_body_hash,
                signature_valid=True,
                received_at=received_at,
                processed=True,
                dedup_result="new",
            )
            exception_id = str(ULID())
            insert_exception_entry(
                self._conn,
                exception_id=exception_id,
                run_id=None,
                episode_id=None,
                stage="ingest",
                reason_code=exc.code,
                reason_text=str(exc),
            )
            self._audit.append(
                stage="gate",
                actor="system",
                payment_id=payment_id,
                outcome="exception",
                rationale=str(exc),
            )
            self._logger.warning(
                "schema drift on payment_id=%s: %s", payment_id, exc, extra={"code": exc.code}
            )
            return IngestResult(
                event_id=event_id,
                event_type=event_type,
                dedup_result="new",
                exception_id=exception_id,
            )

        episode_id = str(ULID())
        insert_episode(
            self._conn,
            episode_id=episode_id,
            payment_id=normalized.payment_id,
            order_id=normalized.order_id,
            customer_id=None,
            amount_paise=normalized.amount_paise,
            currency=normalized.currency,
            instrument=normalized.instrument,
            issuer_family=normalized.issuer_family,
            error_code=normalized.error_code,
            error_description=normalized.error_description,
            error_source=normalized.error_source,
            error_step=normalized.error_step,
            error_reason=normalized.error_reason,
            failed_at=normalized.failed_at,
            received_at=received_at,
            split=None,
            is_synthetic=False,
            harvested_from=None,
        )
        insert_webhook_event(
            self._conn,
            event_id=event_id,
            event_type=event_type,
            payment_id=normalized.payment_id,
            plink_id=None,
            raw_body_hash=raw_body_hash,
            signature_valid=True,
            received_at=received_at,
            processed=True,
            dedup_result="new",
        )
        self._audit.append(
            stage="gate",
            actor="system",
            episode_id=episode_id,
            payment_id=normalized.payment_id,
            outcome="new_episode",
            rationale=(
                f"payment.failed ingested: {normalized.error_reason} on {normalized.instrument}"
            ),
        )
        return IngestResult(
            event_id=event_id, event_type=event_type, dedup_result="new", episode_id=episode_id
        )

    def _handle_terminal_event(
        self,
        event_id: str,
        event_type: str,
        payload: dict,
        payment_id: str | None,
        raw_body_hash: str,
        received_at: str,
    ) -> IngestResult:
        # A terminal event's payment_id (via extract_payment_id) refers to
        # whatever payment satisfied it. For payment_link.paid that is a
        # brand-new payment created against our recovery link, not the
        # original failed payment_id the episode was opened under — so
        # "existing" here is only used to decide out_of_order vs. new for
        # the dedup boundary. Actual outcome correlation for attribution
        # (src/attribute/rules.py) matches on plink_id/order_id, both
        # captured below regardless of whether an episode with this exact
        # payment_id happens to exist.
        fields = extract_terminal_event_fields(payload)
        existing = get_episode_by_payment_id(self._conn, payment_id) if payment_id else None

        if existing is None:
            insert_webhook_event(
                self._conn,
                event_id=event_id,
                event_type=event_type,
                payment_id=payment_id,
                plink_id=fields.plink_id,
                order_id=fields.order_id,
                amount_paise=fields.amount_paise,
                raw_body_hash=raw_body_hash,
                signature_valid=True,
                received_at=received_at,
                processed=True,
                dedup_result="out_of_order",
            )
            self._audit.append(
                stage="gate",
                actor="system",
                payment_id=payment_id,
                outcome="out_of_order",
                rationale=(
                    f"{event_type} arrived with no prior payment.failed episode for this "
                    "payment_id — recorded, no recovery episode created"
                ),
            )
            return IngestResult(
                event_id=event_id, event_type=event_type, dedup_result="out_of_order"
            )

        insert_webhook_event(
            self._conn,
            event_id=event_id,
            event_type=event_type,
            payment_id=payment_id,
            plink_id=fields.plink_id,
            order_id=fields.order_id,
            amount_paise=fields.amount_paise,
            raw_body_hash=raw_body_hash,
            signature_valid=True,
            received_at=received_at,
            processed=True,
            dedup_result="new",
        )
        self._audit.append(
            stage="gate",
            actor="system",
            episode_id=existing["episode_id"],
            payment_id=payment_id,
            outcome="terminal_event_recorded",
            rationale=f"{event_type} recorded for existing episode; attribution happens later",
        )
        return IngestResult(
            event_id=event_id,
            event_type=event_type,
            dedup_result="new",
            episode_id=existing["episode_id"],
        )
