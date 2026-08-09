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
        hide_input_in_errors=True,
    )

    deployment_mode: Literal["local", "test", "production"] = "local"
    database_url: SecretStr | None = None
    public_base_url: str | None = None
    cookie_name: str = "weiquan_session"
    cookie_secure: bool = False
    session_secret: SecretStr | None = None
    ip_hmac_secret: SecretStr | None = None
    privacy_policy_version: str = "local-development"

    aliyun_access_key_id: SecretStr | None = None
    aliyun_access_key_secret: SecretStr | None = None
    directmail_account_name: str = ""
    directmail_from_alias: str = "维权咨询助手"
    directmail_region: str = "cn-hangzhou"
    captcha_scene_id: str = ""
    captcha_endpoint: str = "captcha.cn-shanghai.aliyuncs.com"
    oss_endpoint: str = ""
    oss_bucket: str = ""
    deletion_manifest_recipient: str = ""

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
    log_dir: Path = Path("./logs")
    backup_staging_dir: Path = Path("./.tmp/backup-staging")

    @field_validator(
        "server_api_key",
        "deepseek_api_key",
        "database_url",
        "session_secret",
        "ip_hmac_secret",
        "aliyun_access_key_id",
        "aliyun_access_key_secret",
        mode="before",
    )
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

    @field_validator("public_base_url")
    @classmethod
    def normalize_public_base_url(
        cls,
        value: str | None,
    ) -> str | None:
        if value is None:
            return None
        normalized = value.strip().rstrip("/")
        if not normalized:
            return None
        parsed = urlparse(normalized)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("PUBLIC_BASE_URL 必须是无凭据的站点根 URL")
        return normalized

    @field_validator(
        "directmail_account_name",
        "directmail_from_alias",
        "directmail_region",
        "captcha_scene_id",
        "captcha_endpoint",
        "oss_endpoint",
        "oss_bucket",
        "deletion_manifest_recipient",
        "privacy_policy_version",
    )
    @classmethod
    def trim_service_text(cls, value: str) -> str:
        return value.strip()

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
        if (
            self.deployment_mode != "production"
            and (temp_path == root or not temp_path.is_relative_to(root))
        ):
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

    @model_validator(mode="after")
    def validate_production_settings(self) -> "Settings":
        if self.deployment_mode != "production":
            return self

        invalid: set[str] = set()
        dsn = self.database_dsn
        if dsn is None or urlparse(dsn).scheme != "postgresql+psycopg":
            invalid.add("DATABASE_URL")

        parsed_public = urlparse(self.public_base_url or "")
        if (
            parsed_public.scheme != "https"
            or not parsed_public.netloc
            or parsed_public.hostname in {"localhost", "127.0.0.1", "::1"}
        ):
            invalid.add("PUBLIC_BASE_URL")

        origins = self.allowed_origins
        if (
            not origins
            or "*" in origins
            or self.public_origin is None
            or set(origins) != {self.public_origin}
        ):
            invalid.add("CORS_ORIGINS")

        if not self.cookie_secure:
            invalid.add("COOKIE_SECURE")
        if self.llm_provider != "deepseek":
            invalid.add("LLM_PROVIDER")
        if self.key_mode != "server":
            invalid.add("KEY_MODE")

        required_secrets = {
            "DEEPSEEK_API_KEY": self.deepseek_api_key,
            "SESSION_SECRET": self.session_secret,
            "IP_HMAC_SECRET": self.ip_hmac_secret,
            "ALIYUN_ACCESS_KEY_ID": self.aliyun_access_key_id,
            "ALIYUN_ACCESS_KEY_SECRET": self.aliyun_access_key_secret,
        }
        for field_name, secret in required_secrets.items():
            if secret is None:
                invalid.add(field_name)

        for field_name, secret in {
            "SESSION_SECRET": self.session_secret,
            "IP_HMAC_SECRET": self.ip_hmac_secret,
        }.items():
            if (
                secret is not None
                and len(secret.get_secret_value().encode("utf-8")) < 32
            ):
                invalid.add(field_name)

        required_text = {
            "DIRECTMAIL_ACCOUNT_NAME": self.directmail_account_name,
            "CAPTCHA_SCENE_ID": self.captcha_scene_id,
            "OSS_ENDPOINT": self.oss_endpoint,
            "OSS_BUCKET": self.oss_bucket,
            "DELETION_MANIFEST_RECIPIENT": (
                self.deletion_manifest_recipient
            ),
            "PRIVACY_POLICY_VERSION": self.privacy_policy_version,
        }
        for field_name, value in required_text.items():
            if not value:
                invalid.add(field_name)

        if (
            not self.statute_database_path.exists()
            or not self.statute_database_path.is_file()
        ):
            invalid.add("STATUTES_DB_PATH")

        private_paths = {
            "ATTACHMENT_TEMP_DIR": self.attachment_temp_path,
            "LOG_DIR": self.logs_path,
            "BACKUP_STAGING_DIR": self.backup_staging_path,
        }
        values = list(private_paths.values())
        if len(set(values)) != len(values):
            invalid.update(private_paths)
        static_path = (PROJECT_ROOT / "app" / "web").resolve()
        for field_name, path in private_paths.items():
            if path == static_path or path.is_relative_to(static_path):
                invalid.add(field_name)

        if invalid:
            fields = ", ".join(sorted(invalid))
            raise ValueError(f"生产配置无效: {fields}")
        return self

    @property
    def allowed_origins(self) -> list[str]:
        return [
            origin.strip()
            for origin in self.cors_origins.split(",")
            if origin.strip()
        ]

    @property
    def database_dsn(self) -> str | None:
        if self.database_url is None:
            return None
        return self.database_url.get_secret_value()

    @property
    def public_origin(self) -> str | None:
        if self.public_base_url is None:
            return None
        parsed = urlparse(self.public_base_url)
        return f"{parsed.scheme}://{parsed.netloc}"

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

    @property
    def logs_path(self) -> Path:
        return self.absolute_path(self.log_dir)

    @property
    def backup_staging_path(self) -> Path:
        return self.absolute_path(self.backup_staging_dir)


@lru_cache
def get_settings() -> Settings:
    return Settings()
