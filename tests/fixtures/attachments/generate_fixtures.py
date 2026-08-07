from __future__ import annotations

import struct
import zlib
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
)


FIXTURE_DIR = Path(__file__).resolve().parent


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    return ImageFont.load_default(size=size)


def _ocr_image() -> Image.Image:
    image = Image.new("RGB", (1200, 500), "white")
    draw = ImageDraw.Draw(image)
    draw.text((55, 70), "ORDER 299", fill="black", font=_font(72))
    draw.text(
        (55, 210),
        "DATE 2026-08-07",
        fill="black",
        font=_font(58),
    )
    draw.text((55, 340), "TOTAL 1299", fill="black", font=_font(58))
    return image


def _pdf_text(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("(", "\\(")
        .replace(")", "\\)")
    )


def _add_text_page(writer: PdfWriter, lines: tuple[str, ...]) -> None:
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    resources = DictionaryObject(
        {
            NameObject("/Font"): DictionaryObject(
                {NameObject("/F1"): writer._add_object(font)}
            )
        }
    )
    operations = ["BT", "/F1 20 Tf", "72 720 Td"]
    for index, line in enumerate(lines):
        if index:
            operations.append("0 -34 Td")
        operations.append(f"({_pdf_text(line)}) Tj")
    operations.append("ET")

    contents = DecodedStreamObject()
    contents.set_data(("\n".join(operations) + "\n").encode("ascii"))
    page[NameObject("/Resources")] = resources
    page[NameObject("/Contents")] = writer._add_object(contents)


def _write_selectable_pdf() -> None:
    writer = PdfWriter()
    _add_text_page(writer, ("PAGE ONE", "ORDER 299"))
    _add_text_page(writer, ("PAGE TWO", "DATE 2026-08-07"))
    with (FIXTURE_DIR / "selectable.pdf").open("wb") as output:
        writer.write(output)


def _write_scan_pdf(image: Image.Image) -> None:
    image.save(
        FIXTURE_DIR / "scan.pdf",
        "PDF",
        resolution=144,
    )


def _write_mixed_pdf() -> None:
    writer = PdfWriter()
    selectable = PdfReader(FIXTURE_DIR / "selectable.pdf", strict=True)
    scanned = PdfReader(FIXTURE_DIR / "scan.pdf", strict=True)
    writer.add_page(selectable.pages[0])
    writer.add_page(scanned.pages[0])
    with (FIXTURE_DIR / "mixed.pdf").open("wb") as output:
        writer.write(output)


def _write_encrypted_pdf() -> None:
    source = PdfReader(FIXTURE_DIR / "selectable.pdf", strict=True)
    writer = PdfWriter()
    writer.add_page(source.pages[0])
    writer.encrypt("fixture-password")
    with (FIXTURE_DIR / "encrypted.pdf").open("wb") as output:
        writer.write(output)


def _write_over_page_limit_pdf() -> None:
    writer = PdfWriter()
    for _ in range(21):
        writer.add_blank_page(width=72, height=72)
    with (FIXTURE_DIR / "over-pages.pdf").open("wb") as output:
        writer.write(output)


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    checksum = zlib.crc32(kind)
    checksum = zlib.crc32(data, checksum)
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", checksum & 0xFFFFFFFF)
    )


def _write_huge_png_header() -> None:
    header = struct.pack(">IIBBBBB", 5001, 5000, 8, 2, 0, 0, 0)
    payload = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IEND", b"")
    )
    (FIXTURE_DIR / "huge-header.png").write_bytes(payload)


def main() -> None:
    image = _ocr_image()
    image.save(FIXTURE_DIR / "numeric.png", "PNG")
    image.save(FIXTURE_DIR / "numeric.jpg", "JPEG", quality=95)
    Image.new("RGB", (320, 180), "white").save(
        FIXTURE_DIR / "blank.png",
        "PNG",
    )
    image.filter(ImageFilter.GaussianBlur(radius=5)).save(
        FIXTURE_DIR / "blurred.png",
        "PNG",
    )

    _write_selectable_pdf()
    _write_scan_pdf(image)
    _write_mixed_pdf()
    _write_encrypted_pdf()
    _write_over_page_limit_pdf()

    source = (FIXTURE_DIR / "selectable.pdf").read_bytes()
    (FIXTURE_DIR / "truncated.pdf").write_bytes(source[: len(source) // 2])
    (FIXTURE_DIR / "disguised.pdf").write_bytes(
        (FIXTURE_DIR / "numeric.png").read_bytes()
    )
    (FIXTURE_DIR / "malformed.png").write_bytes(
        b"\x89PNG\r\n\x1a\nnot-a-complete-png"
    )
    _write_huge_png_header()


if __name__ == "__main__":
    main()
