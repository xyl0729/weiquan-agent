from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import ValidationError

from app.attachments.models import ExtractionResult
from app.db.contracts import LOCAL_DEVELOPMENT_OWNER_ID


_DRAFT_NAME = re.compile(r"^[0-9a-f]{32}\.ocr\.json$")
_TEMP_NAME = re.compile(r"^[0-9a-f]{32}\.ocr\.tmp$")
_DEFAULT_MAX_BYTES = 8 * 1024 * 1024


class OcrDraftStore:
    """Keep unconfirmed extraction blocks outside the application database."""

    def __init__(
        self,
        root: Path,
        *,
        max_bytes: int = _DEFAULT_MAX_BYTES,
    ) -> None:
        if max_bytes <= 0:
            raise ValueError("sidecar 大小上限必须大于 0")
        self.root = Path(root).resolve()
        self.max_bytes = int(max_bytes)
        self._paths: dict[tuple[str, str], Path] = {}
        _validate_root(self.root)

    def save(
        self,
        attachment_id: str,
        result: ExtractionResult,
        *,
        owner_id: str = LOCAL_DEVELOPMENT_OWNER_ID,
    ) -> Path:
        normalized_attachment = _uuid(attachment_id)
        normalized_owner = _uuid(owner_id)
        payload = {
            "schema_version": 1,
            "attachment_id": normalized_attachment,
            "owner_id": normalized_owner,
            "result": result.model_dump(mode="json"),
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > self.max_bytes:
            raise ValueError("sidecar 超过大小上限")

        self._ensure_root()
        final_path = self.root / f"{uuid4().hex}.ocr.json"
        temporary_path = self.root / f"{uuid4().hex}.ocr.tmp"
        try:
            descriptor = os.open(
                temporary_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(descriptor, "wb") as output:
                output.write(encoded)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(temporary_path, 0o600)
            os.replace(temporary_path, final_path)
            os.chmod(final_path, 0o600)
        except OSError:
            _safe_unlink(temporary_path, self.root)
            _safe_unlink(final_path, self.root)
            raise

        key = (normalized_owner, normalized_attachment)
        old_paths = self._matching_paths(*key)
        self._paths[key] = final_path
        for old_path in old_paths:
            if old_path != final_path:
                _safe_unlink(old_path, self.root)
        return final_path

    write = save

    def load(
        self,
        attachment_id: str,
        *,
        owner_id: str = LOCAL_DEVELOPMENT_OWNER_ID,
    ) -> ExtractionResult | None:
        key = (_uuid(owner_id), _uuid(attachment_id))
        for path in self._matching_paths(*key):
            envelope = self._read(path)
            if envelope is not None:
                return envelope
        return None

    read = load

    def delete(
        self,
        attachment_id: str,
        *,
        owner_id: str = LOCAL_DEVELOPMENT_OWNER_ID,
    ) -> int:
        key = (_uuid(owner_id), _uuid(attachment_id))
        removed = 0
        for path in self._matching_paths(*key):
            if _safe_unlink(path, self.root):
                removed += 1
        self._paths.pop(key, None)
        return removed

    remove = delete

    def cleanup_all(self, *, limit: int = 100) -> int:
        if limit <= 0:
            raise ValueError("limit 必须大于 0")
        self._ensure_root()
        removed = 0
        for path in self._candidate_paths(include_temporary=True):
            if removed >= limit:
                break
            if _safe_unlink(path, self.root):
                removed += 1
        self._paths.clear()
        return removed

    def cleanup_orphans(
        self,
        exists: Callable[[str, str], bool],
        *,
        limit: int = 100,
    ) -> int:
        """Remove malformed or unreferenced sidecars without following links."""
        if limit <= 0:
            raise ValueError("limit 必须大于 0")
        self._ensure_root()
        removed = 0
        for path in self._candidate_paths(include_temporary=True):
            if removed >= limit:
                break
            if _TEMP_NAME.fullmatch(path.name) is not None:
                if _safe_unlink(path, self.root):
                    removed += 1
                continue
            envelope = self._read_envelope(path)
            if envelope is None:
                if _safe_unlink(path, self.root):
                    removed += 1
                continue
            owner_id = envelope["owner_id"]
            attachment_id = envelope["attachment_id"]
            if self._validated_result(envelope) is None:
                if _safe_unlink(path, self.root):
                    removed += 1
                self._paths.pop((owner_id, attachment_id), None)
                continue
            if exists(owner_id, attachment_id):
                self._paths[(owner_id, attachment_id)] = path
                continue
            if _safe_unlink(path, self.root):
                removed += 1
            self._paths.pop((owner_id, attachment_id), None)
        return removed

    def _matching_paths(
        self,
        owner_id: str,
        attachment_id: str,
    ) -> list[Path]:
        key = (owner_id, attachment_id)
        paths: list[Path] = []
        cached = self._paths.get(key)
        if cached is not None and cached.exists():
            paths.append(cached)
        for path in self._candidate_paths():
            if path in paths:
                continue
            envelope = self._read_envelope(path)
            if envelope is None:
                continue
            if (
                envelope["owner_id"] == owner_id
                and envelope["attachment_id"] == attachment_id
            ):
                paths.append(path)
        return paths

    def _candidate_paths(
        self,
        *,
        include_temporary: bool = False,
    ) -> list[Path]:
        try:
            entries = sorted(self.root.iterdir(), key=lambda item: item.name)
        except OSError:
            raise
        def is_managed(name: str) -> bool:
            if _DRAFT_NAME.fullmatch(name) is not None:
                return True
            return (
                include_temporary
                and _TEMP_NAME.fullmatch(name) is not None
            )

        return [
            path
            for path in entries
            if path.is_file()
            and not path.is_symlink()
            and is_managed(path.name)
        ]

    def _read(self, path: Path) -> ExtractionResult | None:
        envelope = self._read_envelope(path)
        if envelope is None:
            return None
        return self._validated_result(envelope)

    @staticmethod
    def _validated_result(
        envelope: dict[str, object],
    ) -> ExtractionResult | None:
        try:
            return ExtractionResult.model_validate(envelope["result"])
        except (TypeError, ValueError, ValidationError):
            return None

    def _read_envelope(self, path: Path) -> dict[str, object] | None:
        try:
            if path.is_symlink():
                return None
            size = path.stat().st_size
            if size <= 0 or size > self.max_bytes:
                return None
            decoded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError, TypeError):
            return None
        if not isinstance(decoded, dict):
            return None
        if decoded.get("schema_version") != 1:
            return None
        try:
            owner_id = _uuid(decoded["owner_id"])
            attachment_id = _uuid(decoded["attachment_id"])
        except (KeyError, ValueError):
            return None
        result = decoded.get("result")
        if not isinstance(result, dict):
            return None
        return {
            "owner_id": owner_id,
            "attachment_id": attachment_id,
            "result": result,
        }

    def _ensure_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)


AttachmentDraftStore = OcrDraftStore


def _validate_root(root: Path) -> None:
    project_root = Path(__file__).resolve().parents[2]
    static_root = project_root / "app" / "web"
    if (
        root == project_root
        or root == static_root
        or root.is_relative_to(static_root)
    ):
        raise ValueError("sidecar 目录必须是私有非静态目录")
    if root.exists() and not root.is_dir():
        raise ValueError("sidecar 目录不能指向普通文件")


def _safe_unlink(path: Path, root: Path) -> bool:
    try:
        if path.parent.resolve() != root:
            return False
        if (
            _DRAFT_NAME.fullmatch(path.name) is None
            and _TEMP_NAME.fullmatch(path.name) is None
        ):
            return False
        path.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def _uuid(value: object) -> str:
    try:
        return str(UUID(str(value)))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError("ID 必须是有效 UUID") from exc
