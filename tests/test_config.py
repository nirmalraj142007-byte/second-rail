from __future__ import annotations

import pytest

from src.config import Settings, load_settings, require_razorpay
from src.errors import ConfigError

_ENV_VARS = (
    "RAZORPAY_KEY_ID",
    "RAZORPAY_KEY_SECRET",
    "RAZORPAY_WEBHOOK_SECRET",
    "LLM_PROVIDER",
    "LLM_API_KEY",
    "LLM_MODEL",
    "DB_PATH",
    "AUDIT_DIR",
    "CACHE_DIR",
    "MODE",
    "TIMEZONE",
)


def test_load_settings_succeeds_with_empty_environment_and_no_env_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)

    settings = load_settings()

    assert settings.mode == "dry_run"
    assert settings.razorpay_key_id is None
    assert settings.razorpay_key_secret is None
    assert settings.llm_provider == "none"


def test_require_razorpay_raises_config_error_naming_missing_vars():
    settings = Settings(razorpay_key_id=None, razorpay_key_secret=None)

    with pytest.raises(ConfigError) as exc_info:
        require_razorpay(settings)

    message = str(exc_info.value)
    assert "RAZORPAY_KEY_ID" in message
    assert "RAZORPAY_KEY_SECRET" in message
    assert ".env.example" in exc_info.value.remediation


def test_require_razorpay_succeeds_when_both_credentials_present():
    settings = Settings(razorpay_key_id="dummy_key_id", razorpay_key_secret="dummy_secret")

    key_id, key_secret = require_razorpay(settings)

    assert key_id == "dummy_key_id"
    assert key_secret == "dummy_secret"
