"""Verifies the hash chain in an audit JSONL file without ever loading the
whole file into memory — it streams one line at a time, keeping only the
running prev_hash and a one-line lookahead buffer so a truncated last line
(a crash mid-write) can be told apart from real corruption.

Two independent checks run against every record, in order:
  1. record["prev_hash"] == the hash we're tracking from the previous
     record. This is what catches reordering or a deleted/inserted line —
     the declared linkage no longer matches the true chain.
  2. record["hash"] == compute_hash(record["prev_hash"], record_without_hash).
     This is what catches content tampering — the record's own fields
     (including a tampered "hash" field itself) no longer hash to what's
     stored.
Whichever fails first is reported, naming the record's own seq number.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer

from src.audit.writer import GENESIS_PREV_HASH, canonical_json, compute_hash
from src.config import load_settings


@dataclass
class VerifyResult:
    intact: bool
    count: int
    first_bad_seq: int | None
    expected: str | None
    actual: str | None
    elapsed_s: float


def _lines_with_last_flag(f: Any):
    """Yield (line, is_last_line) without ever holding more than one line
    of lookahead in memory."""
    pending: str | None = None
    for line in f:
        if pending is not None:
            yield pending, False
        pending = line
    if pending is not None:
        yield pending, True


def verify_chain(path: Path) -> VerifyResult:
    start = time.monotonic()
    prev_hash = GENESIS_PREV_HASH
    count = 0

    with path.open("r", encoding="utf-8") as f:
        for raw_line, is_last in _lines_with_last_flag(f):
            line = raw_line.strip()
            if not line:
                continue

            try:
                record: dict[str, Any] = json.loads(line)
            except json.JSONDecodeError:
                if is_last:
                    # A crash mid-write leaves exactly this: an incomplete
                    # final line. Everything before it is still a valid,
                    # fully-committed prefix.
                    break
                return VerifyResult(
                    intact=False, count=count, first_bad_seq=None,
                    expected=None, actual=None, elapsed_s=time.monotonic() - start,
                )

            declared_prev = record.get("prev_hash")
            if declared_prev != prev_hash:
                return VerifyResult(
                    intact=False, count=count, first_bad_seq=record.get("seq"),
                    expected=prev_hash, actual=declared_prev,
                    elapsed_s=time.monotonic() - start,
                )

            stored_hash = record.get("hash")
            record_without_hash = {k: v for k, v in record.items() if k != "hash"}
            expected_hash = compute_hash(prev_hash, record_without_hash)
            if stored_hash != expected_hash:
                return VerifyResult(
                    intact=False, count=count, first_bad_seq=record.get("seq"),
                    expected=expected_hash, actual=stored_hash,
                    elapsed_s=time.monotonic() - start,
                )

            prev_hash = stored_hash
            count += 1

    return VerifyResult(
        intact=True, count=count, first_bad_seq=None,
        expected=None, actual=None, elapsed_s=time.monotonic() - start,
    )


def _audit_dir() -> Path:
    return load_settings().audit_dir


def _all_audit_files(audit_dir: Path) -> list[Path]:
    if not audit_dir.exists():
        return []
    return sorted(audit_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)


def _newest_audit_file(audit_dir: Path) -> Path | None:
    files = _all_audit_files(audit_dir)
    return files[-1] if files else None


def _short(h: str | None) -> str:
    if h is None:
        return "None"
    # Plain ASCII only — cmd.exe / a legacy Windows console on the cp1252
    # codepage raises UnicodeEncodeError on non-ASCII punctuation (reproduced
    # directly against this project's own tooling; see src/ui/theme.py's
    # matching comment). This is `make verify-audit`'s failure path, called
    # on camera, so it must survive that console too.
    return h[:11] + "..." if len(h) > 11 else h


def _print_failure(result: VerifyResult) -> None:
    typer.echo(
        f"chain BROKEN at seq {result.first_bad_seq} - "
        f"expected {_short(result.expected)} got {_short(result.actual)}"
    )


def _flip_one_byte_in_middle_record(path: Path) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    non_empty = [i for i, line in enumerate(lines) if line.strip()]
    mid = non_empty[len(non_empty) // 2]
    record = json.loads(lines[mid])
    h = record["hash"]
    flipped_char = "0" if h[-1] != "0" else "1"
    record["hash"] = h[:-1] + flipped_char
    lines[mid] = canonical_json(record)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


app = typer.Typer(add_completion=False)


@app.callback(invoke_without_command=True)
def main(
    run_id: str | None = typer.Option(
        None, "--run-id", help="Verify only evidence/audit/<run_id>.jsonl"
    ),
    all_: bool = typer.Option(
        False, "--all", help="Verify every audit file, combined into one summary"
    ),
    tamper_test: bool = typer.Option(
        False, "--tamper-test",
        help="Demonstrate the failure path on a throwaway copy of the newest audit file",
    ),
) -> None:
    audit_dir = _audit_dir()

    if tamper_test:
        original = _newest_audit_file(audit_dir)
        if original is None:
            typer.echo("no audit files found to tamper-test", err=True)
            raise typer.Exit(code=1)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp) / original.name
            shutil.copy(original, tmp_path)
            _flip_one_byte_in_middle_record(tmp_path)
            result = verify_chain(tmp_path)
        if result.intact:
            typer.echo(
                "tamper-test did not corrupt the chain — this is a bug in the tamper test, "
                "not a claim the chain is unbreakable",
                err=True,
            )
            raise typer.Exit(code=1)
        _print_failure(result)
        return

    if all_:
        files = _all_audit_files(audit_dir)
        if not files:
            typer.echo("no audit files found", err=True)
            raise typer.Exit(code=1)
        total_count = 0
        total_elapsed = 0.0
        for f in files:
            result = verify_chain(f)
            total_elapsed += result.elapsed_s
            if not result.intact:
                _print_failure(result)
                raise typer.Exit(code=1)
            total_count += result.count
        typer.echo(f"chain intact - {total_count} records ({total_elapsed:.2f}s)")
        return

    target = audit_dir / f"{run_id}.jsonl" if run_id else _newest_audit_file(audit_dir)
    if target is None or not target.exists():
        typer.echo(f"no audit file found: {target}", err=True)
        raise typer.Exit(code=1)

    result = verify_chain(target)
    if not result.intact:
        _print_failure(result)
        raise typer.Exit(code=1)
    typer.echo(f"chain intact - {result.count} records ({result.elapsed_s:.2f}s)")


if __name__ == "__main__":
    app()
