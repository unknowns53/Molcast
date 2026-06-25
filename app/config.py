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

    # Local-dev only. Both default False so production deploys, which
    # never set these env vars, never take the bypass branches. A
    # combined startup log line (see app.main lifespan) warns loudly if
    # either is True so a misconfigured prod deploy is obvious in logs.
    #
    # DEV_SKIP_SIGNATURE_VERIFICATION: bypass the Slack request signing
    # HMAC check on /slack/mol. Useful for curl-driven testing and for
    # tunnel-based dev where the prod signing secret is not on hand.
    #
    # DEV_INLINE_NAME_RESOLUTION: in the name: branch, skip Cloud Tasks
    # and run OPSIN + RDKit + Firestore + response_url POST in a
    # background thread. Lets `/mol name: ...` work locally without a
    # Cloud Tasks queue or invoker service account.
    DEV_SKIP_SIGNATURE_VERIFICATION: bool = False
    DEV_INLINE_NAME_RESOLUTION: bool = False

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
