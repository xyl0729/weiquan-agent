from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.retrieval.database import connect_database, get_metadata
from app.retrieval.search import search_connection


class RecallCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    query: str = Field(min_length=1)
    expected_refs: list[str] = Field(min_length=1)


class RecallBenchmark(BaseModel):
    model_config = ConfigDict(extra="forbid")

    placeholder_data: bool = False
    warning: str = ""
    minimum_cases: int = Field(default=50, ge=50)
    minimum_recall_at_5: float = Field(default=0.9, ge=0, le=1)
    cases: list[RecallCase]

    @model_validator(mode="after")
    def validate_unique_ids(self) -> "RecallBenchmark":
        ids = [case.id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("召回测试样本 id 不得重复")
        return self


@dataclass(frozen=True, slots=True)
class RecallReport:
    case_count: int
    passed_cases: int
    recall_at_5: float
    missed: dict[str, list[str]]


class DataGateError(RuntimeError):
    pass


def load_benchmark(path: Path) -> RecallBenchmark:
    if not path.is_file():
        raise FileNotFoundError(f"召回测试集不存在: {path}")
    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    if not isinstance(raw, dict):
        raise ValueError("召回测试集根节点必须是对象")
    return RecallBenchmark.model_validate(raw)


def assert_data_gate_ready(
    benchmark: RecallBenchmark,
    database_path: Path,
) -> None:
    reasons: list[str] = []
    if benchmark.placeholder_data:
        reasons.append("召回测试集仍标记为占位数据")
    if len(benchmark.cases) < benchmark.minimum_cases:
        reasons.append(
            f"召回测试样本仅 {len(benchmark.cases)} 条，"
            f"至少需要 {benchmark.minimum_cases} 条"
        )

    connection = connect_database(database_path)
    try:
        metadata = get_metadata(connection)
    finally:
        connection.close()
    if metadata.get("placeholder_data") != "false":
        reasons.append("法条数据库仍由占位数据生成")

    if reasons:
        raise DataGateError("；".join(reasons))


def evaluate_recall_at_5(
    benchmark: RecallBenchmark,
    database_path: Path,
) -> RecallReport:
    connection = connect_database(database_path)
    total_recall = 0.0
    passed_cases = 0
    missed: dict[str, list[str]] = {}
    try:
        for case in benchmark.cases:
            retrieved = {
                hit.statute.ref
                for hit in search_connection(connection, case.query, limit=5)
            }
            expected = set(case.expected_refs)
            matched = expected & retrieved
            case_recall = len(matched) / len(expected)
            total_recall += case_recall
            if case_recall == 1.0:
                passed_cases += 1
            else:
                missed[case.id] = sorted(expected - retrieved)
    finally:
        connection.close()

    case_count = len(benchmark.cases)
    recall = total_recall / case_count if case_count else 0.0
    return RecallReport(
        case_count=case_count,
        passed_cases=passed_cases,
        recall_at_5=recall,
        missed=missed,
    )

