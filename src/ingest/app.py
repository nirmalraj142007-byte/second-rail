"""FastAPI webhook receiver.

`POST /webhooks/razorpay` verifies the signature synchronously (a bad
signature must write nothing and return fast) and then does the absolute
minimum work before returning 200: parse the envelope enough to find
`event` and the `X-Razorpay-Event-Id` header, and hand the rest to a
background worker thread over an in-process queue. That is what keeps this
endpoint under the 50ms target — dedup, normalization, and the DB writes in
src/ingest/service.py all happen off the request path.
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
    raw_body = await request.body()
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
