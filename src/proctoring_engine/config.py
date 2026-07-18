"""Environment-backed application settings with no import-time side effects."""

from __future__ import annotations

from dataclasses import dataclass
from os import getenv

from dotenv import load_dotenv


@dataclass(frozen=True, slots=True)
class Settings:
    """Configuration required by the service and schema tooling."""

    database_url: str
    app_env: str


def get_settings() -> Settings:
    """Load a local `.env` when present, then read current environment values."""

    load_dotenv(override=False)

    return Settings(
        database_url=getenv(
            "DATABASE_URL",
            "postgresql+psycopg://proctoring:change-me@localhost:5432/proctoring",
        ),
        app_env=getenv("APP_ENV", "development"),
    )
