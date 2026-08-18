from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from app.attachments.drafts import OcrDraftStore
from app.attachments.models import ExtractionBlock, ExtractionResult


def _result(text: str = "draft text") -> ExtractionResult:
    return ExtractionResult(
        media_type="application/pdf",
        page_count=1,
        extraction_method="direct_text",
        blocks=(
            ExtractionBlock(
                page_number=1,
                block_index=0,
                text=text,
                confidence=1,
            ),
        ),
    )


def _envelope(
    attachment_id: str,
    owner_id: str,
    result: object,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "attachment_id": attachment_id,
        "owner_id": owner_id,
        "result": result,
    }


def _write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )


def test_cleanup_orphans_removes_invalid_and_temporary_sidecars(
    tmp_path: Path,
) -> None:
    root = tmp_path / "drafts"
    store = OcrDraftStore(root)
    owner_id = str(uuid4())
    live_attachment_id = str(uuid4())
    orphan_attachment_id = str(uuid4())
    result = _result()

    live_path = store.save(
        live_attachment_id,
        result,
        owner_id=owner_id,
    )
    corrupt_path = root / f"{uuid4().hex}.ocr.json"
    corrupt_path.write_text("{not-json", encoding="utf-8")
    invalid_path = root / f"{uuid4().hex}.ocr.json"
    _write_json(
        invalid_path,
        _envelope(
            str(uuid4()),
            owner_id,
            {
                "media_type": "application/pdf",
                "page_count": 1,
                "extraction_method": "direct_text",
                "blocks": [],
            },
        ),
    )
    orphan_path = root / f"{uuid4().hex}.ocr.json"
    _write_json(
        orphan_path,
        _envelope(
            orphan_attachment_id,
            owner_id,
            result.model_dump(mode="json"),
        ),
    )
    temporary_path = root / f"{uuid4().hex}.ocr.tmp"
    _write_json(
        temporary_path,
        _envelope(
            live_attachment_id,
            owner_id,
            result.model_dump(mode="json"),
        ),
    )
    unrelated_path = root / "keep.txt"
    unrelated_path.write_text("unmanaged", encoding="utf-8")

    removed = store.cleanup_orphans(
        lambda candidate_owner, candidate_attachment: (
            candidate_owner == owner_id
            and candidate_attachment == live_attachment_id
        )
    )

    assert removed == 4
    assert live_path.exists()
    assert store.load(live_attachment_id, owner_id=owner_id) == result
    assert not corrupt_path.exists()
    assert not invalid_path.exists()
    assert not orphan_path.exists()
    assert not temporary_path.exists()
    assert unrelated_path.exists()


def test_sidecars_are_isolated_by_owner_id(tmp_path: Path) -> None:
    store = OcrDraftStore(tmp_path / "drafts")
    attachment_id = str(uuid4())
    first_owner = str(uuid4())
    second_owner = str(uuid4())
    first_result = _result("first owner")
    second_result = _result("second owner")

    store.save(attachment_id, first_result, owner_id=first_owner)
    store.save(attachment_id, second_result, owner_id=second_owner)

    assert store.load(attachment_id, owner_id=first_owner) == first_result
    assert store.load(attachment_id, owner_id=second_owner) == second_result

    assert store.delete(attachment_id, owner_id=first_owner) == 1
    assert store.load(attachment_id, owner_id=first_owner) is None
    assert store.load(attachment_id, owner_id=second_owner) == second_result
