from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from src.audit.verify import verify_chain
from src.audit.writer import AuditWriter, canonical_json
from src.db.migrate import get_connection, migrate


def _make_writer(tmp_path: Path, run_id: str = "run_test") -> AuditWriter:
    db_path = tmp_path / "second_rail.db"
    migrate(db_path)
    conn = get_connection(db_path)
    return AuditWriter(run_id, tmp_path / "audit", conn)


def test_100_appended_records_verify_intact(tmp_path):
    writer = _make_writer(tmp_path)
    for i in range(100):
        writer.append(stage="gate", actor="system", rationale=f"r{i}")
    writer.close()

    result = verify_chain(tmp_path / "audit" / "run_test.jsonl")

    assert result.intact
    assert result.count == 100


def test_byte_flip_at_record_50_is_detected_at_that_seq(tmp_path):
    writer = _make_writer(tmp_path)
    for i in range(100):
        writer.append(stage="gate", actor="system", rationale=f"r{i}")
    writer.close()

    path = tmp_path / "audit" / "run_test.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[50])
    assert record["seq"] == 50
    h = record["hash"]
    record["hash"] = h[:-1] + ("0" if h[-1] != "0" else "1")
    lines[50] = canonical_json(record)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = verify_chain(path)

    assert not result.intact
    assert result.first_bad_seq == 50


def test_truncated_last_line_verifies_intact_prefix(tmp_path):
    writer = _make_writer(tmp_path)
    for i in range(10):
        writer.append(stage="gate", actor="system", rationale=f"r{i}")
    writer.close()

    path = tmp_path / "audit" / "run_test.jsonl"
    raw = path.read_bytes()
    # Chop the file off partway through the last line, simulating a crash
    # mid-write. The newline before it stays intact, so the truncation
    # lands inside the final record's JSON, not between records.
    truncated = raw[: len(raw) - 20]
    path.write_bytes(truncated)

    result = verify_chain(path)

    assert result.intact
    assert result.count == 9


def test_reordered_lines_are_detected(tmp_path):
    writer = _make_writer(tmp_path)
    for i in range(10):
        writer.append(stage="gate", actor="system", rationale=f"r{i}")
    writer.close()

    path = tmp_path / "audit" / "run_test.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[4], lines[5] = lines[5], lines[4]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = verify_chain(path)

    assert not result.intact


@pytest.mark.slow
def test_2000_records_verify_in_under_two_seconds(tmp_path):
    # The 2.0s budget below is verify_chain()'s own elapsed_s, not this
    # test's wall time — writing 2000 records first (each AuditWriter.append()
    # fsyncs its line, real disk I/O by design, see writer.py's module
    # docstring) is what makes this test itself take several seconds.
    writer = _make_writer(tmp_path)
    for i in range(2000):
        writer.append(stage="gate", actor="system", rationale=f"r{i}")
    writer.close()

    result = verify_chain(tmp_path / "audit" / "run_test.jsonl")

    assert result.intact
    assert result.count == 2000
    assert result.elapsed_s < 2.0


def test_only_writer_opens_audit_files_for_writing():
    src_root = Path(__file__).resolve().parent.parent / "src"
    writer_path = src_root / "audit" / "writer.py"
    append_mode_pattern = re.compile(r"""open\([^)]*["']a["']""")

    offenders = []
    for path in src_root.rglob("*.py"):
        if path == writer_path:
            continue
        text = path.read_text(encoding="utf-8")
        if append_mode_pattern.search(text):
            offenders.append(str(path.relative_to(src_root)))

    assert offenders == []
