from __future__ import annotations

import math
import re
from collections.abc import Sequence
from pathlib import Path

import pytest
from PIL import Image

from app.attachments.errors import (
    AttachmentInputError,
    AttachmentResourceLimitError,
    AttachmentServiceUnavailableError,
)
from app.attachments.extractors import (
    LocalDocumentExtractor,
    OcrEngine,
    OcrSpan,
)


FIXTURES = Path(__file__).parent / "fixtures" / "attachments"


class FakeOcrEngine(OcrEngine):
    def __init__(
        self,
        responses: Sequence[Sequence[OcrSpan]],
    ) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[tuple[int, int], str]] = []

    def recognize(self, image: Image.Image) -> tuple[OcrSpan, ...]:
        self.calls.append((image.size, image.mode))
        if not self.responses:
            raise AssertionError("unexpected OCR call")
        return tuple(self.responses.pop(0))


def _span(
    text: str,
    confidence: float = 0.98,
    *,
    left: float = 10,
    top: float = 10,
) -> OcrSpan:
    return OcrSpan(
        text=text,
        confidence=confidence,
        box=(
            (left, top),
            (left + 100, top),
            (left + 100, top + 20),
            (left, top + 20),
        ),
    )


def _assert_code(
    expected_code: str,
    operation: object,
    expected_type: type[Exception],
) -> None:
    with pytest.raises(expected_type) as captured:
        operation()  # type: ignore[operator]
    assert getattr(captured.value, "code", None) == expected_code


def test_selectable_pdf_preserves_page_order_without_initializing_ocr() -> None:
    def fail_if_initialized() -> OcrEngine:
        raise AssertionError("selectable PDF must not initialize OCR")

    extractor = LocalDocumentExtractor(ocr_factory=fail_if_initialized)
    result = extractor.extract(FIXTURES / "selectable.pdf")

    assert result.media_type == "application/pdf"
    assert result.page_count == 2
    assert result.extraction_method == "direct_text"
    assert [block.page_number for block in result.blocks] == [1, 1, 2, 2]
    assert "PAGE ONE" in result.blocks[0].text
    assert "PAGE TWO" in result.blocks[2].text
    assert all(block.confidence == 1 for block in result.blocks)


def test_scanned_and_mixed_pdf_only_ocr_pages_without_selectable_text() -> None:
    scan_ocr = FakeOcrEngine(
        [
            (
                _span("订单金额 299 元", top=20),
                _span("日期 2026-08-07", top=60),
            )
        ]
    )
    scan = LocalDocumentExtractor(ocr_engine=scan_ocr).extract(
        FIXTURES / "scan.pdf"
    )

    assert scan.extraction_method == "ocr"
    assert scan.page_count == 1
    assert len(scan_ocr.calls) == 1
    assert [block.page_number for block in scan.blocks] == [1, 1]

    mixed_ocr = FakeOcrEngine(
        [[_span("扫描页金额 1299 元", top=30)]]
    )
    mixed = LocalDocumentExtractor(ocr_engine=mixed_ocr).extract(
        FIXTURES / "mixed.pdf"
    )

    assert mixed.extraction_method == "mixed"
    assert mixed.page_count == 2
    assert len(mixed_ocr.calls) == 1
    assert mixed.blocks[0].page_number == 1
    assert mixed.blocks[-1].page_number == 2
    assert mixed.blocks[-1].text == "扫描页金额 1299 元"


@pytest.mark.parametrize(
    ("fixture_name", "expected_media_type"),
    [
        ("numeric.png", "image/png"),
        ("numeric.jpg", "image/jpeg"),
    ],
)
def test_images_return_ordered_structured_ocr_blocks(
    fixture_name: str,
    expected_media_type: str,
) -> None:
    ocr = FakeOcrEngine(
        [
            (
                _span("日期 2026-08-07", left=15, top=80),
                _span("订单金额 299 元", left=20, top=20),
                _span("编号 18", left=300, top=20),
            )
        ]
    )
    result = LocalDocumentExtractor(ocr_engine=ocr).extract(
        FIXTURES / fixture_name,
        declared_media_type=expected_media_type,
    )

    assert result.media_type == expected_media_type
    assert result.page_count == 1
    assert result.extraction_method == "ocr"
    assert result.width_px == 1200
    assert result.height_px == 500
    assert [block.text for block in result.blocks] == [
        "订单金额 299 元",
        "编号 18",
        "日期 2026-08-07",
    ]
    assert [block.block_index for block in result.blocks] == [0, 1, 2]
    assert all(
        block.page_number == 1 and 0 <= block.confidence <= 1
        for block in result.blocks
    )
    assert ocr.calls == [((1200, 500), "RGB")]


def test_low_confidence_is_warned_and_blank_ocr_is_rejected() -> None:
    low_confidence = FakeOcrEngine(
        [[_span("模糊金额 299 元", confidence=0.5)]]
    )
    result = LocalDocumentExtractor(
        ocr_engine=low_confidence,
        low_confidence_threshold=0.75,
    ).extract(FIXTURES / "blurred.png")
    assert result.warnings == ("low_confidence",)

    empty = LocalDocumentExtractor(
        ocr_engine=FakeOcrEngine([()])
    )
    _assert_code(
        "attachment_text_empty",
        lambda: empty.extract(FIXTURES / "blank.png"),
        AttachmentInputError,
    )


def test_pdf_and_image_input_failures_have_stable_safe_codes(
    tmp_path: Path,
) -> None:
    extractor = LocalDocumentExtractor(
        ocr_engine=FakeOcrEngine([()])
    )
    unknown = tmp_path / "unknown.bin"
    unknown.write_bytes(b"not a supported document")

    cases = (
        (
            "attachment_pdf_encrypted",
            lambda: extractor.extract(FIXTURES / "encrypted.pdf"),
            AttachmentInputError,
        ),
        (
            "attachment_corrupt",
            lambda: extractor.extract(FIXTURES / "truncated.pdf"),
            AttachmentInputError,
        ),
        (
            "attachment_corrupt",
            lambda: extractor.extract(FIXTURES / "malformed.png"),
            AttachmentInputError,
        ),
        (
            "attachment_type_mismatch",
            lambda: extractor.extract(
                FIXTURES / "disguised.pdf",
                declared_media_type="application/pdf",
            ),
            AttachmentInputError,
        ),
        (
            "attachment_type_unsupported",
            lambda: extractor.extract(unknown),
            AttachmentInputError,
        ),
    )

    for code, operation, error_type in cases:
        _assert_code(code, operation, error_type)


def test_extension_is_not_trusted_but_valid_signature_is_accepted() -> None:
    ocr = FakeOcrEngine([[_span("订单 299")]])
    result = LocalDocumentExtractor(ocr_engine=ocr).extract(
        FIXTURES / "disguised.pdf"
    )

    assert result.media_type == "image/png"
    assert result.extraction_method == "ocr"


def test_page_pixel_byte_and_character_limits_reject_whole_file(
    tmp_path: Path,
) -> None:
    _assert_code(
        "attachment_page_limit_exceeded",
        lambda: LocalDocumentExtractor().extract(
            FIXTURES / "over-pages.pdf"
        ),
        AttachmentResourceLimitError,
    )
    _assert_code(
        "attachment_pixel_limit_exceeded",
        lambda: LocalDocumentExtractor().extract(
            FIXTURES / "huge-header.png"
        ),
        AttachmentResourceLimitError,
    )
    _assert_code(
        "attachment_extracted_text_too_long",
        lambda: LocalDocumentExtractor(
            max_extracted_chars=10
        ).extract(FIXTURES / "selectable.pdf"),
        AttachmentResourceLimitError,
    )

    oversized = tmp_path / "oversized.png"
    oversized.write_bytes(
        (FIXTURES / "numeric.png").read_bytes() + b"padding"
    )
    _assert_code(
        "attachment_too_large",
        lambda: LocalDocumentExtractor(
            max_file_bytes=10
        ).extract(oversized),
        AttachmentResourceLimitError,
    )


def test_pillow_decompression_bomb_is_mapped_to_pixel_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 100)
    _assert_code(
        "attachment_pixel_limit_exceeded",
        lambda: LocalDocumentExtractor().extract(
            FIXTURES / "numeric.png"
        ),
        AttachmentResourceLimitError,
    )


@pytest.mark.parametrize("confidence", [math.nan, -0.01, 1.01])
def test_invalid_ocr_confidence_fails_closed(confidence: float) -> None:
    ocr = FakeOcrEngine([[_span("订单 299", confidence=confidence)]])
    _assert_code(
        "attachment_service_unavailable",
        lambda: LocalDocumentExtractor(ocr_engine=ocr).extract(
            FIXTURES / "numeric.png"
        ),
        AttachmentServiceUnavailableError,
    )


def test_real_rapidocr_smoke_uses_bundled_local_models() -> None:
    result = LocalDocumentExtractor().extract(FIXTURES / "numeric.png")
    digits = re.sub(r"\D", "", " ".join(
        block.text for block in result.blocks
    ))

    assert result.extraction_method == "ocr"
    assert result.blocks
    assert "299" in digits
    assert all(0 <= block.confidence <= 1 for block in result.blocks)
