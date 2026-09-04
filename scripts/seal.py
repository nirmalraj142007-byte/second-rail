"""`make seal` / `make verify-seal` — checksum-seal the sealed split.

`make seal` hashes holdout/sealed.jsonl and holdout/labels.jsonl and
writes holdout/SEAL.sha256. `make verify-seal` recomputes both hashes and
compares them against what's recorded — this is the command run on
camera, so its output is deliberately one legible line.
"""

from __future__ import annotations

import hashlib
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))
ROOT = Path(__file__).resolve().parent.parent
HOLDOUT_DIR = ROOT / "holdout"
SEALED_PATH = HOLDOUT_DIR / "sealed.jsonl"
LABELS_PATH = HOLDOUT_DIR / "labels.jsonl"
SEAL_PATH = HOLDOUT_DIR / "SEAL.sha256"

_FILES = (("sealed.jsonl", SEALED_PATH), ("labels.jsonl", LABELS_PATH))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _count_episodes(path: Path) -> int:
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())


def seal() -> int:
    missing = [name for name, path in _FILES if not path.exists()]
    if missing:
        print(f"FAIL: missing {missing} — run `make data` first", file=sys.stderr)
        return 1
    sealed_at = datetime.now(IST).isoformat()
    lines = [f"{name}  sha256:{_sha256(path)}" for name, path in _FILES]
    lines.append(f"sealed_at  {sealed_at}")
    SEAL_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"sealed — {SEAL_PATH} written ({sealed_at})")
    return 0


def verify() -> int:
    if not SEAL_PATH.exists():
        print(f"FAIL: {SEAL_PATH} not found — run `make seal` first", file=sys.stderr)
        return 1

    recorded: dict[str, str] = {}
    sealed_at = "unknown"
    for line in SEAL_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("sealed_at"):
            sealed_at = line.split(None, 1)[1]
            continue
        name, _, digest = line.partition("sha256:")
        recorded[name.strip()] = digest.strip()

    problems = []
    for name, path in _FILES:
        if not path.exists():
            problems.append(f"{name} missing")
            continue
        actual = _sha256(path)
        expected = recorded.get(name)
        if expected != actual:
            problems.append(f"{name} sha256 changed (sealed {expected}, now {actual})")

    if problems:
        print("FAIL: sealed split verification failed — " + "; ".join(problems), file=sys.stderr)
        return 1

    n_episodes = _count_episodes(SEALED_PATH)
    short_hash = recorded["sealed.jsonl"][:12]
    print(
        f"sealed split verified — {n_episodes} episodes, "
        f"sha256:{short_hash}… (sealed {sealed_at})"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv or argv[0] not in ("seal", "verify"):
        print("usage: python -m scripts.seal {seal|verify}", file=sys.stderr)
        return 2
    return seal() if argv[0] == "seal" else verify()


if __name__ == "__main__":
    raise SystemExit(main())
