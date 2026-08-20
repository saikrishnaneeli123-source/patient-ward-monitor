"""Application settings, loaded from the environment (see .env.example)."""
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="WARD_", env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./ward.db"
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
    return settings
