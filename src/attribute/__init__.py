"""Outcome attribution and the recovery ledger. NO LLM, ever — this package
computes whether a recovery action worked and what it was worth, both of
which are deterministic facts about webhook/API data, never a model's
opinion. See tests/test_llm_boundary.py.
"""

from __future__ import annotations
