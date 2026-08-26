from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import holdout_guard
from src.errors import HoldoutLeakageError


def test_open_labels_succeeds_when_called_from_outside_src():
    records = holdout_guard.open_labels()
    assert len(records) == 200
    assert all("episode_id" in r and "cause_class" in r for r in records)


def test_caller_is_inside_src_detects_a_frame_under_src():
    src_frame = SimpleNamespace(
        filename=str(holdout_guard.ROOT / "src" / "diagnose" / "classifier.py")
    )
    non_src_frame = SimpleNamespace(filename=str(holdout_guard.ROOT / "scripts" / "eval.py"))

    assert holdout_guard._caller_is_inside_src([src_frame]) is True
    assert holdout_guard._caller_is_inside_src([non_src_frame]) is False
    assert holdout_guard._caller_is_inside_src([non_src_frame, src_frame]) is True


def test_open_labels_raises_when_a_src_frame_is_on_the_stack(monkeypatch):
    # open_labels() -> _caller_is_inside_src() -> inspect.stack()[1:], so the
    # mock needs a throwaway frame 0 (representing _caller_is_inside_src's
    # own frame) ahead of the src/ frame under test.
    own_frame = SimpleNamespace(filename=str(holdout_guard.ROOT / "scripts" / "holdout_guard.py"))
    fake_frame = SimpleNamespace(filename=str(holdout_guard.ROOT / "src" / "choose" / "engine.py"))
    monkeypatch.setattr(holdout_guard.inspect, "stack", lambda: [own_frame, fake_frame])

    with pytest.raises(HoldoutLeakageError):
        holdout_guard.open_labels()


def test_open_labels_raises_a_clear_error_when_the_file_is_missing(tmp_path: Path):
    missing = tmp_path / "does_not_exist.jsonl"
    with pytest.raises(FileNotFoundError):
        holdout_guard.open_labels(missing)


def test_no_file_under_src_mentions_labels_jsonl_literally():
    """Defense in depth alongside the runtime guard, same static-grep
    pattern the project already uses for the LLM boundary."""
    src_root = Path(__file__).resolve().parent.parent / "src"
    offenders = []
    for path in src_root.rglob("*.py"):
        if "labels.jsonl" in path.read_text(encoding="utf-8"):
            offenders.append(str(path))
    assert not offenders, f"src/ file(s) reference labels.jsonl directly: {offenders}"
