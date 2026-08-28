"""Cause classification: regex baseline + LLM classifier, cache, prompts.

This is one of two packages in Second Rail an LLM is allowed to touch (the
other is src/choose/, not yet built). It is never imported by src/gate/,
src/execute/, src/attribute/, or src/audit/ — see tests/test_llm_boundary.py
and docs/where-the-llm-is-not.md.
"""

from __future__ import annotations
