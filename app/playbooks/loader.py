from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

import yaml


def iter_playbook_documents(
    directory: Path,
) -> Iterator[tuple[Path, dict[str, Any]]]:
    if not directory.is_dir():
        raise FileNotFoundError(f"playbook 目录不存在: {directory}")

    for path in sorted(directory.glob("*.yaml")):
        with path.open("r", encoding="utf-8") as handle:
            document = yaml.safe_load(handle)
        if not isinstance(document, dict):
            raise ValueError(f"playbook 根节点必须是对象: {path}")
        yield path, document


def legal_refs(document: dict[str, Any], path: Path) -> list[str]:
    raw_basis = document.get("legal_basis", [])
    if not isinstance(raw_basis, list):
        raise ValueError(f"legal_basis 必须是列表: {path}")

    refs: list[str] = []
    for index, item in enumerate(raw_basis):
        if not isinstance(item, dict) or not isinstance(item.get("ref"), str):
            raise ValueError(f"legal_basis[{index}].ref 缺失或格式错误: {path}")
        refs.append(item["ref"].strip())
    return refs

