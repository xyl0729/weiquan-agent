from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pydantic import ValidationError

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.retrieval.benchmark import (  # noqa: E402
    DataGateError,
    assert_data_gate_ready,
    evaluate_recall_at_5,
    load_benchmark,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="执行法条检索 Recall@5 门禁。")
    parser.add_argument(
        "--benchmark",
        type=Path,
        default=PROJECT_ROOT / "data" / "retrieval_benchmark.yaml",
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=PROJECT_ROOT / "data" / "statutes.db",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        benchmark = load_benchmark(args.benchmark)
        assert_data_gate_ready(benchmark, args.database)
        report = evaluate_recall_at_5(benchmark, args.database)
    except (
        DataGateError,
        FileNotFoundError,
        OSError,
        ValueError,
        ValidationError,
    ) as exc:
        print(f"召回门禁未通过: {exc}", file=sys.stderr)
        return 1

    print(
        f"Recall@5={report.recall_at_5:.3f} "
        f"({report.passed_cases}/{report.case_count} 样本完全命中)"
    )
    if report.recall_at_5 < benchmark.minimum_recall_at_5:
        for case_id, refs in report.missed.items():
            print(f"未命中 {case_id}: {', '.join(refs)}", file=sys.stderr)
        print(
            f"召回门禁未通过: 要求 >= {benchmark.minimum_recall_at_5:.2f}",
            file=sys.stderr,
        )
        return 1
    print("召回门禁通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

