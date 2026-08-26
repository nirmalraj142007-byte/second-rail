from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from src.config_models import config_hash, load_all
from src.errors import ConfigError

REAL_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


def _copy_config(tmp_path: Path) -> Path:
    dest = tmp_path / "config"
    shutil.copytree(REAL_CONFIG_DIR, dest)
    return dest


def test_load_all_succeeds_against_the_real_config_dir():
    bundle = load_all(REAL_CONFIG_DIR)

    assert len(bundle.taxonomy.classes) > 0
    assert len(bundle.policy.rules) > 0
    assert bundle.guardrails.attribution_window_hours == 48


def test_config_hash_is_stable_across_two_loads():
    first = config_hash(load_all(REAL_CONFIG_DIR))
    second = config_hash(load_all(REAL_CONFIG_DIR))

    assert first == second
    assert len(first) == 64  # sha256 hex digest


def test_config_hash_changes_when_a_file_changes(tmp_path):
    config_dir = _copy_config(tmp_path)
    baseline = config_hash(load_all(config_dir))

    guardrails_path = config_dir / "guardrails.yaml"
    text = guardrails_path.read_text(encoding="utf-8")
    guardrails_path.write_text(
        text.replace("batch_contact_ceiling: 50", "batch_contact_ceiling: 51"),
        encoding="utf-8",
    )

    changed = config_hash(load_all(config_dir))
    assert changed != baseline


def test_corrupted_taxonomy_raises_config_error_naming_the_file(tmp_path):
    config_dir = _copy_config(tmp_path)
    taxonomy_path = config_dir / "taxonomy.yaml"
    text = taxonomy_path.read_text(encoding="utf-8")
    # Drop a required field (recoverable_in_principle) from the first class.
    corrupted = text.replace("    recoverable_in_principle: true\n", "", 1)
    taxonomy_path.write_text(corrupted, encoding="utf-8")

    with pytest.raises(ConfigError) as exc_info:
        load_all(config_dir)

    assert "taxonomy.yaml" in str(exc_info.value)


def test_corrupted_policy_table_raises_config_error_naming_the_file(tmp_path):
    config_dir = _copy_config(tmp_path)
    policy_path = config_dir / "policy_table.yaml"
    text = policy_path.read_text(encoding="utf-8")
    # Break a rule so its admissible_actions set no longer includes no_action.
    corrupted = text.replace(
        "admissible_actions: [link_alt_instrument, defer_2h, no_action]\n"
        "    escalation_tier: auto\n"
        "    justification: >-\n"
        "      Low amount, proven customer",
        "admissible_actions: [link_alt_instrument, defer_2h]\n"
        "    escalation_tier: auto\n"
        "    justification: >-\n"
        "      Low amount, proven customer",
        1,
    )
    assert corrupted != text, "fixture text did not match — update replacement for policy_table"
    policy_path.write_text(corrupted, encoding="utf-8")

    with pytest.raises(ConfigError) as exc_info:
        load_all(config_dir)

    assert "policy_table.yaml" in str(exc_info.value)


def test_corrupted_guardrails_raises_config_error_naming_the_file(tmp_path):
    config_dir = _copy_config(tmp_path)
    guardrails_path = config_dir / "guardrails.yaml"
    text = guardrails_path.read_text(encoding="utf-8")
    corrupted = text.replace(
        'default_mode: dry_run               # execution requires --execute',
        'default_mode: not_a_real_mode       # execution requires --execute',
    )
    assert corrupted != text, "fixture text did not match — update replacement for guardrails.yaml"
    guardrails_path.write_text(corrupted, encoding="utf-8")

    with pytest.raises(ConfigError) as exc_info:
        load_all(config_dir)

    assert "guardrails.yaml" in str(exc_info.value)
