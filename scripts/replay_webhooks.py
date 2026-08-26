"""`make replay-webhooks` — POSTs every fixtures/webhooks/*.json file to a
running Second Rail ingest server with a correctly computed HMAC signature.

This is the Phase 9 demo's backup failure scenario (duplicate-webhook replay
-> idempotent no-op, see BUILD_LOG.md) and the offline path the no-key eval
harness exercises without a live tunnel. Each fixture gets a stable
X-Razorpay-Event-Id derived from its filename, so re-running this command
against a database that already has these events is itself a legitimate
dedup exercise, not a bug — expect "duplicate" dedup_results on a second run.
"""

from __future__ import annotations

import hashlib
import hmac
import sys
from pathlib import Path

import httpx

from src.config import load_settings

ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = ROOT / "fixtures" / "webhooks"
DEFAULT_URL = "http://localhost:8000/webhooks/razorpay"


def _event_id_for(fixture_path: Path) -> str:
    digest = hashlib.sha256(fixture_path.stem.encode("utf-8")).hexdigest()[:20]
    return f"evt_fixture_{digest}"


def _sign(raw_body: bytes, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()


def replay(url: str = DEFAULT_URL) -> int:
    settings = load_settings()
    secret = settings.razorpay_webhook_secret
    if not secret:
        print("RAZORPAY_WEBHOOK_SECRET is not set — cannot sign replayed webhooks", file=sys.stderr)
        return 1

    fixture_paths = sorted(FIXTURES_DIR.glob("*.json"))
    if not fixture_paths:
        print(f"no fixtures found under {FIXTURES_DIR}", file=sys.stderr)
        return 1

    exit_code = 0
    with httpx.Client(timeout=10.0) as client:
        for path in fixture_paths:
            raw_body = path.read_bytes()
            headers = {
                "Content-Type": "application/json",
                "X-Razorpay-Signature": _sign(raw_body, secret),
                "X-Razorpay-Event-Id": _event_id_for(path),
            }
            try:
                response = client.post(url, content=raw_body, headers=headers)
            except httpx.TransportError as exc:
                print(f"{path.name}: transport error — {exc}", file=sys.stderr)
                exit_code = 1
                continue
            print(f"{path.name}: HTTP {response.status_code} {response.text}")
            if response.status_code != 200:
                exit_code = 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(replay())
