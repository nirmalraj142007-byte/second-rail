"""matplotlib -> evidence/charts/*.png, committed. No interactive backend,
no charting library beyond what requirements.txt already pins
(matplotlib==3.9.2) — the `Agg` backend is selected explicitly so this
module never tries to open a window on a headless CI-less machine.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402  (backend must be set first)

from src.report.render import ClassMetric, ExceptionRow, RecoveryFigure  # noqa: E402

CHARTS_DIR = Path("evidence/charts")

_BLUE = "#4C72B0"
_GREEN = "#55A868"
_RED = "#C44E52"
_PURPLE = "#8172B2"


def _save(fig: plt.Figure, name: str, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / name
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return path


def plot_outcome_distribution(by_outcome: dict[str, int], out_dir: Path = CHARTS_DIR) -> Path:
    labels = list(by_outcome.keys()) or ["(none)"]
    values = [by_outcome.get(k, 0) for k in labels]
    fig, ax = plt.subplots(figsize=(6, 4))
    ax.bar(labels, values, color=_BLUE)
    for i, v in enumerate(values):
        ax.text(i, v, str(v), ha="center", va="bottom")
    ax.set_title("Episode outcomes — Second Rail sealed run")
    ax.set_ylabel("episodes")
    fig.autofmt_xdate(rotation=25)
    return _save(fig, "outcome_distribution.png", out_dir)


def plot_per_class_f1(per_class: list[ClassMetric], out_dir: Path = CHARTS_DIR) -> Path:
    ids = [m.class_id for m in per_class] or ["(none)"]
    f1s = [m.f1 for m in per_class] or [0.0]
    fig, ax = plt.subplots(figsize=(7, 4))
    ax.bar(ids, f1s, color=_GREEN)
    ax.set_ylim(0, 1.05)
    ax.set_title("Per-class F1 — self-graded against generator truth, sealed split")
    ax.set_ylabel("F1")
    return _save(fig, "per_class_f1.png", out_dir)


def plot_recovery_vs_baseline(
    second_rail: RecoveryFigure, baseline: RecoveryFigure, out_dir: Path = CHARTS_DIR
) -> Path:
    labels = [second_rail.label, baseline.label]
    bases = [second_rail.net_base_paise / 100, baseline.net_base_paise / 100]
    lowers = [
        max(0.0, (second_rail.net_base_paise - second_rail.net_low_paise) / 100),
        max(0.0, (baseline.net_base_paise - baseline.net_low_paise) / 100),
    ]
    uppers = [
        max(0.0, (second_rail.net_high_paise - second_rail.net_base_paise) / 100),
        max(0.0, (baseline.net_high_paise - baseline.net_base_paise) / 100),
    ]
    fig, ax = plt.subplots(figsize=(6, 4))
    ax.bar(labels, bases, yerr=[lowers, uppers], capsize=8, color=[_BLUE, _RED])
    ax.axhline(0, color="black", linewidth=0.8)
    ax.set_ylabel("net recovery (Rs, illustrative)")
    ax.set_title("Net recovery range — Second Rail vs FIXED_RETRY_AT_T30")
    return _save(fig, "recovery_vs_baseline.png", out_dir)


def plot_exception_reasons(rows: list[ExceptionRow], out_dir: Path = CHARTS_DIR) -> Path:
    labels = [r.reason_code for r in rows] or ["(none)"]
    counts = [r.count for r in rows] or [0]
    fig, ax = plt.subplots(figsize=(7, 4))
    ax.barh(labels, counts, color=_PURPLE)
    ax.set_xlabel("count")
    ax.set_title("Exceptions by reason_code")
    return _save(fig, "exception_reasons.png", out_dir)


def render_all(
    *,
    by_outcome: dict[str, int],
    per_class: list[ClassMetric],
    second_rail: RecoveryFigure,
    baseline: RecoveryFigure,
    exception_rows: list[ExceptionRow],
    out_dir: Path = CHARTS_DIR,
) -> list[Path]:
    return [
        plot_outcome_distribution(by_outcome, out_dir),
        plot_per_class_f1(per_class, out_dir),
        plot_recovery_vs_baseline(second_rail, baseline, out_dir),
        plot_exception_reasons(exception_rows, out_dir),
    ]
