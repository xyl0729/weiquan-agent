from __future__ import annotations

import math
import warnings
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

import pypdfium2 as pdfium
from PIL import Image, UnidentifiedImageError
from pypdf import PdfReader

from app.attachments.errors import (
    AttachmentError,
    AttachmentInputError,
    AttachmentResourceLimitError,
    AttachmentServiceUnavailableError,
)
from app.attachments.models import (
    AttachmentMediaType,
    ExtractionBlock,
    ExtractionResult,
)


_PDF_SIGNATURE = b"%PDF-"
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_JPEG_SIGNATURE = b"\xff\xd8\xff"
_ALLOWED_MEDIA_TYPES = frozenset(
    {"application/pdf", "image/png", "image/jpeg"}
)


@dataclass(frozen=True, slots=True)
class OcrSpan:
    text: str
    confidence: float
    box: tuple[tuple[float, float], ...]


class OcrEngine(Protocol):
    def recognize(self, image: Image.Image) -> Sequence[OcrSpan]:
        ...


class DocumentExtractor(Protocol):
    def extract(
        self,
        path: Path,
        *,
        declared_media_type: str | None = None,
    ) -> ExtractionResult:
        ...


class RapidOcrEngine:
    def __init__(self) -> None:
        try:
            from rapidocr_onnxruntime import RapidOCR

            self._engine = RapidOCR()
        except Exception:
            raise AttachmentServiceUnavailableError() from None

    def recognize(self, image: Image.Image) -> tuple[OcrSpan, ...]:
        try:
            import numpy as np

            pixels = np.asarray(image)
            if pixels.ndim == 3 and pixels.shape[2] >= 3:
                pixels = pixels[:, :, :3][:, :, ::-1]
            raw_result, _ = self._engine(pixels)
            if raw_result is None:
                return ()

            spans: list[OcrSpan] = []
            for item in raw_result:
                if not isinstance(item, (list, tuple)) or len(item) < 3:
                    raise ValueError("invalid OCR item")
                raw_box, raw_text, raw_confidence = item[:3]
                if not isinstance(raw_text, str):
                    raise ValueError("invalid OCR text")
                box = tuple(
                    (float(point[0]), float(point[1]))
                    for point in raw_box
                )
                spans.append(
                    OcrSpan(
                        text=raw_text,
                        confidence=float(raw_confidence),
                        box=box,
                    )
                )
            return tuple(spans)
        except AttachmentError:
            raise
        except Exception:
            raise AttachmentServiceUnavailableError() from None


@dataclass(frozen=True, slots=True)
class _OrderedSpan:
    text: str
    confidence: float
    left: float
    top: float
    right: float
    bottom: float

    @property
    def height(self) -> float:
        return max(1.0, self.bottom - self.top)


class LocalDocumentExtractor:
    def __init__(
        self,
        *,
        max_file_bytes: int = 10 * 1024 * 1024,
        max_pdf_pages: int = 20,
        max_image_pixels: int = 25_000_000,
        max_extracted_chars: int = 200_000,
        low_confidence_threshold: float = 0.75,
        pdf_render_dpi: int = 144,
        min_direct_text_chars: int = 8,
        ocr_engine: OcrEngine | None = None,
        ocr_factory: Callable[[], OcrEngine] | None = None,
    ) -> None:
        positive_limits = (
            max_file_bytes,
            max_pdf_pages,
            max_image_pixels,
            max_extracted_chars,
            pdf_render_dpi,
            min_direct_text_chars,
        )
        if any(value <= 0 for value in positive_limits):
            raise ValueError("提取限制必须大于 0")
        if not 0 <= low_confidence_threshold <= 1:
            raise ValueError("低置信度阈值必须位于 0 到 1")
        if ocr_engine is not None and ocr_factory is not None:
            raise ValueError("OCR 引擎和工厂不能同时提供")

        self.max_file_bytes = max_file_bytes
        self.max_pdf_pages = max_pdf_pages
        self.max_image_pixels = max_image_pixels
        self.max_extracted_chars = max_extracted_chars
        self.low_confidence_threshold = low_confidence_threshold
        self.pdf_render_dpi = pdf_render_dpi
        self.min_direct_text_chars = min_direct_text_chars
        self._ocr_engine = ocr_engine
        self._ocr_factory = ocr_factory or RapidOcrEngine

    def extract(
        self,
        path: Path,
        *,
        declared_media_type: str | None = None,
    ) -> ExtractionResult:
        file_path = Path(path)
        self._check_file_size(file_path)
        media_type = self._detect_media_type(file_path)
        self._check_declared_type(media_type, declared_media_type)

        if media_type == "application/pdf":
            return self._extract_pdf(file_path)
        return self._extract_image(file_path, media_type)

    def _check_file_size(self, path: Path) -> None:
        try:
            size_bytes = path.stat().st_size
        except OSError:
            raise AttachmentInputError("attachment_corrupt") from None
        if size_bytes > self.max_file_bytes:
            raise AttachmentResourceLimitError("attachment_too_large")
        if size_bytes <= 0:
            raise AttachmentInputError("attachment_corrupt")

    def _detect_media_type(self, path: Path) -> AttachmentMediaType:
        try:
            with path.open("rb") as source:
                signature = source.read(16)
        except OSError:
            raise AttachmentInputError("attachment_corrupt") from None

        if signature.startswith(_PDF_SIGNATURE):
            return "application/pdf"
        if signature.startswith(_PNG_SIGNATURE):
            return "image/png"
        if signature.startswith(_JPEG_SIGNATURE):
            return "image/jpeg"
        raise AttachmentInputError("attachment_type_unsupported")

    @staticmethod
    def _check_declared_type(
        actual: AttachmentMediaType,
        declared: str | None,
    ) -> None:
        if declared is None:
            return
        normalized = declared.strip().lower()
        if normalized not in _ALLOWED_MEDIA_TYPES:
            raise AttachmentInputError("attachment_type_unsupported")
        if normalized != actual:
            raise AttachmentInputError("attachment_type_mismatch")

    def _extract_pdf(self, path: Path) -> ExtractionResult:
        reader = self._open_pdf(path)
        page_count = self._pdf_page_count(reader)
        if page_count > self.max_pdf_pages:
            raise AttachmentResourceLimitError(
                "attachment_page_limit_exceeded"
            )
        self._reject_active_pdf_content(reader)

        direct_pages: list[tuple[str, ...] | None] = []
        for page in reader.pages:
            try:
                text = page.extract_text() or ""
            except Exception:
                raise AttachmentInputError("attachment_corrupt") from None
            paragraphs = _text_paragraphs(text)
            meaningful_chars = sum(
                1
                for paragraph in paragraphs
                for char in paragraph
                if char.isalnum()
            )
            direct_pages.append(
                paragraphs
                if meaningful_chars >= self.min_direct_text_chars
                else None
            )

        ocr_indexes = [
            index
            for index, paragraphs in enumerate(direct_pages)
            if paragraphs is None
        ]
        ocr_pages = self._ocr_pdf_pages(path, page_count, ocr_indexes)

        blocks: list[ExtractionBlock] = []
        total_chars = 0
        has_direct = False
        has_ocr = False
        has_low_confidence = False
        for page_index, paragraphs in enumerate(direct_pages):
            page_number = page_index + 1
            if paragraphs is not None:
                has_direct = True
                page_blocks = tuple(
                    ExtractionBlock(
                        page_number=page_number,
                        block_index=block_index,
                        text=text,
                        confidence=1,
                    )
                    for block_index, text in enumerate(paragraphs)
                )
            else:
                has_ocr = True
                page_blocks = ocr_pages[page_index]

            total_chars = self._checked_character_total(
                total_chars,
                page_blocks,
            )
            if any(
                block.confidence < self.low_confidence_threshold
                for block in page_blocks
            ):
                has_low_confidence = True
            blocks.extend(page_blocks)

        if not blocks:
            raise AttachmentInputError("attachment_text_empty")
        if has_direct and has_ocr:
            method = "mixed"
        elif has_ocr:
            method = "ocr"
        else:
            method = "direct_text"

        return ExtractionResult(
            media_type="application/pdf",
            page_count=page_count,
            extraction_method=method,
            blocks=tuple(blocks),
            warnings=("low_confidence",) if has_low_confidence else (),
        )

    @staticmethod
    def _open_pdf(path: Path) -> PdfReader:
        try:
            reader = PdfReader(path, strict=True)
            if reader.is_encrypted:
                raise AttachmentInputError("attachment_pdf_encrypted")
            return reader
        except AttachmentError:
            raise
        except Exception:
            raise AttachmentInputError("attachment_corrupt") from None

    @staticmethod
    def _pdf_page_count(reader: PdfReader) -> int:
        try:
            page_count = len(reader.pages)
        except Exception:
            raise AttachmentInputError("attachment_corrupt") from None
        if page_count <= 0:
            raise AttachmentInputError("attachment_text_empty")
        return page_count

    @staticmethod
    def _reject_active_pdf_content(reader: PdfReader) -> None:
        try:
            root = reader.root_object
            if "/OpenAction" in root or "/AA" in root:
                raise AttachmentInputError("attachment_corrupt")
            names = _resolved_mapping(root.get("/Names"))
            if names is not None and (
                "/EmbeddedFiles" in names or "/JavaScript" in names
            ):
                raise AttachmentInputError("attachment_corrupt")

            for page in reader.pages:
                if "/AA" in page:
                    raise AttachmentInputError("attachment_corrupt")
                annotations = page.get("/Annots") or ()
                for raw_annotation in annotations:
                    annotation = _resolved_mapping(raw_annotation)
                    if annotation is None:
                        continue
                    if annotation.get("/Subtype") == "/FileAttachment":
                        raise AttachmentInputError("attachment_corrupt")
                    action = _resolved_mapping(annotation.get("/A"))
                    if action is not None and action.get("/S") in {
                        "/JavaScript",
                        "/Launch",
                    }:
                        raise AttachmentInputError("attachment_corrupt")
        except AttachmentError:
            raise
        except Exception:
            raise AttachmentInputError("attachment_corrupt") from None

    def _ocr_pdf_pages(
        self,
        path: Path,
        expected_page_count: int,
        page_indexes: Sequence[int],
    ) -> dict[int, tuple[ExtractionBlock, ...]]:
        if not page_indexes:
            return {}
        try:
            document = pdfium.PdfDocument(path)
        except Exception:
            raise AttachmentInputError("attachment_corrupt") from None

        results: dict[int, tuple[ExtractionBlock, ...]] = {}
        try:
            if len(document) != expected_page_count:
                raise AttachmentInputError("attachment_corrupt")
            for page_index in page_indexes:
                results[page_index] = self._ocr_pdf_page(
                    document,
                    page_index,
                )
        except AttachmentError:
            raise
        except Exception:
            raise AttachmentInputError("attachment_corrupt") from None
        finally:
            document.close()
        return results

    def _ocr_pdf_page(
        self,
        document: pdfium.PdfDocument,
        page_index: int,
    ) -> tuple[ExtractionBlock, ...]:
        page = document[page_index]
        bitmap = None
        image = None
        normalized = None
        try:
            width, height = page.get_size()
            scale = self.pdf_render_dpi / 72
            pixel_width = math.ceil(float(width) * scale)
            pixel_height = math.ceil(float(height) * scale)
            self._check_pixel_count(pixel_width, pixel_height)

            bitmap = page.render(scale=scale)
            image = bitmap.to_pil()
            normalized = _normalize_image(image)
            return self._ocr_blocks(normalized, page_index + 1)
        except AttachmentError:
            raise
        except Exception:
            raise AttachmentInputError("attachment_corrupt") from None
        finally:
            if normalized is not None:
                normalized.close()
            if image is not None:
                image.close()
            if bitmap is not None:
                bitmap.close()
            page.close()

    def _extract_image(
        self,
        path: Path,
        media_type: AttachmentMediaType,
    ) -> ExtractionResult:
        image = None
        try:
            width, height, decoded_type = self._verify_image(path)
            if decoded_type != media_type:
                raise AttachmentInputError("attachment_type_mismatch")
            self._check_pixel_count(width, height)
            image = self._load_normalized_image(path)
            blocks = self._ocr_blocks(image, 1)
            self._checked_character_total(0, blocks)
        except AttachmentError:
            raise
        finally:
            if image is not None:
                image.close()

        if not blocks:
            raise AttachmentInputError("attachment_text_empty")
        has_low_confidence = any(
            block.confidence < self.low_confidence_threshold
            for block in blocks
        )
        return ExtractionResult(
            media_type=media_type,
            page_count=1,
            extraction_method="ocr",
            blocks=blocks,
            warnings=("low_confidence",) if has_low_confidence else (),
            width_px=width,
            height_px=height,
        )

    def _verify_image(
        self,
        path: Path,
    ) -> tuple[int, int, AttachmentMediaType]:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter(
                    "error",
                    Image.DecompressionBombWarning,
                )
                with Image.open(path) as candidate:
                    decoded_type = _pillow_media_type(candidate.format)
                    width, height = candidate.size
                    if width <= 0 or height <= 0:
                        raise ValueError("invalid dimensions")
                    self._check_pixel_count(width, height)
                    candidate.verify()
            return width, height, decoded_type
        except (
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
        ):
            raise AttachmentResourceLimitError(
                "attachment_pixel_limit_exceeded"
            ) from None
        except AttachmentError:
            raise
        except (
            OSError,
            SyntaxError,
            UnidentifiedImageError,
            ValueError,
            IndexError,
        ):
            raise AttachmentInputError("attachment_corrupt") from None

    @staticmethod
    def _load_normalized_image(path: Path) -> Image.Image:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter(
                    "error",
                    Image.DecompressionBombWarning,
                )
                with Image.open(path) as candidate:
                    candidate.load()
                    return _normalize_image(candidate)
        except (
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
        ):
            raise AttachmentResourceLimitError(
                "attachment_pixel_limit_exceeded"
            ) from None
        except (OSError, SyntaxError, UnidentifiedImageError, ValueError):
            raise AttachmentInputError("attachment_corrupt") from None

    def _check_pixel_count(self, width: int, height: int) -> None:
        if (
            width <= 0
            or height <= 0
            or width > self.max_image_pixels
            or height > self.max_image_pixels
            or width * height > self.max_image_pixels
        ):
            raise AttachmentResourceLimitError(
                "attachment_pixel_limit_exceeded"
            )

    def _ocr_blocks(
        self,
        image: Image.Image,
        page_number: int,
    ) -> tuple[ExtractionBlock, ...]:
        try:
            raw_spans = self._get_ocr_engine().recognize(image)
            normalized = tuple(
                span
                for span in (
                    _normalize_ocr_span(raw_span)
                    for raw_span in raw_spans
                )
                if span is not None
            )
            ordered = _reading_order(normalized)
        except AttachmentError:
            raise
        except Exception:
            raise AttachmentServiceUnavailableError() from None

        return tuple(
            ExtractionBlock(
                page_number=page_number,
                block_index=block_index,
                text=span.text,
                confidence=span.confidence,
            )
            for block_index, span in enumerate(ordered)
        )

    def _get_ocr_engine(self) -> OcrEngine:
        if self._ocr_engine is None:
            try:
                self._ocr_engine = self._ocr_factory()
            except AttachmentError:
                raise
            except Exception:
                raise AttachmentServiceUnavailableError() from None
        return self._ocr_engine

    def _checked_character_total(
        self,
        current: int,
        blocks: Iterable[ExtractionBlock],
    ) -> int:
        total = current + sum(len(block.text) for block in blocks)
        if total > self.max_extracted_chars:
            raise AttachmentResourceLimitError(
                "attachment_extracted_text_too_long"
            )
        return total


def _text_paragraphs(text: str) -> tuple[str, ...]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return tuple(
        line.strip()
        for line in normalized.split("\n")
        if line.strip()
    )


def _resolved_mapping(value: object) -> object:
    if value is None:
        return None
    resolver = getattr(value, "get_object", None)
    if callable(resolver):
        value = resolver()
    if hasattr(value, "get"):
        return value
    return None


def _pillow_media_type(value: str | None) -> AttachmentMediaType:
    if value == "PNG":
        return "image/png"
    if value == "JPEG":
        return "image/jpeg"
    raise AttachmentInputError("attachment_type_mismatch")


def _normalize_image(image: Image.Image) -> Image.Image:
    if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
        rgba = image.convert("RGBA")
        background = Image.new("RGBA", image.size, "white")
        try:
            background.alpha_composite(rgba)
            return background.convert("RGB")
        finally:
            rgba.close()
            background.close()
    return image.convert("RGB")


def _normalize_ocr_span(value: OcrSpan) -> _OrderedSpan | None:
    if not isinstance(value, OcrSpan):
        raise AttachmentServiceUnavailableError()
    if not isinstance(value.text, str):
        raise AttachmentServiceUnavailableError()
    text = value.text.strip()
    if not text:
        return None

    confidence = value.confidence
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not math.isfinite(float(confidence))
        or not 0 <= float(confidence) <= 1
    ):
        raise AttachmentServiceUnavailableError()

    points: list[tuple[float, float]] = []
    try:
        for point in value.box:
            if len(point) != 2:
                raise ValueError("invalid point")
            x = float(point[0])
            y = float(point[1])
            if not math.isfinite(x) or not math.isfinite(y):
                raise ValueError("invalid coordinate")
            points.append((x, y))
    except (TypeError, ValueError, IndexError):
        raise AttachmentServiceUnavailableError() from None
    if len(points) < 2:
        raise AttachmentServiceUnavailableError()

    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    left, right = min(xs), max(xs)
    top, bottom = min(ys), max(ys)
    if right <= left or bottom <= top:
        raise AttachmentServiceUnavailableError()
    return _OrderedSpan(
        text=text,
        confidence=float(confidence),
        left=left,
        top=top,
        right=right,
        bottom=bottom,
    )


def _reading_order(
    spans: Sequence[_OrderedSpan],
) -> tuple[_OrderedSpan, ...]:
    lines: list[list[_OrderedSpan]] = []
    for span in sorted(spans, key=lambda item: (item.top, item.left)):
        if not lines or not _same_text_line(lines[-1], span):
            lines.append([span])
        else:
            lines[-1].append(span)

    ordered: list[_OrderedSpan] = []
    for line in lines:
        ordered.extend(sorted(line, key=lambda item: (item.left, item.top)))
    return tuple(ordered)


def _same_text_line(
    line: Sequence[_OrderedSpan],
    candidate: _OrderedSpan,
) -> bool:
    line_top = min(span.top for span in line)
    line_bottom = max(span.bottom for span in line)
    overlap = min(line_bottom, candidate.bottom) - max(
        line_top,
        candidate.top,
    )
    reference_height = min(
        max(1.0, line_bottom - line_top),
        candidate.height,
    )
    return overlap >= reference_height * 0.5
