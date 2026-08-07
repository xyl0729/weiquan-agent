from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    llm_provider: Literal["fake", "deepseek"] = "fake"
    deepseek_api_key: SecretStr | None = None
    deepseek_model: str = "deepseek-chat"
    deepseek_base_url: str = "https://api.deepseek.com"
    llm_timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    llm_max_retries: int = Field(default=1, ge=0, le=3)
    deepseek_price_input_per_million: float | None = Field(
        default=None,
        ge=0,
    )
    deepseek_price_output_per_million: float | None = Field(
        default=None,
        ge=0,
    )

    max_message_length: int = Field(default=4000, ge=100, le=20000)
    max_slot_string_length: int = Field(default=2000, ge=100, le=10000)
    classification_min_confidence: float = Field(
        default=0.45,
        ge=0,
        le=1,
    )

    # These fields remain for the existing credential/limit boundary. The
    # consultation endpoint reads the DeepSeek key only from settings.
    key_mode: Literal["byok", "server", "hybrid"] = "byok"
    server_api_key: SecretStr | None = None
    free_quota_per_day: int = Field(default=2, ge=0)
    daily_spend_limit_usd: float = Field(default=5.0, gt=0)

    db_path: Path = Path("./data/app.db")
    statutes_db_path: Path = Path("./data/statutes.db")
    playbook_dir: Path = Path("./app/playbooks")
    template_dir: Path = Path("./app/rendering/templates")
    jurisdiction_config_path: Path = Path("./app/jurisdiction/data.yaml")
    retrieval_mode: Literal["bm25", "hybrid"] = "bm25"
    cors_origins: str = "http://localhost:8000"
    session_ttl_hours: int = Field(default=72, gt=0)
    default_jurisdiction: str = ""

    max_attachments_per_turn: int = Field(default=3, ge=1, le=3)
    max_attachment_bytes: int = Field(
        default=10 * 1024 * 1024,
        ge=1024,
        le=50 * 1024 * 1024,
    )
    max_attachment_pdf_pages: int = Field(default=20, ge=1, le=100)
    max_attachment_image_pixels: int = Field(
        default=25_000_000,
        ge=1,
        le=100_000_000,
    )
    max_attachment_extracted_chars: int = Field(
        default=200_000,
        ge=1,
        le=1_000_000,
    )
    max_attachment_context_chars: int = Field(
        default=12_000,
        ge=1,
        le=100_000,
    )
    attachment_extraction_timeout_seconds: float = Field(
        default=90,
        ge=1,
        le=300,
    )
    attachment_draft_ttl_seconds: int = Field(
        default=3600,
        ge=60,
        le=86_400,
    )
    attachment_low_confidence_threshold: float = Field(
        default=0.75,
        ge=0,
        le=1,
    )
    attachment_temp_dir: Path = Path("./.tmp/attachments")

    @field_validator("server_api_key", "deepseek_api_key", mode="before")
    @classmethod
    def empty_key_is_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator(
        "deepseek_price_input_per_million",
        "deepseek_price_output_per_million",
        mode="before",
    )
    @classmethod
    def empty_price_is_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("deepseek_model")
    @classmethod
    def model_name_is_not_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("DEEPSEEK_MODEL 不能为空")
        return value

    @field_validator("deepseek_base_url")
    @classmethod
    def validate_deepseek_base_url(cls, value: str) -> str:
        normalized = value.strip().rstrip("/")
        parsed = urlparse(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("DEEPSEEK_BASE_URL 必须是 http(s) URL")
        if parsed.query or parsed.fragment:
            raise ValueError("DEEPSEEK_BASE_URL 不能包含 query 或 fragment")
        return normalized

    @model_validator(mode="after")
    def validate_spend_prices(self) -> "Settings":
        prices = (
            self.deepseek_price_input_per_million,
            self.deepseek_price_output_per_million,
        )
        if (prices[0] is None) != (prices[1] is None):
            raise ValueError("DeepSeek 输入和输出单价必须同时配置或同时留空")
        return self

    @model_validator(mode="after")
    def validate_attachment_temp_directory(self) -> "Settings":
        root = PROJECT_ROOT.resolve()
        temp_path = self.attachment_temp_path
        static_path = (root / "app" / "web").resolve()
        forbidden_files = {
            self.database_path,
            self.statute_database_path,
        }
        if temp_path == root or not temp_path.is_relative_to(root):
            raise ValueError("附件临时目录必须位于项目根目录内")
        if (
            temp_path == static_path
            or temp_path.is_relative_to(static_path)
        ):
            raise ValueError("附件临时目录不能位于静态资源目录")
        if temp_path in forbidden_files:
            raise ValueError("附件临时目录不能指向数据库文件")
        if temp_path.exists() and not temp_path.is_dir():
            raise ValueError("附件临时目录不能指向普通文件")
        return self

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

    @property
    def database_path(self) -> Path:
        return self.absolute_path(self.db_path)

    @property
    def statute_database_path(self) -> Path:
        return self.absolute_path(self.statutes_db_path)

    @property
    def playbooks_path(self) -> Path:
        return self.absolute_path(self.playbook_dir)

    @property
    def templates_path(self) -> Path:
        return self.absolute_path(self.template_dir)

    @property
    def jurisdiction_path(self) -> Path:
        return self.absolute_path(self.jurisdiction_config_path)

    @property
    def attachment_temp_path(self) -> Path:
        return self.absolute_path(self.attachment_temp_dir)


@lru_cache
def get_settings() -> Settings:
    return Settings()
