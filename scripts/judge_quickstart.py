"""Prints the exact acceptance sequence a reviewer runs, with the expected
outcome of each step — `make judge-quickstart`. This is a printed checklist,
not an executor: it does not itself clone, install, or run anything, because
several of its own steps (clone, setup) don't make sense run recursively from
inside the repo they'd be cloning. The same table is duplicated in README.md
under "How to check this in 90 seconds" so a reviewer sees it without running
anything either.

REPO_URL is filled in once the repo has a real remote — see BUILD_LOG.md /
the commit that added this file for why it can't be guessed in advance.
"""

from __future__ import annotations

REPO_URL = "https://github.com/nirmalraj142007-byte/second-rail.git"

STEPS = [
    (f"git clone {REPO_URL} && cd second-rail",
     "repo present locally"),
    ("head -40 README.md",
     "seam, quickstart, and the no-code-moves-money heading all visible on one screen"),
    ("make setup",
     "pinned venv installs clean, no errors"),
    ("make eval",
     "evidence/report.md regenerated, under 5 minutes, no API key, no network"),
    ("cat evidence/report.md",
     "sections 1-4 in order: guardrail correctness, admissibility, cost/throughput, "
     "externally-anchored classification, then a recovery RANGE (never a point estimate)"),
    ("make verify-audit",
     "prints \"chain intact - N records\" in under 2 seconds"),
    ("cat config/guardrails.yaml",
     "every money-adjacent number, each with its own justification comment, under 60 lines"),
    ("cat docs/where-the-llm-is-not.md",
     "the closed list of decisions the LLM is refused, each with the test that enforces it"),
    ("cat BUILD_LOG.md",
     "one entry per working session from D1, including a \"Wrong turns\" index"),
    ("git log --format='%ad %s' --date=short | tail -30",
     "dated commit history; outcome_model.md's commit predates src/attribute/'s"),
]


def render() -> str:
    lines = ["Second Rail -- 90-second acceptance check", "=" * 42, ""]
    for i, (cmd, outcome) in enumerate(STEPS, start=1):
        lines.append(f"{i:2d}. {cmd}")
        lines.append(f"    -> {outcome}")
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    print(render())


if __name__ == "__main__":
    main()
