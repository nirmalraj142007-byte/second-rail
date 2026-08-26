"""Second Rail — synthetic episode generator.

Every distribution below (instrument mix, amount lognormal shape, segment
mix, time-of-day peaks, issuer-family and amount shift on the sealed
split) is ILLUSTRATIVE, calibrated to be plausible, NOT sourced. Nothing
here claims to reflect real Razorpay merchant traffic — see
outcome_model.md §5 for what this model cannot tell you.

`generate()` is threaded through a single seeded `random.Random`
instance. No call anywhere in this module touches the global `random`
module or numpy, and nothing reads the wall clock for content that ends
up in an output file — two calls with the same seed produce
byte-identical `data/train.jsonl` and `holdout/sealed.jsonl`, checked by
tests/test_generator.py.

SPLIT NAMING: this is a SEALED SPLIT — train and the sealed split come
from the same generator, so it is deliberately never called by the other,
stricter term for a split whose data the model has never seen in any
form (see CLAUDE.md's U-05 resolution for why that distinction matters
here). What makes this split worth anything anyway is that it carries a
genuine, engineered distribution shift (see THE SHIFT below), not a
same-distribution slice under a different label.

THE SHIFT — three independent, disjoint mechanisms, all real (not a
relabelling):
  1. Issuer family: one synthetic issuer family (BANK_E) is reserved for
     the sealed split and never assigned to a train episode.
  2. Harvested error strings: 11 of the 20 real harvested records (>= the
     10 required) are reserved for the sealed split and never emitted in
     train — see SEALED_ONLY_HARVEST_IDS below and holdout/SHIFT.md for
     the identical-across-runs verbatim uniqueness claim, checked on
     `harvested_from` (the harvest_id), not on `error_description` — the
     ratified taxonomy (config/taxonomy.yaml header) already established
     that error_description collapses to ~2 generic strings across the
     20 harvested records, so it cannot itself carry 10 distinct unseen
     values; harvest_id is the field that actually identifies a distinct
     verbatim record.
  3. Amount distribution: the sealed split's lognormal median is ~20%
     higher than train's (SEALED_MEDIAN_SHIFT).

GROUND TRUTH: `cause_class` (the diagnosis target) is present in
data/train.jsonl but stripped from holdout/sealed.jsonl. The true
cause_class and a simulated customer-response draw for every sealed
episode live only in holdout/labels.jsonl, which src/ must never read —
see scripts/holdout_guard.py.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.config_models import ConfigBundle, load_all

IST = timezone(timedelta(hours=5, minutes=30))
ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
HARVEST_PATH = ROOT / "evidence" / "harvested_errors.jsonl"
DATA_DIR = ROOT / "data"
HOLDOUT_DIR = ROOT / "holdout"

DEFAULT_SEED = 20260825
N_EPISODES_DEFAULT = 600
N_CUSTOMERS_DEFAULT = 500

# A fixed synthetic reference point, not the real wall clock — every
# timestamp in the output is derived from this constant plus a seeded
# random delta, which is what makes two runs byte-identical regardless
# of when they're actually executed.
REFERENCE_NOW = datetime(2026, 8, 26, 12, 0, 0, tzinfo=IST)
GENERATION_WINDOW_DAYS = 30

INSTRUMENT_WEIGHTS: dict[str, float] = {
    "upi": 0.62, "card": 0.22, "netbanking": 0.09, "wallet": 0.07,
}
SEGMENT_WEIGHTS: dict[str, float] = {
    "first_time": 0.40, "repeat": 0.45, "high_value": 0.15,
}

TRAIN_ISSUER_FAMILIES: list[str] = ["BANK_A", "BANK_B", "BANK_C", "BANK_D", "BANK_F", "BANK_G"]
SEALED_ONLY_ISSUER_FAMILY = "BANK_E"
SEALED_ISSUER_WEIGHTS: dict[str, float] = {
    **{f: 0.80 / len(TRAIN_ISSUER_FAMILIES) for f in TRAIN_ISSUER_FAMILIES},
    SEALED_ONLY_ISSUER_FAMILY: 0.20,
}

TRAIN_MEDIAN_PAISE = 85_000              # Rs 850, per phase spec
SEALED_MEDIAN_SHIFT = 1.20               # sealed median is ~20% higher than train
SEALED_MEDIAN_PAISE = round(TRAIN_MEDIAN_PAISE * SEALED_MEDIAN_SHIFT)
AMOUNT_SIGMA = 1.3                 # calibrated so the ~99.9th pctile lands near Rs 45,000
AMOUNT_FLOOR_PAISE = 100
AMOUNT_CEILING_PAISE = 30_000_000  # Rs 300,000 sanity clip on the tail, not a business rule

SEGMENT_RESPONSE_MULTIPLIER: dict[str, float] = {
    "first_time": 0.85, "repeat": 1.00, "high_value": 1.15,
}
AMOUNT_BAND_RESPONSE_DECAY: dict[str, float] = {"A1": 1.00, "A2": 0.85, "A3": 0.65}
RESPONSE_PROBABILITY_FLOOR = 0.02
RESPONSE_PROBABILITY_CEILING = 0.95

# Reserved for the sealed split only — never emitted in train. C1 and C4
# only have one harvested anchor each in the ratified taxonomy, so both
# stay in the shared pool; every other class keeps at least one anchor
# in the shared pool too (its first-listed anchor), and everything past
# that is reserved. 11 reserved (>= the 10 the phase requires) across 7
# of the 9 classes.
SEALED_ONLY_HARVEST_IDS: frozenset[str] = frozenset({
    "01M0Y2S99XVV5CP9DQEACATSBJ",  # C2 card_disabled_for_online_payments
    "01M0Y2S99XVSMTKM1GR0QC9E9C",  # C2 debit_instrument_blocked
    "01M0Y2S99XD2ADM65TC4EKVZ1B",  # C3 pin_not_set
    "01M0Y2S99X8J27DAV25ZZ018TK",  # C5 transaction_limit_exceeded
    "01M0Y2S99XYPKN8TAEJRS3M2X9",  # C5 transaction_frequency_limit_exceeded
    "01M0Y2S99X6VF2X2T59B3T8NXV",  # C6 payment_cancelled (card)
    "01M0Y2S99XYE8KHR4S5F00634Q",  # C6 payment_declined
    "01M0Y2S99X7PYM2APST6858TBC",  # C7 payment_timed_out (2nd)
    "01M0Y2S99XJWVXDTFTC7GBT405",  # C8 bank_technical_error (2nd)
    "01M0Y2S99X69E4QDPYKQM03YXY",  # C8 bank_technical_error (3rd)
    "01M0Y2S99XPCNFHBEZQHPG12E1",  # C9 upi_app_not_available
})

EDGE_CASE_NAMES: list[str] = [
    "duplicate_webhook", "out_of_order_webhook", "already_paid_parallel",
    "refund_already_issued", "customer_opted_out", "amount_above_cap",
    "episode_older_than_window", "issuer_outage_cluster",
    "order_already_fulfilled", "frequency_cap_trip",
]

OUTAGE_CLUSTER_CLASS_ID = "C8"    # config/policy_table.yaml names C8 explicitly for this cluster
OUTAGE_CLUSTER_SIZE = 40
FREQUENCY_CAP_TRIP_COUNT = 3


@dataclass(frozen=True)
class GeneratedSet:
    customers: list[dict[str, Any]]
    train_episodes: list[dict[str, Any]]
    sealed_episodes: list[dict[str, Any]]
    sealed_labels: list[dict[str, Any]]
    shift_manifest: dict[str, Any]
    edge_case_counts: dict[str, int]


# ---------------------------------------------------------------------------
# small deterministic helpers
# ---------------------------------------------------------------------------


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _weighted_choice(rng: random.Random, weights: dict[str, float]) -> str:
    keys = list(weights.keys())
    vals = list(weights.values())
    return rng.choices(keys, weights=vals, k=1)[0]


def _draw_amount_paise(rng: random.Random, median_paise: int) -> int:
    mu = math.log(median_paise)
    value = rng.lognormvariate(mu, AMOUNT_SIGMA)
    value = max(AMOUNT_FLOOR_PAISE, min(AMOUNT_CEILING_PAISE, value))
    return round(value)


def _draw_failed_at(rng: random.Random) -> datetime:
    day_offset = rng.uniform(0, GENERATION_WINDOW_DAYS)
    day = REFERENCE_NOW - timedelta(days=day_offset)
    if rng.random() < 0.5:
        hour = rng.uniform(12, 14)   # lunch peak, IST
    else:
        hour = rng.uniform(19, 22)   # evening peak, IST
    minute = int((hour - int(hour)) * 60)
    return day.replace(hour=int(hour), minute=minute, second=rng.randrange(0, 60), microsecond=0)


def _band_for_amount(amount_paise: int, bands: list[Any]) -> str:
    for band in bands:
        above_min = amount_paise >= band.min_paise
        below_max = band.max_paise is None or amount_paise <= band.max_paise
        if above_min and below_max:
            return band.id
    raise ValueError(f"amount_paise={amount_paise} matches no configured amount_band")


def _response_probability(base_rate: float, segment: str, amount_band: str) -> float:
    prob = (
        base_rate
        * SEGMENT_RESPONSE_MULTIPLIER[segment]
        * AMOUNT_BAND_RESPONSE_DECAY[amount_band]
    )
    return max(RESPONSE_PROBABILITY_FLOOR, min(RESPONSE_PROBABILITY_CEILING, prob))


# ---------------------------------------------------------------------------
# loading harvested evidence + building per-class anchor pools
# ---------------------------------------------------------------------------


def _load_harvested(path: Path = HARVEST_PATH) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            records.append(json.loads(line))
    return records


def _build_anchor_pools(
    config: ConfigBundle, harvested: list[dict[str, Any]]
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    """Per class_id: (shared_pool, sealed_only_pool) — both lists of dicts
    with error_code/error_description/error_source/error_step/error_reason/
    harvest_id, ready to drop straight into an episode."""
    by_id = {r["harvest_id"]: r for r in harvested}
    shared: dict[str, list[dict[str, Any]]] = {}
    sealed_only: dict[str, list[dict[str, Any]]] = {}
    for cls in config.taxonomy.classes:
        shared[cls.class_id] = []
        sealed_only[cls.class_id] = []
        for anchor in cls.anchor_error_strings:
            record = by_id.get(anchor.harvest_id or "")
            if record is None:
                continue
            entry = {
                "harvest_id": anchor.harvest_id,
                "error_code": anchor.error_code,
                "error_description": anchor.error_description,
                "error_source": record.get("error_source"),
                "error_step": record.get("error_step"),
                "error_reason": anchor.reason,
            }
            pool = sealed_only if anchor.harvest_id in SEALED_ONLY_HARVEST_IDS else shared
            pool[cls.class_id].append(entry)
    for cls in config.taxonomy.classes:
        if not shared[cls.class_id]:
            raise ValueError(
                f"class {cls.class_id} has no shared (train-eligible) harvested anchor — "
                "every class needs at least one anchor outside SEALED_ONLY_HARVEST_IDS"
            )
    return shared, sealed_only


# ---------------------------------------------------------------------------
# customers
# ---------------------------------------------------------------------------


def _generate_customers(rng: random.Random, n: int) -> list[dict[str, Any]]:
    customers = []
    for i in range(1, n + 1):
        customer_id = f"cust_{i:04d}"
        segment = _weighted_choice(rng, SEGMENT_WEIGHTS)
        created_at = REFERENCE_NOW - timedelta(days=rng.uniform(30, 730))
        customers.append({
            "customer_id": customer_id,
            "synthetic_name": f"Synthetic Customer {i:04d}",
            "contact_hash": hashlib.sha256(f"synthetic:{customer_id}:contact".encode()).hexdigest(),
            "email_hash": hashlib.sha256(f"synthetic:{customer_id}:email".encode()).hexdigest(),
            "segment": segment,
            "opted_out": False,
            "opt_out_ts": None,
            "created_at": _iso(created_at),
        })
    return customers


# ---------------------------------------------------------------------------
# episode assembly
# ---------------------------------------------------------------------------


class _IdCounter:
    def __init__(self, start: int = 0) -> None:
        self._n = start

    def next(self) -> int:
        self._n += 1
        return self._n


def _build_episode(
    rng: random.Random,
    idx: int,
    split: str,
    class_id: str,
    anchor: dict[str, Any],
    median_paise: int,
    issuer_pool: dict[str, float] | list[str],
    customer: dict[str, Any],
    *,
    amount_paise_override: int | None = None,
    failed_at_override: datetime | None = None,
    received_at_override: datetime | None = None,
    edge_case: str | None = None,
    edge_case_note: str | None = None,
    extra_fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    instrument = _weighted_choice(rng, INSTRUMENT_WEIGHTS)
    issuer_family = (
        _weighted_choice(rng, issuer_pool)
        if isinstance(issuer_pool, dict)
        else rng.choice(issuer_pool)
    )
    amount_paise = (
        amount_paise_override if amount_paise_override is not None
        else _draw_amount_paise(rng, median_paise)
    )
    failed_at = failed_at_override or _draw_failed_at(rng)
    received_at = received_at_override or (failed_at + timedelta(seconds=rng.uniform(1, 240)))
    episode = {
        "episode_id": f"epi_{idx:05d}",
        "payment_id": f"pay_synthetic_{idx:05d}",
        "order_id": f"order_synthetic_{idx:05d}",
        "customer_id": customer["customer_id"],
        "amount_paise": amount_paise,
        "currency": "INR",
        "instrument": instrument,
        "issuer_family": issuer_family,
        "error_code": anchor["error_code"],
        "error_description": anchor["error_description"],
        "error_source": anchor["error_source"],
        "error_step": anchor["error_step"],
        "error_reason": anchor["error_reason"],
        "failed_at": _iso(failed_at),
        "received_at": _iso(received_at),
        "split": split,
        "is_synthetic": True,
        "harvested_from": anchor["harvest_id"],
        "cause_class": class_id,
        "segment": customer["segment"],
        "edge_case": edge_case,
        "edge_case_note": edge_case_note,
    }
    if extra_fields:
        episode.update(extra_fields)
    return episode


def _pick_class(rng: random.Random, class_weights: dict[str, float]) -> str:
    return _weighted_choice(rng, class_weights)


# ---------------------------------------------------------------------------
# train: regular episodes + the ten seeded edge cases
# ---------------------------------------------------------------------------


def _generate_train_regular(
    rng: random.Random,
    ids: _IdCounter,
    n: int,
    customers: list[dict[str, Any]],
    class_weights: dict[str, float],
    shared_pool: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    episodes = []
    for _ in range(n):
        class_id = _pick_class(rng, class_weights)
        anchor = rng.choice(shared_pool[class_id])
        customer = rng.choice(customers)
        episodes.append(_build_episode(
            rng, ids.next(), "train", class_id, anchor, TRAIN_MEDIAN_PAISE,
            TRAIN_ISSUER_FAMILIES, customer,
        ))
    return episodes


def _generate_train_edge_cases(
    rng: random.Random,
    ids: _IdCounter,
    customers: list[dict[str, Any]],
    class_weights: dict[str, float],
    shared_pool: dict[str, list[dict[str, Any]]],
    config: ConfigBundle,
) -> list[dict[str, Any]]:
    episodes: list[dict[str, Any]] = []

    def regular_episode(**kwargs: Any) -> dict[str, Any]:
        class_id = _pick_class(rng, class_weights)
        anchor = rng.choice(shared_pool[class_id])
        customer = kwargs.pop("customer", None) or rng.choice(customers)
        return _build_episode(
            rng, ids.next(), "train", class_id, anchor, TRAIN_MEDIAN_PAISE,
            TRAIN_ISSUER_FAMILIES, customer, **kwargs,
        )

    # 1. duplicate_webhook
    episodes.append(regular_episode(
        edge_case="duplicate_webhook",
        edge_case_note="Same payment_id webhook delivered twice — ingestion must dedupe.",
        extra_fields={"simulated_webhook_delivery_count": 2},
    ))

    # 2. out_of_order_webhook
    episodes.append(regular_episode(
        edge_case="out_of_order_webhook",
        edge_case_note="A terminal-state webhook arrives before its predecessor.",
        extra_fields={"simulated_webhook_order": "out_of_order"},
    ))

    # 3. already_paid_parallel
    episodes.append(regular_episode(
        edge_case="already_paid_parallel",
        edge_case_note="The order shows a successful payment through another channel already.",
        extra_fields={"already_paid_elsewhere": True},
    ))

    # 4. refund_already_issued
    episodes.append(regular_episode(
        edge_case="refund_already_issued",
        edge_case_note="A refund already exists — no recovery action should ever fire.",
        extra_fields={"refund_already_issued": True},
    ))

    # 5. customer_opted_out — mutate a designated customer, then reference it.
    opted_out_customer = customers[0]
    opted_out_customer["opted_out"] = True
    opted_out_customer["opt_out_ts"] = _iso(REFERENCE_NOW - timedelta(days=5))
    episodes.append(regular_episode(
        customer=opted_out_customer,
        edge_case="customer_opted_out",
        edge_case_note="Customer has opted out of recovery contact — hard_refuse at gate time.",
    ))

    # 6. amount_above_cap
    ceiling = config.guardrails.auto_approve_ceiling_paise
    episodes.append(regular_episode(
        edge_case="amount_above_cap",
        edge_case_note=f"amount_paise exceeds guardrails.auto_approve_ceiling_paise ({ceiling}).",
        amount_paise_override=ceiling + 250_000,
    ))

    # 7. episode_older_than_window — a very late-processed episode: the gap
    # between received_at and failed_at exceeds max_episode_age_hours.
    cap_hours = config.guardrails.max_episode_age_hours
    old_failed_at = REFERENCE_NOW - timedelta(days=10)
    old_received_at = old_failed_at + timedelta(hours=cap_hours + 28)
    episodes.append(regular_episode(
        edge_case="episode_older_than_window",
        edge_case_note=(
            f"received_at - failed_at exceeds guardrails.max_episode_age_hours ({cap_hours}h)."
        ),
        failed_at_override=old_failed_at,
        received_at_override=old_received_at,
    ))

    # 8. issuer_outage_cluster — exactly OUTAGE_CLUSTER_SIZE episodes, all
    # cause class C8, inside a 30-minute window. The mass-contact trap.
    window_start = REFERENCE_NOW - timedelta(days=rng.uniform(1, GENERATION_WINDOW_DAYS - 1))
    outage_anchor = rng.choice(shared_pool[OUTAGE_CLUSTER_CLASS_ID])
    outage_customers = rng.sample(customers, OUTAGE_CLUSTER_SIZE)
    threshold = config.guardrails.outage_cluster_threshold
    for cust in outage_customers:
        failed_at = window_start + timedelta(minutes=rng.uniform(0, 30))
        episodes.append(_build_episode(
            rng, ids.next(), "train", OUTAGE_CLUSTER_CLASS_ID, outage_anchor, TRAIN_MEDIAN_PAISE,
            TRAIN_ISSUER_FAMILIES, cust,
            failed_at_override=failed_at,
            edge_case="issuer_outage_cluster",
            edge_case_note=(
                f"{OUTAGE_CLUSTER_SIZE} episodes share cause C8 inside a 30-minute window — "
                f"exceeds guardrails.outage_cluster_threshold ({threshold}); hard_refuse, not "
                "individually recoverable."
            ),
            extra_fields={"outage_cluster_id": f"outage_{window_start.date().isoformat()}"},
        ))

    # 9. order_already_fulfilled
    episodes.append(regular_episode(
        edge_case="order_already_fulfilled",
        edge_case_note="The order was already fulfilled through a non-payment channel.",
        extra_fields={"order_already_fulfilled": True},
    ))

    # 10. frequency_cap_trip — one customer, three episodes inside 24h.
    freq_customer = customers[1]
    base_time = REFERENCE_NOW - timedelta(days=rng.uniform(1, GENERATION_WINDOW_DAYS - 1))
    offsets_hours = [0, 8, 16]
    for offset in offsets_hours:
        episodes.append(regular_episode(
            customer=freq_customer,
            failed_at_override=base_time + timedelta(hours=offset),
            edge_case="frequency_cap_trip",
            edge_case_note=(
                f"Same customer in {FREQUENCY_CAP_TRIP_COUNT} episodes inside 24h — "
                "exceeds guardrails.max_contacts_per_customer_7d after the first contact."
            ),
            extra_fields={"frequency_cap_group": "fcg_0001"},
        ))

    return episodes


# ---------------------------------------------------------------------------
# sealed split
# ---------------------------------------------------------------------------


def _generate_sealed_regular(
    rng: random.Random,
    ids: _IdCounter,
    n: int,
    customers: list[dict[str, Any]],
    class_weights: dict[str, float],
    shared_pool: dict[str, list[dict[str, Any]]],
    sealed_only_pool: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    episodes = []
    for _ in range(n):
        class_id = _pick_class(rng, class_weights)
        pool = shared_pool[class_id] + sealed_only_pool[class_id]
        anchor = rng.choice(pool)
        customer = rng.choice(customers)
        episodes.append(_build_episode(
            rng, ids.next(), "sealed", class_id, anchor, SEALED_MEDIAN_PAISE,
            SEALED_ISSUER_WEIGHTS, customer,
        ))
    return episodes


def _generate_sealed_forced(
    rng: random.Random,
    ids: _IdCounter,
    customers: list[dict[str, Any]],
    sealed_only_pool: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """One forced episode per reserved harvest_id — guarantees every
    reserved verbatim string actually shows up in the sealed split,
    rather than leaving it to chance in _generate_sealed_regular."""
    episodes = []
    for class_id, anchors in sealed_only_pool.items():
        for anchor in anchors:
            customer = rng.choice(customers)
            episodes.append(_build_episode(
                rng, ids.next(), "sealed", class_id, anchor, SEALED_MEDIAN_PAISE,
                SEALED_ISSUER_WEIGHTS, customer,
            ))
    return episodes


# ---------------------------------------------------------------------------
# ground-truth labels
# ---------------------------------------------------------------------------


def _build_labels(
    sealed_episodes: list[dict[str, Any]],
    config: ConfigBundle,
    base_rate_by_class: dict[str, float],
    rng: random.Random,
) -> list[dict[str, Any]]:
    labels = []
    for ep in sealed_episodes:
        band = _band_for_amount(ep["amount_paise"], config.policy.amount_bands)
        prob = _response_probability(base_rate_by_class[ep["cause_class"]], ep["segment"], band)
        responded = rng.random() < prob
        labels.append({
            "episode_id": ep["episode_id"],
            "cause_class": ep["cause_class"],
            "segment": ep["segment"],
            "amount_band": band,
            "response_probability": round(prob, 4),
            "responded": responded,
            "recovered_amount_paise": ep["amount_paise"] if responded else None,
        })
    return labels


# ---------------------------------------------------------------------------
# top-level generate()
# ---------------------------------------------------------------------------


def generate(
    seed: int = DEFAULT_SEED,
    n_episodes: int = N_EPISODES_DEFAULT,
    n_customers: int = N_CUSTOMERS_DEFAULT,
    config: ConfigBundle | None = None,
    harvested: list[dict[str, Any]] | None = None,
) -> GeneratedSet:
    rng = random.Random(seed)
    config = config or load_all(CONFIG_DIR)
    harvested = harvested if harvested is not None else _load_harvested()

    shared_pool, sealed_only_pool = _build_anchor_pools(config, harvested)
    class_weights = {cls.class_id: cls.generation_weight for cls in config.taxonomy.classes}
    base_rate_by_class = {cls.class_id: cls.response_base_rate for cls in config.taxonomy.classes}

    customers = _generate_customers(rng, n_customers)

    ids = _IdCounter()
    edge_cases = _generate_train_edge_cases(rng, ids, customers, class_weights, shared_pool, config)

    train_n = (n_episodes * 2) // 3
    sealed_n = n_episodes - train_n
    train_regular_n = train_n - len(edge_cases)
    if train_regular_n < 0:
        raise ValueError(f"train_n={train_n} too small to fit {len(edge_cases)} seeded edge cases")
    regular_train = _generate_train_regular(
        rng, ids, train_regular_n, customers, class_weights, shared_pool
    )
    train_episodes = regular_train + edge_cases

    forced_sealed = _generate_sealed_forced(rng, ids, customers, sealed_only_pool)
    sealed_regular_n = sealed_n - len(forced_sealed)
    if sealed_regular_n < 0:
        raise ValueError(
            f"sealed_n={sealed_n} too small to fit {len(forced_sealed)} forced anchors"
        )
    regular_sealed = _generate_sealed_regular(
        rng, ids, sealed_regular_n, customers, class_weights, shared_pool, sealed_only_pool
    )
    sealed_episodes_full = forced_sealed + regular_sealed

    sealed_labels = _build_labels(sealed_episodes_full, config, base_rate_by_class, rng)

    # Strip the diagnosis ground truth from the public sealed file.
    sealed_episodes_public = [
        {k: v for k, v in ep.items() if k != "cause_class"} for ep in sealed_episodes_full
    ]

    edge_case_counts = {name: 0 for name in EDGE_CASE_NAMES}
    for ep in train_episodes:
        if ep["edge_case"]:
            edge_case_counts[ep["edge_case"]] += 1

    shift_manifest = _build_shift_manifest(train_episodes, sealed_episodes_full)

    return GeneratedSet(
        customers=customers,
        train_episodes=train_episodes,
        sealed_episodes=sealed_episodes_public,
        sealed_labels=sealed_labels,
        shift_manifest=shift_manifest,
        edge_case_counts=edge_case_counts,
    )


def _build_shift_manifest(
    train_episodes: list[dict[str, Any]], sealed_episodes: list[dict[str, Any]]
) -> dict[str, Any]:
    train_families = {ep["issuer_family"] for ep in train_episodes}
    sealed_families = {ep["issuer_family"] for ep in sealed_episodes}
    train_harvest_ids = {ep["harvested_from"] for ep in train_episodes if ep["harvested_from"]}
    sealed_harvest_ids = {ep["harvested_from"] for ep in sealed_episodes if ep["harvested_from"]}
    reserved_seen_in_sealed = sealed_harvest_ids & SEALED_ONLY_HARVEST_IDS
    reserved_leaked_into_train = train_harvest_ids & SEALED_ONLY_HARVEST_IDS

    train_amounts = [ep["amount_paise"] for ep in train_episodes]
    sealed_amounts = [ep["amount_paise"] for ep in sealed_episodes]

    return {
        "issuer_family": {
            "sealed_only_family": SEALED_ONLY_ISSUER_FAMILY,
            "train_families": sorted(train_families),
            "sealed_families": sorted(sealed_families),
            "sealed_only_family_count_in_train": sum(
                1 for ep in train_episodes if ep["issuer_family"] == SEALED_ONLY_ISSUER_FAMILY
            ),
            "sealed_only_family_count_in_sealed": sum(
                1 for ep in sealed_episodes if ep["issuer_family"] == SEALED_ONLY_ISSUER_FAMILY
            ),
        },
        "harvested_error_strings": {
            "reserved_harvest_id_count": len(SEALED_ONLY_HARVEST_IDS),
            "reserved_harvest_ids": sorted(SEALED_ONLY_HARVEST_IDS),
            "reserved_ids_seen_in_sealed": len(reserved_seen_in_sealed),
            "reserved_ids_leaked_into_train": len(reserved_leaked_into_train),
        },
        "amount_distribution": {
            "train_median_target_paise": TRAIN_MEDIAN_PAISE,
            "sealed_median_target_paise": SEALED_MEDIAN_PAISE,
            "shift_factor": SEALED_MEDIAN_SHIFT,
            "train_actual_median_paise": round(statistics.median(train_amounts)),
            "sealed_actual_median_paise": round(statistics.median(sealed_amounts)),
        },
    }


# ---------------------------------------------------------------------------
# output writing
# ---------------------------------------------------------------------------


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(record, ensure_ascii=True, sort_keys=True) for record in records]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_shift_md(path: Path, manifest: dict[str, Any]) -> None:
    fam = manifest["issuer_family"]
    err = manifest["harvested_error_strings"]
    amt = manifest["amount_distribution"]
    train_rupees = amt["train_actual_median_paise"] / 100
    sealed_rupees = amt["sealed_actual_median_paise"] / 100
    lines = [
        "# Sealed split — distribution shift manifest",
        "",
        "Generated by `data/generator.py` as part of `make data`. This is what makes",
        "`holdout/sealed.jsonl` a genuine sealed split rather than a same-distribution",
        "slice under a different filename. See CLAUDE.md's U-05 resolution.",
        "",
        "## 1. Issuer family",
        "",
        f"`{fam['sealed_only_family']}` is reserved for the sealed split only.",
        f"Train families: {', '.join(fam['train_families'])}",
        f"Sealed families: {', '.join(fam['sealed_families'])}",
        "",
        f"- `{fam['sealed_only_family']}` episodes in train: "
        f"{fam['sealed_only_family_count_in_train']} (must be 0)",
        f"- `{fam['sealed_only_family']}` episodes in sealed: "
        f"{fam['sealed_only_family_count_in_sealed']}",
        "",
        "## 2. Harvested error strings",
        "",
        f"{err['reserved_harvest_id_count']} of the 20 real harvested `harvest_id`s are reserved",
        "for the sealed split and never emitted in train. Uniqueness is checked on",
        "`harvested_from` (the harvest_id), not `error_description` — the ratified",
        "taxonomy (config/taxonomy.yaml header) already established that",
        "`error_description` collapses to ~2 generic strings across the 20 harvested",
        "records, so it cannot itself carry 10 distinct sealed-only values.",
        "",
        f"- reserved harvest_ids seen in sealed: "
        f"{err['reserved_ids_seen_in_sealed']} (must be >= 10)",
        f"- reserved harvest_ids leaked into train: "
        f"{err['reserved_ids_leaked_into_train']} (must be 0)",
        "",
        "Reserved harvest_ids:",
        *[f"- {hid}" for hid in err["reserved_harvest_ids"]],
        "",
        "## 3. Amount distribution",
        "",
        f"- train median target: Rs {amt['train_median_target_paise'] / 100:.2f} "
        f"({amt['train_median_target_paise']} paise)",
        f"- sealed median target: Rs {amt['sealed_median_target_paise'] / 100:.2f} "
        f"({amt['sealed_median_target_paise']} paise), a "
        f"{(amt['shift_factor'] - 1) * 100:.0f}% shift",
        f"- train actual median (data/train.jsonl): Rs {train_rupees:.2f}",
        f"- sealed actual median (holdout/sealed.jsonl): Rs {sealed_rupees:.2f}",
        "",
        "## Machine-readable summary",
        "",
        "```json",
        json.dumps(manifest, indent=2, sort_keys=True),
        "```",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def write_outputs(gen: GeneratedSet) -> None:
    _write_jsonl(DATA_DIR / "customers.jsonl", gen.customers)
    _write_jsonl(DATA_DIR / "train.jsonl", gen.train_episodes)
    _write_jsonl(HOLDOUT_DIR / "sealed.jsonl", gen.sealed_episodes)
    _write_jsonl(HOLDOUT_DIR / "labels.jsonl", gen.sealed_labels)
    _write_shift_md(HOLDOUT_DIR / "SHIFT.md", gen.shift_manifest)


def _print_summary(gen: GeneratedSet) -> None:
    print(
        "Second Rail synthetic generator — distributions below are ILLUSTRATIVE, "
        "calibrated to be plausible, NOT sourced."
    )
    print(
        f"generated {len(gen.train_episodes)} train + {len(gen.sealed_episodes)} sealed "
        f"episodes across {len(gen.customers)} customers (seed={DEFAULT_SEED})"
    )
    print()
    print("edge case counts (train split):")
    width = max(len(name) for name in EDGE_CASE_NAMES)
    for name in EDGE_CASE_NAMES:
        print(f"  {name.ljust(width)}  {gen.edge_case_counts[name]}")
    print()
    fam = gen.shift_manifest["issuer_family"]
    err = gen.shift_manifest["harvested_error_strings"]
    print(
        f"shift check: {fam['sealed_only_family']} in train="
        f"{fam['sealed_only_family_count_in_train']} (must be 0), in sealed="
        f"{fam['sealed_only_family_count_in_sealed']}"
    )
    print(
        f"shift check: reserved harvest_ids in sealed={err['reserved_ids_seen_in_sealed']} "
        f"(must be >= 10), leaked into train={err['reserved_ids_leaked_into_train']} (must be 0)"
    )


def main() -> int:
    gen = generate()
    write_outputs(gen)
    _print_summary(gen)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
