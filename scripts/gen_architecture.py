"""Generates docs/architecture.png — the named architecture deliverable.

This is the editable source for the diagram. There is no separate binary
Excalidraw/draw.io file: this project already commits matplotlib-rendered
PNGs as its diagram convention (CLAUDE.md's stack list), and a script a
`git diff` can show is more reviewable than a proprietary binary format, at
the cost of not being drag-and-drop editable in a GUI. Edit the STAGES list
below and rerun `python scripts/gen_architecture.py` to regenerate the PNG —
`scripts/check_architecture.py` then asserts every `module` named here still
exists under `src/`, so the diagram cannot silently drift from the code.

Run: python scripts/gen_architecture.py
"""

from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrow, FancyBboxPatch

ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = ROOT / "docs" / "architecture.png"

DETERMINISTIC = "#1b5e7a"   # teal-blue -- rule-driven, no model in the loop
LLM = "#c96a1f"             # amber -- the two model calls in the pipeline
BG = "#ffffff"

# (label, module, kind, detail) -- one row per pipeline stage, top to bottom.
# `module` must be a real directory name under src/ (checked by
# check_architecture.py) or None for a stage with no single owning module
# (the webhook itself, and the outcome event, are inputs, not code).
STAGES = [
    ("payment.failed webhook", None, "input", "Razorpay event, HMAC-signed"),
    ("ingest", "ingest", "det", "signature verify, dedup on payment_id, normalize"),
    ("gate", "gate", "det", "7 ordered eligibility checks"),
    ("diagnose", "diagnose", "llm", "regex baseline first; unmatched tail -> LLM classifier"),
    ("choose", "choose", "mixed", "policy table (det.) admits <=3; LLM selects 1"),
    ("gate (recheck)", "gate", "det", "post-selection: caps, DND, quiet hours, idempotency"),
    ("approve", "ui", "det", "auto | human keystroke | hard refuse"),
    ("execute", "execute", "det", "idempotent Payment Link, hand-rolled backoff"),
    ("outcome listener", "attribute", "det", "payment_link.paid webhook"),
    ("attribute + ledger", "attribute", "det", "48h window, gross / fp_cost / net"),
    ("audit", "audit", "det", "append-only JSONL, hash-chained to the previous record"),
]

BOX_W, BOX_H, GAP = 4.6, 0.62, 0.36
FIG_W, FIG_H = 8.6, len(STAGES) * (BOX_H + GAP) + 1.6


def kind_color(kind: str) -> str:
    return LLM if kind in ("llm", "mixed") else DETERMINISTIC


def main() -> None:
    fig, ax = plt.subplots(figsize=(FIG_W, FIG_H))
    ax.set_xlim(0, FIG_W)
    ax.set_ylim(0, FIG_H)
    ax.axis("off")
    fig.patch.set_facecolor(BG)

    top = FIG_H - 0.6
    cx = FIG_W / 2 - 1.1
    centers = []

    for i, (label, module, kind, detail) in enumerate(STAGES):
        y = top - i * (BOX_H + GAP)
        centers.append(y)
        color = kind_color(kind)
        style = "-" if kind != "input" else "--"
        box = FancyBboxPatch(
            (cx - BOX_W / 2, y - BOX_H / 2), BOX_W, BOX_H,
            boxstyle="round,pad=0.02,rounding_size=0.08",
            linewidth=1.6, edgecolor=color,
            facecolor=(color if kind == "input" else "white"),
            alpha=1.0, linestyle=style,
        )
        ax.add_patch(box)
        text_color = "white" if kind == "input" else "#1a1a1a"
        ax.text(cx, y + 0.10, label, ha="center", va="center",
                 fontsize=11.5, fontweight="bold", color=text_color)
        ax.text(cx, y - 0.16, detail, ha="center", va="center",
                 fontsize=7.6, color=text_color, style="italic")
        if module:
            ax.text(cx + BOX_W / 2 + 0.15, y, f"src/{module}/", ha="left",
                     va="center", fontsize=8.2, color=color, fontfamily="monospace")

        if i > 0:
            y0 = centers[i - 1] - BOX_H / 2
            y1 = y + BOX_H / 2
            ax.add_patch(FancyArrow(
                cx, y0 - 0.03, 0, (y1 - y0) + 0.06 - GAP + GAP,
                width=0.012, length_includes_head=True, head_width=0.11,
                head_length=0.09, color="#666666",
            ))

    # legend -- placed low, clear of the flow column and every box's x-range
    # overlap doesn't matter there because nothing else occupies that y-band
    lx, ly = 0.3, 1.55
    ax.add_patch(FancyBboxPatch((lx, ly - 0.62), 2.55, 0.7,
                                  boxstyle="round,pad=0.02", linewidth=1,
                                  edgecolor="#999999", facecolor="#fafafa"))
    ax.add_patch(FancyBboxPatch((lx + 0.15, ly - 0.2), 0.3, 0.18,
                                  boxstyle="round,pad=0.02",
                                  edgecolor=DETERMINISTIC, facecolor="white", linewidth=1.6))
    ax.text(lx + 0.55, ly - 0.11, "deterministic — no LLM", fontsize=8, va="center")
    ax.add_patch(FancyBboxPatch((lx + 0.15, ly - 0.48), 0.3, 0.18,
                                  boxstyle="round,pad=0.02",
                                  edgecolor=LLM, facecolor="white", linewidth=1.6))
    ax.text(lx + 0.55, ly - 0.39, "LLM in the loop (2 calls max/episode)", fontsize=8, va="center")

    ax.text(FIG_W / 2, FIG_H - 0.15,
             "Second Rail — one episode, end to end", ha="center", fontsize=13, fontweight="bold")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(OUT_PATH, dpi=200, facecolor=BG)
    print(f"wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
