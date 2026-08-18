from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.deletion.service import (
    DeletionIntent,
    DeletionManifest,
    DeletionService,
    DeletionUnavailableError,
)
from app.integrations.oss import (
    InMemoryPrivateObjectStore,
    ObjectStorageError,
)


NOW = datetime(2026, 8, 10, 8, 30, 45, tzinfo=UTC)


class RecordingRepository:
    def __init__(
        self,
        intent: DeletionIntent,
        *,
        complete_result: bool = True,
    ) -> None:
        self.intent = intent
        self.complete_result = complete_result
        self.hidden = False
        self.completed = False
        self.events: list[str] = []
        self.failure_categories: list[str] = []

    def begin_session_deletion(
        self,
        session_id: str,
        *,
        owner_id: str,
        deleted_at: datetime,
    ) -> DeletionIntent | None:
        del owner_id
        assert session_id == self.intent.session_id
        assert deleted_at == self.intent.deleted_at
        self.hidden = True
        self.events.append("hidden")
        return self.intent

    def mark_deletion_manifest_uploaded(
        self,
        session_id: str,
        *,
        deleted_at: datetime,
        uploaded_at: datetime,
    ) -> DeletionIntent:
        assert session_id == self.intent.session_id
        assert deleted_at == self.intent.deleted_at
        self.events.append("uploaded")
        self.intent = DeletionIntent(
            session_id=session_id,
            deleted_at=deleted_at,
            manifest_uploaded_at=uploaded_at,
            completed_at=None,
        )
        return self.intent

    def complete_session_deletion(
        self,
        session_id: str,
        *,
        deleted_at: datetime,
        completed_at: datetime,
    ) -> bool:
        del completed_at
        assert session_id == self.intent.session_id
        assert deleted_at == self.intent.deleted_at
        assert self.intent.manifest_uploaded_at is not None
        self.completed = self.complete_result
        self.events.append("completed")
        return self.complete_result

    def record_deletion_failure(
        self,
        session_id: str,
        *,
        category: str,
        attempted_at: datetime,
    ) -> None:
        del attempted_at
        assert session_id == self.intent.session_id
        self.failure_categories.append(category)

    def list_pending_deletions(
        self,
        *,
        limit: int,
    ) -> list[DeletionIntent]:
        assert limit > 0
        return [] if self.completed else [self.intent]


class RecordingEncryptor:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.plaintexts: list[bytes] = []

    def encrypt(self, plaintext: bytes) -> bytes:
        self.plaintexts.append(plaintext)
        if self.error is not None:
            raise self.error
        return b"encrypted:" + plaintext


class FailingObjectStore(InMemoryPrivateObjectStore):
    def put_private_object(
        self,
        object_key: str,
        payload: bytes,
        *,
        content_type: str,
    ) -> None:
        del object_key, payload, content_type
        raise ObjectStorageError()


def _intent() -> DeletionIntent:
    return DeletionIntent(
        session_id=str(uuid4()),
        deleted_at=NOW,
    )


def test_manifest_contains_only_session_id_and_deleted_at() -> None:
    intent = _intent()

    encoded = DeletionManifest.from_intent(intent).to_json_bytes()
    payload = json.loads(encoded)

    assert payload == {
        "session_id": intent.session_id,
        "deleted_at": "2026-08-10T08:30:45Z",
    }
    assert set(payload) == {"session_id", "deleted_at"}


def test_delete_uploads_encrypted_manifest_before_hard_delete() -> None:
    intent = _intent()
    repository = RecordingRepository(intent)
    encryptor = RecordingEncryptor()
    objects = InMemoryPrivateObjectStore()
    service = DeletionService(
        repository=repository,
        encryptor=encryptor,
        object_store=objects,
        now=lambda: NOW,
    )

    deleted = service.delete_session(
        intent.session_id,
        owner_id=str(uuid4()),
    )

    assert deleted
    assert repository.hidden
    assert repository.completed
    assert repository.events == ["hidden", "uploaded", "completed"]
    assert len(objects.objects) == 1
    stored = next(iter(objects.objects.values()))
    assert stored.payload.startswith(b"encrypted:")
    assert stored.content_type == "application/octet-stream"
    assert stored.private is True
    assert intent.session_id.encode() not in stored.payload.split(
        b"encrypted:", 1
    )[0]


@pytest.mark.parametrize(
    ("encrypt_error", "object_store", "category"),
    [
        (
            RuntimeError("encryption secret detail"),
            InMemoryPrivateObjectStore(),
            "encryption_failed",
        ),
        (
            None,
            FailingObjectStore(),
            "upload_failed",
        ),
    ],
)
def test_failure_never_reports_success_and_session_stays_hidden(
    encrypt_error: Exception | None,
    object_store: InMemoryPrivateObjectStore,
    category: str,
) -> None:
    intent = _intent()
    repository = RecordingRepository(intent)
    service = DeletionService(
        repository=repository,
        encryptor=RecordingEncryptor(error=encrypt_error),
        object_store=object_store,
        now=lambda: NOW,
    )

    with pytest.raises(DeletionUnavailableError) as caught:
        service.delete_session(
            intent.session_id,
            owner_id=str(uuid4()),
        )

    assert repository.hidden
    assert not repository.completed
    assert repository.failure_categories == [category]
    assert "secret detail" not in str(caught.value)


def test_pending_outbox_can_resume_after_upload_before_hard_delete() -> None:
    intent = DeletionIntent(
        session_id=str(uuid4()),
        deleted_at=NOW,
        manifest_uploaded_at=NOW,
    )
    repository = RecordingRepository(intent)
    encryptor = RecordingEncryptor()
    objects = InMemoryPrivateObjectStore()
    service = DeletionService(
        repository=repository,
        encryptor=encryptor,
        object_store=objects,
        now=lambda: NOW,
    )

    processed = service.resume_pending(limit=10)

    assert processed == 1
    assert repository.events == ["completed"]
    assert encryptor.plaintexts == []
    assert objects.objects == {}


def test_hard_delete_rejection_never_reports_success() -> None:
    intent = DeletionIntent(
        session_id=str(uuid4()),
        deleted_at=NOW,
        manifest_uploaded_at=NOW,
    )
    repository = RecordingRepository(intent, complete_result=False)
    service = DeletionService(
        repository=repository,
        encryptor=RecordingEncryptor(),
        object_store=InMemoryPrivateObjectStore(),
        now=lambda: NOW,
    )

    with pytest.raises(DeletionUnavailableError):
        service.delete_session(
            intent.session_id,
            owner_id=str(uuid4()),
        )

    assert not repository.completed
    assert repository.failure_categories == ["storage_failed"]
