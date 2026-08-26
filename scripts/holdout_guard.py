"""Runtime guard against src/ ever reading the sealed split's ground truth.

holdout/labels.jsonl carries the true cause_class and simulated customer
response for every sealed episode (data/generator.py, Phase 5). If
anything under src/ read it, the diagnose/choose stages could see the
answer key before running — silently invalidating the whole sealed-split
evaluation, with no visible symptom until a judge asks how the number was
produced. `open_labels()` is the only sanctioned way to read the file: it
inspects the call stack and refuses to run if any frame originates from
inside src/. Only scripts/eval.py (Phase 13) is expected to call it.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any

from src.errors import HoldoutLeakageError

ROOT = Path(__file__).resolve().parent.parent
LABELS_PATH = ROOT / "holdout" / "labels.jsonl"
_SRC_ROOT = (ROOT / "src").resolve()


def _frame_is_inside_src(filename: str) -> bool:
    try:
        frame_path = Path(filename).resolve()
    except OSError:
        return False
    return frame_path == _SRC_ROOT or _SRC_ROOT in frame_path.parents


def _caller_is_inside_src(frames: list[Any] | None = None) -> bool:
    """frames defaults to the real call stack (minus this function's own
    frame); a test passes a fake list of objects exposing `.filename`."""
    if frames is None:
        frames = inspect.stack()[1:]
    return any(_frame_is_inside_src(getattr(f, "filename", "")) for f in frames)


def open_labels(path: Path = LABELS_PATH) -> list[dict[str, Any]]:
    if _caller_is_inside_src():
        raise HoldoutLeakageError(
            f"a caller inside src/ attempted to read {path}",
            remediation="only scripts/eval.py may open the sealed split's ground truth",
        )
    if not path.exists():
        raise FileNotFoundError(f"{path} not found — run `make data && make seal` first")
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            records.append(json.loads(line))
    return records
