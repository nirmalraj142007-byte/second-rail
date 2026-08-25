"""Structured JSON logging to stderr.

No print() anywhere in src/ outside CLI presentation modules — every record
goes through this formatter so it carries ts, level, stage, run_id, and
episode_id consistently, and so a judge can pipe stderr through `jq` and get
something real.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

IST = timezone(timedelta(hours=5, minutes=30))


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(IST).isoformat(timespec="seconds"),
            "level": record.levelname,
            "stage": getattr(record, "stage", "unknown"),
            "run_id": getattr(record, "run_id", None),
            "episode_id": getattr(record, "episode_id", None),
            "msg": record.getMessage(),
        }
        code = getattr(record, "code", None)
        if code is not None:
            payload["code"] = code
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


class RunContextAdapter(logging.LoggerAdapter):
    """Injects run_id / episode_id / stage into every record from this logger."""

    def process(self, msg: str, kwargs: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        extra = kwargs.setdefault("extra", {})
        extra.setdefault("run_id", self.extra.get("run_id") if self.extra else None)
        extra.setdefault("episode_id", self.extra.get("episode_id") if self.extra else None)
        extra.setdefault("stage", (self.extra or {}).get("stage", "unknown"))
        return msg, kwargs


def setup_logging(level: str = "INFO", run_id: str | None = None) -> None:
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(JSONFormatter())
    root.addHandler(handler)
    if run_id is not None:
        root = get_logger("root", run_id=run_id)
        root.debug("logging initialized")


def get_logger(
    name: str,
    *,
    run_id: str | None = None,
    episode_id: str | None = None,
    stage: str = "unknown",
) -> RunContextAdapter:
    return RunContextAdapter(
        logging.getLogger(name),
        {"run_id": run_id, "episode_id": episode_id, "stage": stage},
    )
