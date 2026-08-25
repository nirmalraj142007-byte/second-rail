"""Typed settings, loaded from environment / .env, validated at boot.

Every credential field is Optional. load_settings() must succeed with no
.env and no environment variables set — that's the path `make eval` runs on
a judge's clean machine with no key. Code that actually needs a credential
calls require_razorpay() (or the LLM-client equivalent, added later) and
gets a ConfigError naming exactly what's missing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import typer
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.errors import ConfigError


class Settings(BaseSettings):
    razorpay_key_id: str | None = None
    razorpay_key_secret: str | None = None
    razorpay_webhook_secret: str | None = None
    llm_provider: Literal["gemini", "openai", "none"] = "none"
    llm_api_key: str | None = None
    llm_model: str = "gemini-2.5-flash"
    db_path: Path = Path("second_rail.db")
    audit_dir: Path = Path("evidence/audit")
    cache_dir: Path = Path("cache")
    mode: Literal["dry_run", "execute", "fixture"] = "dry_run"
    timezone: str = "Asia/Kolkata"

    model_config = SettingsConfigDict(env_file=".env", env_prefix="", extra="ignore")


def load_settings() -> Settings:
    return Settings()


def require_razorpay(settings: Settings) -> tuple[str, str]:
    """Raise ConfigError naming exactly which credential is missing."""
    missing = []
    if not settings.razorpay_key_id:
        missing.append("RAZORPAY_KEY_ID")
    if not settings.razorpay_key_secret:
        missing.append("RAZORPAY_KEY_SECRET")
    if missing:
        raise ConfigError(
            f"missing required Razorpay credential(s): {', '.join(missing)}",
            remediation="copy .env.example to .env and fill in the missing value(s)",
            code="MISSING_RAZORPAY_CREDENTIALS",
        )
    assert settings.razorpay_key_id is not None
    assert settings.razorpay_key_secret is not None
    return settings.razorpay_key_id, settings.razorpay_key_secret


_DOCTOR_VARS: list[tuple[str, str, str]] = [
    ("RAZORPAY_KEY_ID", "razorpay_key_id", "Razorpay API key id (test or live mode)"),
    ("RAZORPAY_KEY_SECRET", "razorpay_key_secret", "Razorpay API key secret"),
    (
        "RAZORPAY_WEBHOOK_SECRET",
        "razorpay_webhook_secret",
        "HMAC secret for verifying payment.failed webhooks",
    ),
    (
        "LLM_API_KEY",
        "llm_api_key",
        "API key for the configured LLM provider (diagnose/choose stages)",
    ),
]

app = typer.Typer(add_completion=False)


@app.callback()
def _cli() -> None:
    """Second Rail config CLI."""


@app.command()
def doctor() -> None:
    """Report presence of every required environment variable. Always exits 0."""
    settings = load_settings()
    widths = (24, 9, 62)
    typer.echo(f"{'VAR':<{widths[0]}} {'STATUS':<{widths[1]}} {'PURPOSE':<{widths[2]}}")
    typer.echo("-" * (sum(widths) + 2))
    for env_name, field_name, purpose in _DOCTOR_VARS:
        status = "present" if getattr(settings, field_name) else "MISSING"
        typer.echo(f"{env_name:<{widths[0]}} {status:<{widths[1]}} {purpose:<{widths[2]}}")


if __name__ == "__main__":
    app()
