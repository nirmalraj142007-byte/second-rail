"""PolicyEngine — the deterministic, LLM-free step between diagnosis and
action selection.

This is the mechanism behind this project's central design claim: the LLM
in src/choose/selector.py picks one action from a set it did not construct.
PolicyEngine is what constructs that set, purely as a table lookup keyed on
(cause_class, amount_band, segment, instrument) — the same key the
config/policy_table.yaml UNIQUE constraint and the `policy_rule` table's
UNIQUE constraint both enforce (CLAUDE.md's "deterministic mapping"
guarantee). Nothing in this module ever calls an LLM, imports one, or reads
a model's output — see tests/test_llm_boundary.py's grep, which does not
even need to cover src/choose/ because policy.py and selector.py are
deliberately split so the deterministic half can be read and trusted on its
own.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.config_models import INSTRUMENTS, SEGMENTS, PolicyRule, PolicyTable
from src.diagnose.classifier import Diagnosis
from src.errors import ConfigError
from src.gate.checks import Episode

_PolicyKey = tuple[str, str, str, str]  # (cause_class, amount_band, segment, instrument)


@dataclass(frozen=True)
class PolicyMatch:
    policy_rule_id: str
    admissible_actions: list[str]
    escalation_tier: str
    amount_band: str
    # Not part of the original phase spec's PolicyMatch shape — carried
    # here (rather than threaded separately into ActionSelector) so
    # ActionSelector.select()'s constructor can stay exactly
    # `__init__(self, llm, cache, settings)` as specified, with no direct
    # dependency on PolicyTable. Every PolicyMatch this engine returns
    # carries the same table-wide fallback_priority list.
    fallback_priority: list[str] = field(default_factory=list)


def resolve_amount_band(table: PolicyTable, amount_paise: int) -> str:
    for band in table.amount_bands:
        if amount_paise < band.min_paise:
            continue
        if band.max_paise is not None and amount_paise > band.max_paise:
            continue
        return band.id
    raise ConfigError(
        f"amount_paise={amount_paise} does not fall inside any amount_bands entry",
        code="AMOUNT_BAND_UNRESOLVED",
        remediation="config/policy_table.yaml: amount_bands must cover every non-negative amount",
    )


class PolicyEngine:
    def __init__(self, table: PolicyTable) -> None:
        self._table = table
        self._by_key: dict[_PolicyKey, PolicyRule] = {
            (r.cause_class, r.amount_band, r.segment, r.instrument): r for r in table.rules
        }

    @property
    def table(self) -> PolicyTable:
        return self._table

    def resolve(self, ep: Episode, diagnosis: Diagnosis) -> PolicyMatch:
        """Deterministic: the same (episode, diagnosis) pair always resolves
        to the same PolicyMatch — no clock, no randomness, no I/O beyond the
        table already loaded at construction time. See
        tests/test_choose.py's 1000-call determinism test."""
        band_id = resolve_amount_band(self._table, ep.amount_paise)
        key = (diagnosis.class_id, band_id, ep.segment, ep.instrument)
        rule = self._by_key.get(key)
        if rule is not None:
            return PolicyMatch(
                policy_rule_id=rule.policy_rule_id,
                admissible_actions=list(rule.admissible_actions),
                escalation_tier=rule.escalation_tier,
                amount_band=band_id,
                fallback_priority=list(self._table.fallback_priority),
            )

        # An unmatched (cause, band, segment, instrument) tuple must never
        # silently become "no_action" — that would hide a real policy
        # coverage gap behind a plausible-looking outcome. default_rule is
        # a required field on PolicyTable (config_models.py), so this
        # branch is unreachable via any config that has ever passed
        # load_all() — checked explicitly anyway, both because a caller
        # could hand this engine a hand-built PolicyTable that bypassed
        # that validation, and because the phase spec asks for it in
        # exactly these words.
        if self._table.default_rule is None:
            raise ConfigError(
                f"no policy rule matches {key!r} and no default_rule is configured",
                code="POLICY_COVERAGE_GAP",
                remediation="add an explicit rule or a default_rule to config/policy_table.yaml",
            )
        d = self._table.default_rule
        return PolicyMatch(
            policy_rule_id="default_rule",
            admissible_actions=list(d.admissible_actions),
            escalation_tier=d.escalation_tier,
            amount_band=band_id,
            fallback_priority=list(self._table.fallback_priority),
        )

    def is_total(self) -> tuple[bool, list[_PolicyKey]]:
        """True plus an empty list if every (cause, band, segment,
        instrument) combination this engine has actually seen a rule for
        resolves — false plus the uncovered keys otherwise.

        Scope note: this engine only ever sees config/policy_table.yaml, so
        `cause_classes` here is the set of cause_class values that already
        appear in its own explicit rules, not the full taxonomy (this
        engine never loads config/taxonomy.yaml). The totality check that
        actually matters — every taxonomy class, not just the ones with an
        explicit rule already — is scripts/config_check.py's
        check_totality(), which has both configs in hand. This method
        exists for a caller (or a test) that only has a PolicyEngine and
        wants a quick, taxonomy-independent sanity check; because
        default_rule is a required field, the result is always (True, [])
        for any config that passed load_all().
        """
        band_ids = [b.id for b in self._table.amount_bands]
        cause_classes = sorted({r.cause_class for r in self._table.rules})
        uncovered: list[_PolicyKey] = []
        for cause in cause_classes:
            for band in band_ids:
                for segment in SEGMENTS:
                    for instrument in INSTRUMENTS:
                        key = (cause, band, segment, instrument)
                        if key not in self._by_key and self._table.default_rule is None:
                            uncovered.append(key)
        return (len(uncovered) == 0, uncovered)
