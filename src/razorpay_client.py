"""Thin Razorpay REST client — raw httpx, not the SDK.

The audit trail (added in a later phase) needs the raw HTTP status code, the
raw response body, and the `x-razorpay-request-id` header in hand for every
call. The razorpay SDK swallows all three behind its own exception types, so
every call in this codebase goes through httpx directly against
https://api.razorpay.com/v1/. The SDK stays in requirements.txt for
reference only — no code path here imports it.

Razorpay does not publish a universal per-second rate limit across its API
(only documented, per-endpoint limits exist for a handful of routes). 2
requests/second is a self-imposed conservative bound chosen for this
project, not a documented Razorpay limit — it exists so a harvest run or a
batch run cannot trip whatever undocumented limit actually governs the
account.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import httpx

from src.errors import ExecutorError

RAZORPAY_BASE_URL = "https://api.razorpay.com/v1"

_MAX_ATTEMPTS = 4
_RETRY_STATUS_CODES = {429, 500, 502, 503, 504}
_SELF_IMPOSED_RATE_PER_SECOND = 2.0


class _TokenBucket:
    """Blocks the caller so wrapped calls never exceed `rate` per second."""

    def __init__(self, rate: float) -> None:
        self._interval = 1.0 / rate
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait = self._next_allowed - now
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._next_allowed = max(now, self._next_allowed) + self._interval


def _safe_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text


class RazorpayClient:
    def __init__(
        self,
        key_id: str,
        key_secret: str,
        timeout: float = 15.0,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client = httpx.Client(
            base_url=RAZORPAY_BASE_URL,
            auth=(key_id, key_secret),
            timeout=timeout,
            transport=transport,
        )
        self._bucket = _TokenBucket(rate=_SELF_IMPOSED_RATE_PER_SECOND)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> RazorpayClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _request(
        self, method: str, path: str, *, json: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        attempts: list[dict[str, Any]] = []

        for attempt in range(1, _MAX_ATTEMPTS + 1):
            self._bucket.acquire()
            try:
                response = self._client.request(method, path, json=json)
            except httpx.TransportError as exc:
                attempts.append({"attempt": attempt, "transport_error": str(exc)})
                if attempt == _MAX_ATTEMPTS:
                    err = ExecutorError(
                        f"Razorpay {method} {path} failed after {attempt} attempts: {exc}",
                        remediation="check network connectivity to api.razorpay.com",
                        code="RAZORPAY_TRANSPORT_ERROR",
                    )
                    err.attempts = attempts  # type: ignore[attr-defined]
                    raise err from exc
                time.sleep(2 ** (attempt - 1))
                continue

            if response.status_code < 300:
                return response.json() if response.content else {}

            request_id = response.headers.get("x-razorpay-request-id")
            retryable = response.status_code in _RETRY_STATUS_CODES
            attempts.append(
                {"attempt": attempt, "status_code": response.status_code, "request_id": request_id}
            )

            if retryable and attempt < _MAX_ATTEMPTS:
                time.sleep(2 ** (attempt - 1))
                continue

            err = ExecutorError(
                f"Razorpay {method} {path} returned HTTP {response.status_code}",
                remediation="inspect response_body for the Razorpay error object",
                code=f"RAZORPAY_HTTP_{response.status_code}",
            )
            err.status_code = response.status_code  # type: ignore[attr-defined]
            err.response_body = _safe_json(response)  # type: ignore[attr-defined]
            err.request_id = request_id  # type: ignore[attr-defined]
            err.attempts = attempts  # type: ignore[attr-defined]
            raise err

        raise AssertionError("unreachable: retry loop always returns or raises")

    def create_order(
        self, amount_paise: int, receipt: str, notes: dict[str, str]
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/orders",
            json={"amount": amount_paise, "currency": "INR", "receipt": receipt, "notes": notes},
        )

    def fetch_payment(self, payment_id: str) -> dict[str, Any]:
        return self._request("GET", f"/payments/{payment_id}")

    def fetch_order_payments(self, order_id: str) -> list[dict[str, Any]]:
        result = self._request("GET", f"/orders/{order_id}/payments")
        items = result.get("items", [])
        return list(items)

    def create_payment_link(self, payload: dict[str, Any], reference_id: str) -> dict[str, Any]:
        body = {**payload, "reference_id": reference_id}
        return self._request("POST", "/payment_links", json=body)

    def cancel_payment_link(self, plink_id: str) -> dict[str, Any]:
        return self._request("POST", f"/payment_links/{plink_id}/cancel")
