"""Typed application configuration loaded from the environment.

We use `pydantic-settings` so every env var is:
  * declared in one place with a type and a default,
  * validated at startup (fail fast on missing secrets),
  * editor-autocompletable downstream via `get_settings()`.

`get_settings()` is `lru_cache`-d so the parsing/validation cost is paid
exactly once per process.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal, Optional

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class Settings(BaseSettings):
    """Process-wide configuration.

    Values are read from environment variables (case-insensitive) and, as
    a fallback, from a local `.env` file. Secrets are wrapped in
    `SecretStr` so they don't leak into logs/reprs by accident.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- MongoDB ---------------------------------------------------------
    mongodb_uri: SecretStr = Field(
        default=SecretStr("mongodb://localhost:27017"),
        description="Full MongoDB connection string.",
    )
    mongodb_db: str = Field(
        default="govradar",
        description="Database name used by the pipeline.",
    )
    mongodb_collection: str = Field(
        default="leads",
        description="Collection name for extracted leads.",
    )

    # --- Anthropic / Claude ---------------------------------------------
    # Optional at the settings layer so `--dry-run`, `--help`, and the
    # delivery exporter all work on a fresh checkout without secrets.
    # `LeadExtractor.__init__` raises if it's None at the point of use.
    anthropic_api_key: Optional[SecretStr] = Field(
        default=None,
        description="Anthropic API key. Required for live extraction; may be unset for dry-run / delivery.",
    )
    anthropic_model: str = Field(
        default="claude-sonnet-4-6",
        description=(
            "Claude model ID used for extraction. "
            "See https://docs.anthropic.com/en/docs/about-claude/models"
        ),
    )
    anthropic_max_tokens: int = Field(
        default=4096,
        ge=256,
        le=8192,
        description="Max output tokens per extraction call.",
    )
    anthropic_timeout_seconds: float = Field(
        default=60.0,
        description="Per-request timeout for the Anthropic client.",
    )

    # --- Scraper --------------------------------------------------------
    scraper_headless: bool = Field(
        default=True,
        description="Run Playwright headless. Set false to debug visually.",
    )
    scraper_user_agent: str = Field(
        default=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        description="UA string presented to target sites.",
    )
    scraper_nav_timeout_ms: int = Field(
        default=45_000,
        description="Per-page navigation timeout in milliseconds.",
    )
    scraper_max_html_chars: int = Field(
        default=120_000,
        description=(
            "Hard cap on cleaned HTML/text length sent to Claude. Guards "
            "against runaway pages and keeps token spend predictable."
        ),
    )

    # --- Logging --------------------------------------------------------
    log_level: LogLevel = Field(
        default="INFO",
        description="Root log level.",
    )
    log_json: bool = Field(
        default=False,
        description="If true, emit logs as single-line JSON (ship-friendly).",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the singleton `Settings` instance.

    Cached so repeated calls in hot paths (e.g. inside `extractor.py`)
    don't re-parse the environment each time.
    """
    return Settings()  # type: ignore[call-arg]
