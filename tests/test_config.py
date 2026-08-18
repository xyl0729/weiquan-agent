import pytest
from pydantic import ValidationError

from app.config import PROJECT_ROOT, Settings


def production_values(tmp_path: object) -> dict[str, object]:
    root = tmp_path
    statutes = root / "statutes.db"
    statutes.write_bytes(b"sqlite-placeholder")
    return {
        "_env_file": None,
        "deployment_mode": "production",
        "database_url": (
            "postgresql+psycopg://weiquan:database-secret@"
            "postgres:5432/weiquan"
        ),
        "public_base_url": "https://weiquan.example.test",
        "cors_origins": "https://weiquan.example.test",
        "cookie_secure": True,
        "llm_provider": "deepseek",
        "key_mode": "server",
        "deepseek_api_key": "deepseek-secret-value",
        "session_secret": "session-secret-value-with-at-least-32-bytes",
        "ip_hmac_secret": "ip-hmac-secret-value-with-at-least-32-bytes",
        "aliyun_access_key_id": "aliyun-access-key-id",
        "aliyun_access_key_secret": "aliyun-access-key-secret",
        "directmail_account_name": "notice@example.test",
        "oss_endpoint": "https://oss-cn-hangzhou.aliyuncs.com",
        "oss_bucket": "weiquan-private-test",
        "deletion_manifest_recipient": "age1productionrecipient",
        "privacy_policy_version": "2026-08-10",
        "statutes_db_path": statutes,
        "attachment_temp_dir": root / "private" / "attachments",
        "log_dir": root / "private" / "logs",
        "backup_staging_dir": root / "private" / "backup-staging",
    }


def test_cors_origins_are_split_and_trimmed() -> None:
    settings = Settings(
        _env_file=None,
        cors_origins="http://localhost:8000, https://example.test ",
    )

    assert settings.allowed_origins == [
        "http://localhost:8000",
        "https://example.test",
    ]


def test_empty_server_key_is_not_a_credential() -> None:
    settings = Settings(_env_file=None, server_api_key="  ")

    assert settings.server_api_key is None


def test_unknown_key_mode_fails_early() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, key_mode="unexpected")


def test_attachment_limits_have_local_safe_defaults() -> None:
    settings = Settings(_env_file=None)

    assert settings.max_attachments_per_turn == 3
    assert settings.max_attachment_bytes == 10 * 1024 * 1024
    assert settings.max_attachment_pdf_pages == 20
    assert settings.max_attachment_image_pixels == 25_000_000
    assert settings.max_attachment_extracted_chars == 200_000
    assert settings.max_attachment_context_chars == 12_000
    assert settings.attachment_extraction_timeout_seconds == 90
    assert settings.attachment_draft_ttl_seconds == 3600
    assert settings.attachment_low_confidence_threshold == 0.75
    assert settings.attachment_temp_path.is_relative_to(PROJECT_ROOT)
    assert settings.attachment_temp_path != PROJECT_ROOT


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_attachments_per_turn", 0),
        ("max_attachments_per_turn", 4),
        ("max_attachment_bytes", 0),
        ("max_attachment_bytes", 51 * 1024 * 1024),
        ("max_attachment_pdf_pages", 0),
        ("max_attachment_pdf_pages", 101),
        ("max_attachment_image_pixels", 0),
        ("max_attachment_image_pixels", 100_000_001),
        ("max_attachment_extracted_chars", 0),
        ("max_attachment_extracted_chars", 1_000_001),
        ("max_attachment_context_chars", 0),
        ("max_attachment_context_chars", 100_001),
        ("attachment_extraction_timeout_seconds", 0),
        ("attachment_extraction_timeout_seconds", 301),
        ("attachment_draft_ttl_seconds", 59),
        ("attachment_draft_ttl_seconds", 86_401),
        ("attachment_low_confidence_threshold", -0.01),
        ("attachment_low_confidence_threshold", 1.01),
    ],
)
def test_attachment_limits_reject_values_outside_hard_bounds(
    field: str,
    value: int | float,
) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})


@pytest.mark.parametrize(
    "attachment_temp_dir",
    [
        PROJECT_ROOT,
        PROJECT_ROOT.parent / "outside-attachments",
        PROJECT_ROOT / "app" / "web",
        PROJECT_ROOT / "app" / "web" / "uploads",
        PROJECT_ROOT / "data" / "app.db",
    ],
)
def test_attachment_temp_directory_stays_in_private_project_space(
    attachment_temp_dir: object,
) -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            attachment_temp_dir=attachment_temp_dir,
        )


def test_local_and_test_modes_keep_safe_development_defaults() -> None:
    local = Settings(_env_file=None)
    test = Settings(_env_file=None, deployment_mode="test")

    assert local.deployment_mode == "local"
    assert local.database_dsn is None
    assert local.cookie_secure is False
    assert local.llm_total_timeout_seconds == 30
    assert test.deployment_mode == "test"
    assert test.database_dsn is None


@pytest.mark.parametrize("value", [0, -1, 30.01])
def test_llm_total_timeout_rejects_values_outside_budget(
    value: float,
) -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            llm_total_timeout_seconds=value,
        )


@pytest.mark.parametrize("value", [4, 61])
def test_pending_trial_ip_grant_ttl_rejects_outside_bounds(
    value: int,
) -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            trial_pending_ip_grant_ttl_minutes=value,
        )


@pytest.mark.parametrize("value", [5, 15, 60])
def test_pending_trial_ip_grant_ttl_accepts_supported_values(
    value: int,
) -> None:
    settings = Settings(
        _env_file=None,
        trial_pending_ip_grant_ttl_minutes=value,
    )

    assert settings.trial_pending_ip_grant_ttl_minutes == value


def test_complete_production_settings_are_accepted(tmp_path) -> None:
    settings = Settings(**production_values(tmp_path))

    assert settings.deployment_mode == "production"
    assert settings.database_dsn.startswith("postgresql+psycopg://")
    assert settings.public_origin == "https://weiquan.example.test"
    assert settings.cookie_secure is True
    assert settings.captcha_enabled is False
    assert settings.captcha_scene_id == ""
    assert settings.captcha_prefix == ""
    assert settings.attachment_temp_path.is_absolute()


def test_production_missing_fields_fail_without_exposing_secrets(
    tmp_path,
) -> None:
    values = production_values(tmp_path)
    secret_values = {
        "database-secret",
        "deepseek-secret-value",
        "session-secret-value-with-at-least-32-bytes",
        "ip-hmac-secret-value-with-at-least-32-bytes",
        "aliyun-access-key-secret",
    }
    values.pop("oss_bucket")

    with pytest.raises(ValidationError) as caught:
        Settings(**values)

    error = str(caught.value)
    assert "OSS_BUCKET" in error
    assert "CAPTCHA_PREFIX" not in error
    assert "CAPTCHA_SCENE_ID" not in error
    assert all(secret not in error for secret in secret_values)


def test_enabled_production_captcha_requires_complete_public_config(
    tmp_path,
) -> None:
    values = production_values(tmp_path)
    values["captcha_enabled"] = True

    with pytest.raises(ValidationError) as caught:
        Settings(**values)

    error = str(caught.value)
    assert "CAPTCHA_PREFIX" in error
    assert "CAPTCHA_SCENE_ID" in error

    values.update(
        {
            "captcha_scene_id": "captcha-scene",
            "captcha_prefix": "captcha-prefix-1",
        }
    )
    settings = Settings(**values)
    assert settings.captcha_enabled is True


@pytest.mark.parametrize(
    "prefix",
    [
        "contains.dot",
        "contains_underscore",
        "-starts-with-hyphen",
        "ends-with-hyphen-",
        "contains whitespace",
        "a" * 64,
    ],
)
def test_captcha_prefix_rejects_unsafe_dns_labels(prefix: str) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, captcha_prefix=prefix)


@pytest.mark.parametrize(
    ("field", "value", "expected_name"),
    [
        (
            "database_url",
            "sqlite:///production.db",
            "DATABASE_URL",
        ),
        (
            "public_base_url",
            "http://weiquan.example.test",
            "PUBLIC_BASE_URL",
        ),
        ("cors_origins", "*", "CORS_ORIGINS"),
        ("cookie_secure", False, "COOKIE_SECURE"),
        ("llm_provider", "fake", "LLM_PROVIDER"),
        ("key_mode", "byok", "KEY_MODE"),
        ("session_secret", "short", "SESSION_SECRET"),
        ("ip_hmac_secret", "short", "IP_HMAC_SECRET"),
    ],
)
def test_production_rejects_insecure_values(
    tmp_path,
    field: str,
    value: object,
    expected_name: str,
) -> None:
    values = production_values(tmp_path)
    values[field] = value

    with pytest.raises(ValidationError) as caught:
        Settings(**values)

    assert expected_name in str(caught.value)


@pytest.mark.parametrize(
    ("first", "second", "expected_name"),
    [
        ("attachment_temp_dir", "log_dir", "ATTACHMENT_TEMP_DIR"),
        ("attachment_temp_dir", "backup_staging_dir", "ATTACHMENT_TEMP_DIR"),
        ("log_dir", "backup_staging_dir", "LOG_DIR"),
    ],
)
def test_production_private_paths_must_be_separate(
    tmp_path,
    first: str,
    second: str,
    expected_name: str,
) -> None:
    values = production_values(tmp_path)
    values[second] = values[first]

    with pytest.raises(ValidationError) as caught:
        Settings(**values)

    assert expected_name in str(caught.value)


def test_production_requires_existing_statutes_database(tmp_path) -> None:
    values = production_values(tmp_path)
    values["statutes_db_path"] = tmp_path / "missing-statutes.db"

    with pytest.raises(ValidationError) as caught:
        Settings(**values)

    assert "STATUTES_DB_PATH" in str(caught.value)
