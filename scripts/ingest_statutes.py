from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pydantic import ValidationError

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.retrieval.database import rebuild_database  # noqa: E402
from app.retrieval.schema import load_seed_bundle  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="将人工核对的 YAML 法条数据离线写入 SQLite。",
    )
    parser.add_argument(
        "--seed",
        type=Path,
        default=PROJECT_ROOT / "data" / "seed_statutes.yaml",
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
        bundle = load_seed_bundle(args.seed)
        count = rebuild_database(bundle, args.database)
    except (FileNotFoundError, OSError, ValueError, ValidationError) as exc:
        print(f"入库失败: {exc}", file=sys.stderr)
        return 1

    label = "占位" if bundle.placeholder_data else "已核对"
    print(f"入库完成: {count} 条（{label}数据） -> {args.database.resolve()}")
    if bundle.placeholder_data:
        print(f"警告: {bundle.warning}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

