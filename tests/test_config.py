import pytest
from pydantic import ValidationError

from app.config import PROJECT_ROOT, Settings


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
