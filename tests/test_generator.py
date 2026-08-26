from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from data.generator import (
    DEFAULT_SEED,
    EDGE_CASE_NAMES,
    OUTAGE_CLUSTER_SIZE,
    SEALED_ONLY_ISSUER_FAMILY,
    GeneratedSet,
    generate,
)

# r'\+?\d{10}' matches a bare 10-digit run, which also matches inside a
# sha256 hex digest by pure chance (10 consecutive digit characters among
# 64 hex characters is common) — so contact_hash/email_hash are excluded
# below and checked separately for hash *shape* instead. This is a
# stricter test than a blind grep: it still fails on a real phone number
# in the description/name/error fields, but doesn't false-positive on the
# very hashes the design uses to avoid storing one.
PHONE_RE = re.compile(r"\+?\d{10}")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
HASH_FIELDS = {"contact_hash", "email_hash"}


def _canonical_bytes(records: list[dict]) -> bytes:
    lines = [json.dumps(r, ensure_ascii=True, sort_keys=True) for r in records]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _sha256_of(records: list[dict]) -> str:
    return hashlib.sha256(_canonical_bytes(records)).hexdigest()


def _generated() -> GeneratedSet:
    return generate(seed=DEFAULT_SEED)


def test_two_generate_calls_with_same_seed_produce_identical_hashes():
    first = _generated()
    second = _generated()

    assert _sha256_of(first.train_episodes) == _sha256_of(second.train_episodes)
    assert _sha256_of(first.sealed_episodes) == _sha256_of(second.sealed_episodes)
    assert _sha256_of(first.customers) == _sha256_of(second.customers)
    assert _sha256_of(first.sealed_labels) == _sha256_of(second.sealed_labels)


def test_no_phone_or_email_pattern_outside_the_hash_fields():
    gen = _generated()
    for record in gen.customers + gen.train_episodes + gen.sealed_episodes + gen.sealed_labels:
        for key, value in record.items():
            if key in HASH_FIELDS or not isinstance(value, str):
                continue
            assert not PHONE_RE.search(value), f"phone-like string in {key}={value!r}"
            assert not EMAIL_RE.search(value), f"email-like string in {key}={value!r}"


def test_no_raw_phone_or_email_anywhere_including_hash_fields():
    """A second, literal check that the hash fields are actually hashes
    (64 lowercase hex chars) and not, say, a raw phone number that
    happens to also match \\d{10}."""
    gen = _generated()
    hex64 = re.compile(r"^[0-9a-f]{64}$")
    for customer in gen.customers:
        assert hex64.match(customer["contact_hash"])
        assert hex64.match(customer["email_hash"])
        assert EMAIL_RE.search(customer["contact_hash"]) is None
        assert EMAIL_RE.search(customer["email_hash"]) is None


def test_all_ten_edge_cases_present_with_outage_cluster_at_exact_size():
    gen = _generated()

    assert set(gen.edge_case_counts) == set(EDGE_CASE_NAMES)
    for name, count in gen.edge_case_counts.items():
        assert count >= 1, f"edge case {name!r} has count {count}"

    assert gen.edge_case_counts["issuer_outage_cluster"] == OUTAGE_CLUSTER_SIZE

    tagged = [ep for ep in gen.train_episodes if ep["edge_case"] == "issuer_outage_cluster"]
    assert len(tagged) == OUTAGE_CLUSTER_SIZE
    assert {ep["cause_class"] for ep in tagged} == {"C8"}


def test_edge_cases_are_only_present_in_train():
    gen = _generated()
    assert all(ep.get("edge_case") is None for ep in gen.sealed_episodes)


def test_sealed_only_issuer_family_never_appears_in_train():
    gen = _generated()

    train_families = {ep["issuer_family"] for ep in gen.train_episodes}
    sealed_families = {ep["issuer_family"] for ep in gen.sealed_episodes}

    assert SEALED_ONLY_ISSUER_FAMILY not in train_families
    assert SEALED_ONLY_ISSUER_FAMILY in sealed_families


def test_at_least_ten_harvested_strings_in_sealed_are_absent_from_train():
    """DoD asks for 'verbatim harvested error strings absent from train'.
    The uniqueness check is on `harvested_from` (the harvest_id), not
    `error_description`: config/taxonomy.yaml's own ratification header
    documents that error_description collapses to ~2 generic strings
    across all 20 harvested records, so it cannot carry 10 distinct
    sealed-only values — harvest_id is the field that actually identifies
    a distinct verbatim record, and it's what the sealed split reserves."""
    gen = _generated()

    train_ids = {ep["harvested_from"] for ep in gen.train_episodes if ep["harvested_from"]}
    sealed_ids = {ep["harvested_from"] for ep in gen.sealed_episodes if ep["harvested_from"]}

    sealed_only = sealed_ids - train_ids
    assert len(sealed_only) >= 10, f"only {len(sealed_only)} harvest_ids are sealed-exclusive"


def test_split_and_customer_counts_match_dod():
    gen = _generated()

    assert len(gen.train_episodes) == 400
    assert len(gen.sealed_episodes) == 200
    assert len(gen.customers) == 500
    assert all(ep["split"] == "train" for ep in gen.train_episodes)
    assert all(ep["split"] == "sealed" for ep in gen.sealed_episodes)


def test_sealed_episodes_never_carry_the_diagnosis_ground_truth():
    gen = _generated()
    assert all("cause_class" not in ep for ep in gen.sealed_episodes)


def test_labels_carry_the_ground_truth_for_every_sealed_episode():
    gen = _generated()
    sealed_ids = {ep["episode_id"] for ep in gen.sealed_episodes}
    label_ids = {label["episode_id"] for label in gen.sealed_labels}

    assert sealed_ids == label_ids
    for label in gen.sealed_labels:
        assert label["cause_class"] in {f"C{i}" for i in range(1, 10)}
        assert isinstance(label["responded"], bool)
        assert 0.0 <= label["response_probability"] <= 1.0


def test_episode_ids_and_payment_ids_are_unique_across_the_whole_dataset():
    gen = _generated()
    all_episodes = gen.train_episodes + gen.sealed_episodes
    episode_ids = [ep["episode_id"] for ep in all_episodes]
    payment_ids = [ep["payment_id"] for ep in all_episodes]

    assert len(episode_ids) == len(set(episode_ids))
    assert len(payment_ids) == len(set(payment_ids))


def test_held_out_test_set_phrase_is_absent_from_repo():
    root = Path(__file__).resolve().parent.parent
    # Built from parts so this file itself doesn't contain the literal
    # banned phrase — a repo-wide grep for it must find zero matches,
    # this test file included.
    forbidden = "held" + "-out test set"
    offenders = []
    for candidate in (root / "data", root / "holdout", root / "scripts", root / "src"):
        if not candidate.exists():
            continue
        for path in candidate.rglob("*"):
            if path.is_file() and path.suffix in {".py", ".md", ".jsonl", ".yaml", ".json"}:
                try:
                    text = path.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    continue
                if forbidden in text:
                    offenders.append(str(path))
    assert not offenders, f"forbidden phrase found in: {offenders}"
