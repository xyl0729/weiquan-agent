from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class ObjectStorageError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("对象存储暂时不可用")


class PrivateObjectStore(Protocol):
    def put_private_object(
        self,
        object_key: str,
        payload: bytes,
        *,
        content_type: str,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class StoredPrivateObject:
    payload: bytes
    content_type: str
    private: bool = True


class InMemoryPrivateObjectStore:
    def __init__(self) -> None:
        self.objects: dict[str, StoredPrivateObject] = {}

    def put_private_object(
        self,
        object_key: str,
        payload: bytes,
        *,
        content_type: str,
    ) -> None:
        key = _object_key(object_key)
        self.objects[key] = StoredPrivateObject(
            payload=bytes(payload),
            content_type=_content_type(content_type),
        )


class AliyunPrivateObjectStore:
    def __init__(self, bucket: Any) -> None:
        self._bucket = bucket

    @classmethod
    def from_credentials(
        cls,
        *,
        access_key_id: str,
        access_key_secret: str,
        endpoint: str,
        bucket_name: str,
    ) -> "AliyunPrivateObjectStore":
        try:
            import oss2

            auth = oss2.Auth(access_key_id, access_key_secret)
            bucket = oss2.Bucket(auth, endpoint, bucket_name)
        except Exception as exc:
            raise ObjectStorageError() from exc
        return cls(bucket)

    def put_private_object(
        self,
        object_key: str,
        payload: bytes,
        *,
        content_type: str,
    ) -> None:
        key = _object_key(object_key)
        media_type = _content_type(content_type)
        try:
            import oss2

            self._bucket.put_object(
                key,
                bytes(payload),
                headers={
                    "Content-Type": media_type,
                    "x-oss-object-acl": oss2.OBJECT_ACL_PRIVATE,
                },
            )
        except Exception as exc:
            raise ObjectStorageError() from exc


def _object_key(value: str) -> str:
    normalized = value.strip().replace("\\", "/")
    parts = normalized.split("/")
    if (
        not normalized
        or normalized.startswith("/")
        or any(part in {"", ".", ".."} for part in parts)
        or len(normalized) > 512
    ):
        raise ValueError("OSS 对象键无效")
    return normalized


def _content_type(value: str) -> str:
    normalized = value.strip().casefold()
    if (
        not normalized
        or len(normalized) > 100
        or any(character.isspace() for character in normalized)
    ):
        raise ValueError("对象 Content-Type 无效")
    return normalized

