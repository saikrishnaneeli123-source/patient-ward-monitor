"""Application settings, loaded from the environment (see .env.example)."""
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="WARD_", env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./ward.db"

    # Signs session cookies. Leave unset in development and a random key is
    # generated at startup (sessions then end on restart); set it in production
    # or every restart logs everyone out.
    secret_key: str = ""
    session_max_age: int = 8 * 60 * 60  # a shift length
    cookie_secure: bool = False  # set True behind HTTPS
    upload_dir: Path = Path("./uploads")
    extraction_model: str = "claude-opus-5"
    max_upload_mb: int = 25

    # Auto-created case records always start unverified; a clinician confirms them
    # before they are treated as the source of truth. Set to False to require a
    # human to press "Verify" even for the patient identity match.
    auto_link_existing_patients: bool = True

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    if not settings.secret_key:
        import logging
        import secrets

        settings.secret_key = secrets.token_urlsafe(48)
        logging.getLogger(__name__).warning(
            "WARD_SECRET_KEY is not set — using a random key. Sessions will not "
            "survive a restart, and multiple workers will reject each other's cookies."
        )
    return settings
