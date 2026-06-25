"""Environment-variable settings (see §10 of the design brief)."""
from __future__ import annotations

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

OpsinBackend = Literal["local", "local_only", "web"]
ResponseType = Literal["ephemeral", "in_channel"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    SLACK_SIGNING_SECRET: str = ""
    SLACK_RESPONSE_TYPE: ResponseType = "ephemeral"
    BASE_URL: str | None = None

    FIRESTORE_COLLECTION: str = "molecules"
    RETENTION_DAYS: int = 7
    MAX_ATOMS: int = 200

    OPSIN_BACKEND: OpsinBackend = "local"
    OPSIN_JAR_PATH: str = "/opt/opsin/opsin.jar"
    OPSIN_WEB_URL: str = "https://opsin.ch.cam.ac.uk/opsin/"

    # Phase 3 two-stage flow (Cloud Tasks). Empty defaults let local
    # development / unit tests boot without GCP credentials; the
    # /slack/mol name: branch refuses to run when TASKS_QUEUE_ID,
    # TASKS_PROJECT_ID, or TASKS_INVOKER_SA are blank.
    TASKS_QUEUE_ID: str = ""
    TASKS_LOCATION: str = "asia-northeast1"
    TASKS_PROJECT_ID: str = ""
    TASKS_INVOKER_SA: str = ""
    INTERNAL_PROCESS_PATH: str = "/internal/process"
    IDEMPOTENCY_COLLECTION: str = "molecules_idempotency"
    IDEMPOTENCY_TTL_SECONDS: int = 3600

    LOG_LEVEL: str = "INFO"
    PORT: int = 8080

    # EMBED_MAX_RETRIES is intentionally NOT exposed: §6.1 fixes the retry
    # count at 3 because each attempt has its own bespoke parameter set, and
    # additional attempts would have no defined parameters.


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reload_settings() -> Settings:
    """Test helper — force re-read of the environment."""
    global _settings
    _settings = Settings()
    return _settings
