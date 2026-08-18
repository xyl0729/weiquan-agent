from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from app.agent.errors import NewWorkPausedError
from app.config import Settings


@dataclass(frozen=True, slots=True)
class DiskCapacity:
    available: bool
    used_percent: float
    free_bytes: int


def disk_capacity(settings: Settings) -> DiskCapacity:
    target = _existing_path(settings.attachment_temp_path)
    usage = shutil.disk_usage(target)
    used_percent = (
        100.0
        if usage.total <= 0
        else ((usage.total - usage.free) / usage.total) * 100
    )
    return DiskCapacity(
        available=(
            used_percent < settings.disk_max_used_percent
            and usage.free >= settings.disk_min_free_bytes
        ),
        used_percent=used_percent,
        free_bytes=usage.free,
    )


def temp_directory_ready(settings: Settings) -> bool:
    path = settings.attachment_temp_path
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    return path.is_dir() and os.access(path, os.R_OK | os.W_OK)


def require_new_work_capacity(settings: Settings) -> None:
    if not settings.new_work_enabled:
        raise NewWorkPausedError()
    if settings.deployment_mode != "production":
        return
    try:
        ready = temp_directory_ready(settings)
        capacity = disk_capacity(settings)
    except OSError:
        ready = False
        capacity = None
    if not ready or capacity is None or not capacity.available:
        raise NewWorkPausedError()


def _existing_path(path: Path) -> Path:
    candidate = path.resolve(strict=False)
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate
