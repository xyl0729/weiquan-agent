from __future__ import annotations

import argparse
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Sequence
from uuid import UUID


@dataclass(frozen=True, slots=True)
class ObjectInfo:
    key: str
    last_modified: datetime


def select_prunable_groups(
    objects: Sequence[ObjectInfo],
    *,
    keep: int,
    max_age_days: int,
    now: datetime,
) -> list[str]:
    if keep < 0 or max_age_days < 1:
        raise ValueError("invalid retention boundary")
    current = _utc(now)
    groups: dict[str, list[ObjectInfo]] = {}
    for item in objects:
        base = (
            item.key.removesuffix(".sha256")
            if item.key.endswith(".age.sha256")
            else item.key
        )
        groups.setdefault(base, []).append(item)

    ordered = sorted(
        groups.items(),
        key=lambda entry: (
            max(_utc(item.last_modified) for item in entry[1]),
            entry[0],
        ),
        reverse=True,
    )
    cutoff = current - timedelta(days=max_age_days)
    prunable: list[str] = []
    for index, (_, members) in enumerate(ordered):
        newest = max(_utc(item.last_modified) for item in members)
        if index >= keep or newest < cutoff:
            prunable.extend(item.key for item in members)
    return sorted(prunable)


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"missing configuration: {name}")
    return value


def _bucket() -> Any:
    try:
        import oss2

        auth = oss2.Auth(
            _required_env("ALIYUN_ACCESS_KEY_ID"),
            _required_env("ALIYUN_ACCESS_KEY_SECRET"),
        )
        return oss2.Bucket(
            auth,
            _required_env("OSS_ENDPOINT"),
            _required_env("OSS_BUCKET"),
        )
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError("object_store_initialization_failed") from exc


def _safe_key(value: str) -> str:
    normalized = value.strip().replace("\\", "/").lstrip("/")
    parts = normalized.split("/")
    if (
        not normalized
        or len(normalized) > 512
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise ValueError("invalid object key")
    return normalized


def _put(source: Path, key: str) -> None:
    if not source.is_file() or source.stat().st_size <= 0:
        raise RuntimeError("backup_artifact_missing")
    try:
        import oss2

        _bucket().put_object_from_file(
            _safe_key(key),
            str(source),
            headers={"x-oss-object-acl": oss2.OBJECT_ACL_PRIVATE},
        )
    except (RuntimeError, ValueError):
        raise
    except Exception as exc:
        raise RuntimeError("object_upload_failed") from exc


def _get(key: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        _bucket().get_object_to_file(
            _safe_key(key),
            str(destination),
        )
    except (RuntimeError, ValueError):
        raise
    except Exception as exc:
        raise RuntimeError("object_download_failed") from exc
    if not destination.is_file() or destination.stat().st_size <= 0:
        raise RuntimeError("downloaded_object_empty")


def _iter_objects(prefix: str, *, limit: int) -> list[ObjectInfo]:
    if not 1 <= limit <= 10_000:
        raise ValueError("invalid object limit")
    try:
        import oss2

        items: list[ObjectInfo] = []
        for item in oss2.ObjectIterator(
            _bucket(),
            prefix=_safe_key(prefix).rstrip("/") + "/",
            max_keys=min(limit + 1, 1000),
        ):
            items.append(
                ObjectInfo(
                    key=_safe_key(str(item.key)),
                    last_modified=datetime.fromtimestamp(
                        int(item.last_modified),
                        tz=UTC,
                    ),
                )
            )
            if len(items) > limit:
                raise RuntimeError("object_listing_limit_exceeded")
        return items
    except (RuntimeError, ValueError):
        raise
    except Exception as exc:
        raise RuntimeError("object_listing_failed") from exc


def _fetch_prefix(prefix: str, destination: Path, *, limit: int) -> int:
    destination.mkdir(parents=True, exist_ok=True)
    count = 0
    for item in _iter_objects(prefix, limit=limit):
        if not item.key.endswith(".age"):
            continue
        digest = hashlib.sha256(item.key.encode("utf-8")).hexdigest()
        _get(item.key, destination / f"{digest}.age")
        count += 1
    return count


def _prune(
    prefix: str,
    *,
    keep: int,
    max_age_days: int,
    limit: int,
) -> int:
    objects = _iter_objects(prefix, limit=limit)
    keys = select_prunable_groups(
        objects,
        keep=keep,
        max_age_days=max_age_days,
        now=datetime.now(UTC),
    )
    bucket = _bucket()
    try:
        for key in keys:
            bucket.delete_object(key)
    except Exception as exc:
        raise RuntimeError("object_prune_failed") from exc
    return len(keys)


def _parse_deletion_manifest(source: Path) -> tuple[str, str]:
    try:
        payload = json.loads(source.read_text(encoding="ascii"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("deletion_manifest_invalid") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "session_id",
        "deleted_at",
    }:
        raise RuntimeError("deletion_manifest_invalid")
    try:
        session_id = str(UUID(str(payload["session_id"])))
        deleted_at = str(payload["deleted_at"])
        timestamp = datetime.fromisoformat(
            deleted_at.replace("Z", "+00:00")
        )
        timestamp = _utc(timestamp)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("deletion_manifest_invalid") from exc
    if timestamp > datetime.now(UTC) + timedelta(minutes=5):
        raise RuntimeError("deletion_manifest_invalid")
    return session_id, timestamp.isoformat().replace("+00:00", "Z")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone required")
    return value.astimezone(UTC)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subcommands = parser.add_subparsers(dest="command", required=True)

    put = subcommands.add_parser("put")
    put.add_argument("--source", type=Path, required=True)
    put.add_argument("--key", required=True)

    get = subcommands.add_parser("get")
    get.add_argument("--key", required=True)
    get.add_argument("--destination", type=Path, required=True)

    fetch = subcommands.add_parser("fetch-prefix")
    fetch.add_argument("--prefix", required=True)
    fetch.add_argument("--destination", type=Path, required=True)
    fetch.add_argument("--limit", type=int, default=1000)

    prune = subcommands.add_parser("prune")
    prune.add_argument("--prefix", required=True)
    prune.add_argument("--keep", type=int, required=True)
    prune.add_argument("--max-age-days", type=int, required=True)
    prune.add_argument("--limit", type=int, default=1000)

    manifest = subcommands.add_parser("parse-deletion-manifest")
    manifest.add_argument("--source", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.command == "put":
        _put(arguments.source, arguments.key)
    elif arguments.command == "get":
        _get(arguments.key, arguments.destination)
    elif arguments.command == "fetch-prefix":
        _fetch_prefix(
            arguments.prefix,
            arguments.destination,
            limit=arguments.limit,
        )
    elif arguments.command == "prune":
        _prune(
            arguments.prefix,
            keep=arguments.keep,
            max_age_days=arguments.max_age_days,
            limit=arguments.limit,
        )
    elif arguments.command == "parse-deletion-manifest":
        session_id, deleted_at = _parse_deletion_manifest(
            arguments.source
        )
        print(f"{session_id}\t{deleted_at}")
    else:
        raise RuntimeError("unsupported command")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
