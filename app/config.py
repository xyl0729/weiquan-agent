from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    key_mode: Literal["byok", "server", "hybrid"] = "byok"
    server_api_key: SecretStr | None = None
    free_quota_per_day: int = Field(default=2, ge=0)
    daily_spend_limit_usd: float = Field(default=5.0, gt=0)

    model_cheap: str = "claude-haiku-4-5-20251001"
    model_main: str = "claude-sonnet-5"
    price_cheap_in: float = Field(default=1.0, ge=0)
    price_cheap_out: float = Field(default=5.0, ge=0)
    price_main_in: float = Field(default=3.0, ge=0)
    price_main_out: float = Field(default=15.0, ge=0)

    db_path: Path = Path("./data/app.db")
    statutes_db_path: Path = Path("./data/statutes.db")
    retrieval_mode: Literal["bm25", "hybrid"] = "bm25"
    embed_model: str = ""
    cors_origins: str = "http://localhost:8000"
    session_ttl_hours: int = Field(default=72, gt=0)
    default_jurisdiction: str = ""

    @field_validator("server_api_key", mode="before")
    @classmethod
    def empty_key_is_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @property
    def allowed_origins(self) -> list[str]:
        return [
            origin.strip()
            for origin in self.cors_origins.split(",")
            if origin.strip()
        ]

    def absolute_path(self, value: Path) -> Path:
        if value.is_absolute():
            return value.resolve()
        return (PROJECT_ROOT / value).resolve()


@lru_cache
def get_settings() -> Settings:
    return Settings()

