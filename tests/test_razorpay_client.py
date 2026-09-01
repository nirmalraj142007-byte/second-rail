from __future__ import annotations

import json

import httpx
import pytest

from src.errors import ExecutorError
from src.razorpay_client import RazorpayClient, _TokenBucket


def _client(handler) -> RazorpayClient:
    return RazorpayClient(
        key_id="rzp_test_dummy",
        key_secret="dummy_secret",
        transport=httpx.MockTransport(handler),
    )


def test_create_order_returns_parsed_json_on_success():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/orders"
        body = json.loads(request.content)
        assert body == {"amount": 100, "currency": "INR", "receipt": "r1", "notes": {}}
        return httpx.Response(200, json={"id": "order_ABC123", "status": "created"})

    order = _client(handler).create_order(amount_paise=100, receipt="r1", notes={})

    assert order["id"] == "order_ABC123"


def test_non_2xx_raises_executor_error_with_status_body_and_request_id():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": {"code": "BAD_REQUEST_ERROR", "description": "bad"}},
            headers={"x-razorpay-request-id": "req_xyz"},
        )

    with pytest.raises(ExecutorError) as exc_info:
        _client(handler).fetch_payment("pay_doesnotexist")

    err = exc_info.value
    assert err.status_code == 400
    assert err.response_body["error"]["code"] == "BAD_REQUEST_ERROR"
    assert err.request_id == "req_xyz"


def test_retryable_status_eventually_succeeds(monkeypatch):
    monkeypatch.setattr("src.razorpay_client.time.sleep", lambda _seconds: None)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, json={"error": {"description": "slow down"}})
        return httpx.Response(200, json={"id": "pay_ok", "status": "captured"})

    payment = _client(handler).fetch_payment("pay_retry")

    assert payment["id"] == "pay_ok"
    assert calls["n"] == 3


def test_retryable_status_exhausts_attempts_and_raises(monkeypatch):
    monkeypatch.setattr("src.razorpay_client.time.sleep", lambda _seconds: None)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, json={"error": {"description": "down"}})

    with pytest.raises(ExecutorError) as exc_info:
        _client(handler).fetch_payment("pay_down")

    assert calls["n"] == 4
    assert exc_info.value.status_code == 503
    assert len(exc_info.value.attempts) == 4


def test_fetch_order_payments_extracts_items_list():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/orders/order_1/payments"
        body = {"entity": "collection", "count": 1, "items": [{"id": "pay_1"}]}
        return httpx.Response(200, json=body)

    payments = _client(handler).fetch_order_payments("order_1")

    assert payments == [{"id": "pay_1"}]


def test_create_payment_link_merges_reference_id_into_payload():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["reference_id"] == "ref-1"
        assert body["amount"] == 100
        return httpx.Response(200, json={"id": "plink_1", "short_url": "https://rzp.io/i/x"})

    payload = {"amount": 100, "currency": "INR"}
    link = _client(handler).create_payment_link(payload, reference_id="ref-1")

    assert link["id"] == "plink_1"


def test_cancel_payment_link_hits_cancel_path():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/payment_links/plink_1/cancel"
        assert request.method == "POST"
        return httpx.Response(200, json={"id": "plink_1", "status": "cancelled"})

    result = _client(handler).cancel_payment_link("plink_1")

    assert result["status"] == "cancelled"


def test_list_payment_links_extracts_payment_links_key():
    """Regression test: the real Razorpay list-payment-links response key
    is `payment_links`, not `items` — confirmed against a real live call
    (see BUILD_LOG.md). Before this fix, list_payment_links() read
    result.get("items", []), which is correct for fetch_order_payments()'s
    endpoint but wrong for this one, so it silently returned [] on every
    real call regardless of how many links actually existed —
    guardrail_proof.py's "duplicate links created" figure was never
    actually checked against the real API as a result."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/payment_links"
        body = {
            "entity": "collection",
            "count": 2,
            "payment_links": [
                {"id": "plink_1", "notes": {"run_id": "run_a"}},
                {"id": "plink_2", "notes": {"run_id": "run_b"}},
            ],
        }
        return httpx.Response(200, json=body)

    links = _client(handler).list_payment_links(count=100, skip=0)

    assert links == [
        {"id": "plink_1", "notes": {"run_id": "run_a"}},
        {"id": "plink_2", "notes": {"run_id": "run_b"}},
    ]


def test_fetch_payment_link_hits_get_path():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/payment_links/plink_1"
        assert request.method == "GET"
        return httpx.Response(200, json={"id": "plink_1", "status": "created", "payments": []})

    link = _client(handler).fetch_payment_link("plink_1")

    assert link["status"] == "created"


def test_token_bucket_spaces_out_acquisitions(monkeypatch):
    now = {"t": 0.0}
    slept: list[float] = []

    def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        now["t"] += seconds

    monkeypatch.setattr("src.razorpay_client.time.monotonic", lambda: now["t"])
    monkeypatch.setattr("src.razorpay_client.time.sleep", fake_sleep)

    bucket = _TokenBucket(rate=2.0)
    bucket.acquire()
    bucket.acquire()
    bucket.acquire()

    assert slept == [0.5, 0.5]
