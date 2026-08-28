"""RegexBaseline — the deterministic cause classifier.

Not a fallback bolted on in front of the LLM: this is a real competing
classifier, evaluated head-to-head against it in scripts/classify.py. On
this project's synthetic train/sealed split it wins outright — every
episode's `error_reason` is copied verbatim from the same anchor token
config/taxonomy.yaml's `regex_patterns` were authored from (see that file's
header comment), so a pattern written for a class matches that class's own
episodes by construction. That is disclosed, not hidden, in
scripts/classify.py's output: it is a property of the synthetic generator,
not evidence the regex would hold up against real, unseen Razorpay traffic.
Against the raw harvested strings in evidence/harvested_errors.jsonl — 19 of
20 of which collapse to a generic "Payment failed" / "payment_failed" the
regex was never written to match — the same baseline resolves almost
nothing. Both numbers are real and both are reported.

A pattern list is per-class, and a class is only reported as this episode's
diagnosis when it is the *unique* class matched anywhere in the episode's
`error_reason` or `error_description`. Zero classes matched, or more than
one, both fall through to `None` — the "unmatched tail" the LLM exists for.
Collapsing an ambiguous multi-class match into "unmatched" rather than
guessing is a deliberate choice: a regex baseline that silently picks
whichever class it checked first on a tie is worse than one that admits it
doesn't know.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from src.config_models import Taxonomy
from src.gate.checks import Episode

_SEARCH_FIELDS: tuple[str, ...] = ("error_reason", "error_description")


@dataclass(frozen=True)
class BaselineResult:
    class_id: str
    matched_field: str
    matched_pattern: str


class RegexBaseline:
    def __init__(self, taxonomy: Taxonomy) -> None:
        self._compiled: list[tuple[str, re.Pattern[str]]] = [
            (cls.class_id, re.compile(pattern))
            for cls in taxonomy.classes
            for pattern in cls.regex_patterns
        ]

    def classify(self, ep: Episode) -> BaselineResult | None:
        hits: list[BaselineResult] = []
        matched_classes: set[str] = set()

        for field_name in _SEARCH_FIELDS:
            value = getattr(ep, field_name, None)
            if not value:
                continue
            for class_id, pattern in self._compiled:
                if class_id in matched_classes:
                    continue  # this class already has a hit from an earlier field
                match = pattern.search(value)
                if match:
                    hits.append(BaselineResult(class_id, field_name, pattern.pattern))
                    matched_classes.add(class_id)

        if len(matched_classes) != 1:
            return None
        return hits[0]
