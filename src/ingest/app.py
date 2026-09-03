"""FastAPI webhook receiver.

`POST /webhooks/razorpay` verifies the signature synchronously (a bad
signature must write nothing and return fast) and then does the absolute
minimum work before returning 200: parse the envelope enough to find
`event` and the `X-Razorpay-Event-Id` header, and hand the rest to a
background worker thread over an in-process queue. That is what keeps this
endpoint under the 50ms target — dedup, normalization, and the DB writes in
src/ingest/service.py all happen off the request path.

This is the one public network surface this project has, and it is meant to
be reachable only through `make tunnel`'s cloudflared quick tunnel during a
demo — not bound to a public interface directly. Hardening on the request
path, in the order applied: reject a body over `MAX_BODY_BYTES` with 413
(checked against `Content-Length` before the body is read, and again
against the actual byte count in case that header lied or was absent);
reject a non-`application/json` `Content-Type` with 400; verify the HMAC
signature in constant time (`hmac.compare_digest` in
src/ingest/signature.py); require `X-Razorpay-Event-Id`; parse JSON.
Nothing from the request body is ever reflected back in a response or a log
line — only its sha256 hash and fields already extracted for routing
(`event`, `payment_id`) appear in either. `make serve` additionally passes
uvicorn's `--no-server-header` so responses don't advertise the exact
uvicorn build running behind the tunnel.
"""

from __future__ import annotations

import hashlib
import json
import queue
import threading
from contextlib import asynccontextmanager
from sqlite3 import Connection
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from src.audit.writer import AuditWriter
from src.config import Settings, load_settings, require_webhook_secret
from src.config_models import config_hash, load_all
from src.db.migrate import get_connection, migrate
from src.errors import SignatureError
from src.ingest.service import IngestService
from src.ingest.signature import verify_signature
from src.logging_setup import get_logger, setup_logging

_logger = get_logger("ingest.app", stage="ingest")

_SHUTDOWN = object()

# Every real Razorpay payment.failed payload is a few KB; 256KB is a wide
# margin over that, not a tuned production limit — it exists so a client
# can't force this endpoint to buffer an unbounded body in memory before
# signature verification ever runs.
MAX_BODY_BYTES = 256 * 1024


def _worker(settings: Settings, work_queue: queue.Queue[dict[str, Any] | object]) -> None:
    # sqlite3 connections may only be used from the thread that created them
    # (check_same_thread defaults to True, and this codebase does not turn
    # that off) — so the worker owns its own Connection, AuditWriter, and
    # IngestService end to end, entirely separate from the main thread's
    # /health connection.
    conn = get_connection(settings.db_path)
    audit = AuditWriter(None, settings.audit_dir, conn)
    service = IngestService(conn, audit, settings)
    try:
        while True:
            item = work_queue.get()
            if item is _SHUTDOWN:
                work_queue.task_done()
                return
            assert isinstance(item, dict)
            try:
                service.handle_event(**item)
            except Exception:
                _logger.error(
                    "unhandled exception processing webhook event_id=%s",
                    item.get("event_id"),
                    exc_info=True,
                )
            finally:
                work_queue.task_done()
    finally:
        audit.close()
        conn.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    settings = load_settings()
    # refuse to start: a webhook receiver with no secret can't verify anything
    require_webhook_secret(settings)

    migrate(settings.db_path)
    # This connection belongs to the main/event-loop thread only, and is used
    # solely by GET /health below — the worker thread below owns a completely
    # separate connection of its own.
    conn = get_connection(settings.db_path)
    bundle = load_all()
    _logger.info("starting ingest server, config_hash=%s", config_hash(bundle))

    # At 10k episodes/day this in-process queue.Queue is the first thing that
    # breaks: it holds events only in memory (a process crash between enqueue
    # and processing loses them, relying entirely on Razorpay's own webhook
    # retry to resend), and it has exactly one consumer thread. It should
    # become a durable, multi-consumer queue (Redis Streams / SQS / Rabbit)
    # with at-least-once delivery. This sentence is load-bearing for the
    # scaling-failure claim in the README.
    work_queue: queue.Queue[dict[str, Any] | object] = queue.Queue()

    app.state.settings = settings
    app.state.conn = conn
    app.state.queue = work_queue

    worker = threading.Thread(target=_worker, args=(settings, work_queue), daemon=True)
    worker.start()
    app.state.worker = worker

    try:
        yield
    finally:
        work_queue.put(_SHUTDOWN)
        work_queue.join()
        conn.close()


app = FastAPI(lifespan=lifespan, title="Second Rail ingest")


@app.get("/health")
async def health() -> dict[str, Any]:
    conn: Connection = app.state.conn
    try:
        conn.execute("SELECT 1")
        db_status = "ok"
    except Exception:
        db_status = "error"
    return {"status": "ok", "run_id": None, "db": db_status}


@app.post("/webhooks/razorpay")
async def receive_webhook(request: Request) -> JSONResponse:
    content_length = request.headers.get("content-length")
    if content_length is not None and content_length.isdigit() and int(content_length) > MAX_BODY_BYTES:
        return JSONResponse(status_code=413, content={"error": "payload_too_large"})

    content_type = request.headers.get("content-type", "")
    if content_type.split(";", 1)[0].strip().lower() != "application/json":
        return JSONResponse(status_code=400, content={"error": "unsupported_content_type"})

    raw_body = await request.body()
    # Content-Length can lie or be absent (chunked transfer) -- the actual
    # byte count read is the check that actually matters.
    if len(raw_body) > MAX_BODY_BYTES:
        return JSONResponse(status_code=413, content={"error": "payload_too_large"})

    settings: Settings = app.state.settings
    header_signature = request.headers.get("x-razorpay-signature", "")

    try:
        verify_signature(raw_body, header_signature, settings.razorpay_webhook_secret or "")
    except SignatureError as exc:
        _logger.warning("rejecting webhook: %s", exc)
        return JSONResponse(status_code=400, content={"error": "signature_invalid"})

    event_id = request.headers.get("x-razorpay-event-id")
    if not event_id:
        return JSONResponse(status_code=400, content={"error": "missing_event_id_header"})

    try:
        body = json.loads(raw_body)
    except json.JSONDecodeError:
        return JSONResponse(status_code=400, content={"error": "invalid_json"})

    event_type = body.get("event", "")
    raw_body_hash = hashlib.sha256(raw_body).hexdigest()

    work_queue: queue.Queue[dict[str, Any]] = app.state.queue
    work_queue.put(
        {
            "event_id": event_id,
            "event_type": event_type,
            "payload": body,
            "raw_body_hash": raw_body_hash,
        }
    )

    return JSONResponse(status_code=200, content={"status": "queued"})


def drain(app_: FastAPI = app) -> None:
    """Block until every currently-enqueued event has been processed. Used
    by tests and by scripts/replay_webhooks.py, which otherwise cannot know
    when the background worker has caught up with what they just posted."""
    app_.state.queue.join()
