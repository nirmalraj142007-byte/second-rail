"""Asserts every module the architecture diagram names actually exists under
src/ — a diagram that has drifted from the code is worse than none.

Reads STAGES directly out of scripts/gen_architecture.py (the diagram's
source of truth) rather than re-typing the module list here, so there is
exactly one place this can go stale.

Run: python scripts/check_architecture.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from gen_architecture import STAGES  # noqa: E402


def main() -> int:
    missing: list[str] = []
    checked: list[str] = []

    for label, module, _kind, _detail in STAGES:
        if module is None:
            continue
        path = ROOT / "src" / module
        checked.append(module)
        if not path.is_dir():
            missing.append(f'"{label}" names src/{module}/, which does not exist')

    png = ROOT / "docs" / "architecture.png"
    if not png.exists():
        missing.append("docs/architecture.png is not committed")

    if missing:
        print("check_architecture: FAILED")
        for m in missing:
            print(f"  - {m}")
        return 1

    print(f"check_architecture: OK — {len(set(checked))} module(s) named in the "
          f"diagram all exist under src/, docs/architecture.png present.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
