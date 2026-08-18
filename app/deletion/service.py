from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from app.agent.errors import SafeApplicationError
from app.integrations.oss import PrivateObjectStore


class DeletionUnavailableError(SafeApplicationError):
    def __init__(self) -> None:
        super().__init__(
            "deletion_unavailable",
            "删除服务暂时不可用，请稍后重试",
        )


@dataclass(frozen=True, slots=True)
class DeletionIntent:
    session_id: str
    deleted_at: datetime
    manifest_uploaded_at: datetime | None = None
    completed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class DeletionManifest:
    session_id: str
    deleted_at: datetime

    @classmethod
    def from_intent(cls, intent: DeletionIntent) -> "DeletionManifest":
        return cls(
            session_id=intent.session_id,
            deleted_at=intent.deleted_at,
        )

    def to_json_bytes(self) -> bytes:
        payload = {
            "session_id": self.session_id,
            "deleted_at": _manifest_timestamp(self.deleted_at),
        }
        return json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("ascii")


class DeletionRepository(Protocol):
    def begin_session_deletion(
        self,
        session_id: str,
        *,
        owner_id: str,
        deleted_at: datetime,
    ) -> DeletionIntent | None: ...

    def mark_deletion_manifest_uploaded(
        self,
        session_id: str,
        *,
        deleted_at: datetime,
        uploaded_at: datetime,
    ) -> DeletionIntent: ...

    def complete_session_deletion(
        self,
        session_id: str,
        *,
        deleted_at: datetime,
        completed_at: datetime,
    ) -> bool: ...

    def record_deletion_failure(
        self,
        session_id: str,
        *,
        category: str,
        attempted_at: datetime,
    ) -> None: ...

    def list_pending_deletions(
        self,
        *,
        limit: int,
    ) -> list[DeletionIntent]: ...


class ManifestEncryptor(Protocol):
    def encrypt(self, plaintext: bytes) -> bytes: ...


class AgeCliEncryptor:
    def __init__(
        self,
        recipient: str,
        *,
        executable: str = "age",
        timeout_seconds: float = 15,
    ) -> None:
        self.recipient = recipient.strip()
        self.executable = executable.strip()
        self.timeout_seconds = float(timeout_seconds)
        if not self.recipient or not self.executable:
            raise ValueError("删除清单加密配置不能为空")
        if self.timeout_seconds <= 0:
            raise ValueError("删除清单加密超时必须大于零")

    def encrypt(self, plaintext: bytes) -> bytes:
        try:
            result = subprocess.run(
                [
                    self.executable,
                    "--encrypt",
                    "--recipient",
                    self.recipient,
                ],
                input=bytes(plaintext),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=self.timeout_seconds,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError("删除清单加密失败") from exc
        if result.returncode != 0 or not result.stdout:
            raise RuntimeError("删除清单加密失败")
        return bytes(result.stdout)


class DeletionService:
    def __init__(
        self,
        *,
        repository: DeletionRepository,
        encryptor: ManifestEncryptor,
        object_store: PrivateObjectStore,
        now: Callable[[], datetime] | None = None,
        object_prefix: str = "deletion-manifests",
    ) -> None:
        prefix = object_prefix.strip().strip("/")
        if not prefix:
            raise ValueError("删除清单对象前缀不能为空")
        self.repository = repository
        self.encryptor = encryptor
        self.object_store = object_store
        self._now = now or (lambda: datetime.now(UTC))
        self.object_prefix = prefix

    def delete_session(
        self,
        session_id: str,
        *,
        owner_id: str,
    ) -> bool:
        current = _utc(self._now())
        try:
            intent = self.repository.begin_session_deletion(
                session_id,
                owner_id=owner_id,
                deleted_at=current,
            )
        except Exception as exc:
            raise DeletionUnavailableError() from exc
        if intent is None:
            return False
        self._complete_intent(intent)
        return True

    def resume_pending(self, *, limit: int) -> int:
        _bounded_limit(limit)
        try:
            pending = self.repository.list_pending_deletions(limit=limit)
        except Exception as exc:
            raise DeletionUnavailableError() from exc
        completed = 0
        for intent in pending:
            try:
                self._complete_intent(intent)
            except DeletionUnavailableError:
                continue
            completed += 1
        return completed

    def _complete_intent(self, intent: DeletionIntent) -> None:
        current = _utc(self._now())
        active = intent
        if active.completed_at is not None:
            return
        if active.manifest_uploaded_at is None:
            manifest = DeletionManifest.from_intent(active).to_json_bytes()
            try:
                encrypted = self.encryptor.encrypt(manifest)
            except Exception as exc:
                self._record_failure(
                    active,
                    category="encryption_failed",
                    attempted_at=current,
                )
                raise DeletionUnavailableError() from exc
            try:
                self.object_store.put_private_object(
                    self._object_key(active, manifest),
                    encrypted,
                    content_type="application/octet-stream",
                )
            except Exception as exc:
                self._record_failure(
                    active,
                    category="upload_failed",
                    attempted_at=current,
                )
                raise DeletionUnavailableError() from exc
            try:
                active = self.repository.mark_deletion_manifest_uploaded(
                    active.session_id,
                    deleted_at=active.deleted_at,
                    uploaded_at=current,
                )
            except Exception as exc:
                self._record_failure(
                    active,
                    category="storage_failed",
                    attempted_at=current,
                )
                raise DeletionUnavailableError() from exc
        try:
            completed = self.repository.complete_session_deletion(
                active.session_id,
                deleted_at=active.deleted_at,
                completed_at=current,
            )
        except Exception as exc:
            self._record_failure(
                active,
                category="storage_failed",
                attempted_at=current,
            )
            raise DeletionUnavailableError() from exc
        if not completed:
            self._record_failure(
                active,
                category="storage_failed",
                attempted_at=current,
            )
            raise DeletionUnavailableError()

    def _record_failure(
        self,
        intent: DeletionIntent,
        *,
        category: str,
        attempted_at: datetime,
    ) -> None:
        try:
            self.repository.record_deletion_failure(
                intent.session_id,
                category=category,
                attempted_at=attempted_at,
            )
        except Exception:
            pass

    def _object_key(
        self,
        intent: DeletionIntent,
        manifest: bytes,
    ) -> str:
        deleted_at = _utc(intent.deleted_at)
        digest = hashlib.sha256(
            b"weiquan-deletion-manifest-v1\x00" + manifest
        ).hexdigest()
        return (
            f"{self.object_prefix}/"
            f"{deleted_at:%Y/%m/%d}/{digest}.age"
        )


def _manifest_timestamp(value: datetime) -> str:
    utc = _utc(value)
    if utc.microsecond:
        encoded = utc.isoformat(timespec="microseconds")
    else:
        encoded = utc.isoformat(timespec="seconds")
    return encoded.replace("+00:00", "Z")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("删除时间必须包含时区")
    return value.astimezone(UTC)


def _bounded_limit(limit: int) -> int:
    normalized = int(limit)
    if not 1 <= normalized <= 1000:
        raise ValueError("limit 必须在 1 到 1000 之间")
    return normalized
