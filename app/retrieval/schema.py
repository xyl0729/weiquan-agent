from __future__ import annotations

from datetime import date
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator


class StatuteSeed(BaseModel):
    model_config = ConfigDict(extra="forbid")

    law_name: str = Field(min_length=1)
    law_short: str = Field(min_length=1)
    article_no: str = Field(min_length=1)
    article_num: int = Field(ge=1)
    content: str = Field(min_length=1)
    chapter: str | None = None
    effective_date: date
    source_url: HttpUrl

    @property
    def ref(self) -> str:
        return f"{self.law_short}.{self.article_no}"


class SeedBundle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    placeholder_data: bool = False
    warning: str = ""
    statutes: list[StatuteSeed] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_bundle(self) -> "SeedBundle":
        if self.placeholder_data and not self.warning.strip():
            raise ValueError("占位数据必须提供 warning")

        seen: set[tuple[str, str]] = set()
        duplicate_refs: list[str] = []
        for statute in self.statutes:
            key = (statute.law_name, statute.article_no)
            if key in seen:
                duplicate_refs.append(statute.ref)
            seen.add(key)

        if duplicate_refs:
            joined = ", ".join(sorted(set(duplicate_refs)))
            raise ValueError(f"法条记录重复: {joined}")
        return self


def load_seed_bundle(path: Path) -> SeedBundle:
    if not path.is_file():
        raise FileNotFoundError(f"法条种子文件不存在: {path}")

    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    if not isinstance(raw, dict):
        raise ValueError("法条种子文件根节点必须是对象")
    return SeedBundle.model_validate(raw)

