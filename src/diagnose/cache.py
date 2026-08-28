"""DiskCache — the content-addressed LLM response cache.

Committed to git (a later phase adds the populated cache/*.json files
themselves) so `make eval` and `make classify` never need a live API key:
every prompt this project's committed data can produce was already run once
against a real provider, and a cache hit replays that exact response.

Key stability is the whole point, so the key is deliberately narrow:
sha256(model + ":" + prompt) — nothing else goes into it. No timestamp, no
absolute path, no run_id. Two processes on two different machines computing
the same (model, prompt) pair must get the same key, and the key must not
change just because the cache directory moved.

The prompt itself is never sensitive here — every field it can contain
(error_code, error_description, error_reason, amount_band, instrument,
segment) comes from data/generator.py's synthetic set or
evidence/harvested_errors.jsonl's synthetic-payment test-mode harvest, never
real customer data (see CLAUDE.md: no real PII, ever). That is exactly why
this cache is safe to commit to git — a cache keyed on real customer text
would not be.
"""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Any


class DiskCache:
    def __init__(self, cache_dir: Path) -> None:
        self._dir = cache_dir
        self._dir.mkdir(parents=True, exist_ok=True)

    def key(self, model: str, prompt: str) -> str:
        return sha256(f"{model}:{prompt}".encode()).hexdigest()

    def _path(self, key: str) -> Path:
        return self._dir / f"{key}.json"

    def get(self, key: str) -> dict[str, Any] | None:
        path = self._path(key)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def put(self, key: str, payload: dict[str, Any]) -> None:
        path = self._path(key)
        path.write_text(
            json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
