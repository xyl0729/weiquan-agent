from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.playbooks.loader import iter_playbook_documents, legal_refs  # noqa: E402
from app.retrieval.database import (  # noqa: E402
    connect_database,
    get_statute_by_ref,
    parse_ref,
)


def verify_refs(database_path: Path, playbook_dir: Path) -> list[str]:
    errors: list[str] = []
    connection = connect_database(database_path)
    try:
        playbook_count = 0
        ref_count = 0
        for path, document in iter_playbook_documents(playbook_dir):
            playbook_count += 1
            refs = legal_refs(document, path)
            for ref in refs:
                ref_count += 1
                try:
                    parse_ref(ref)
                except ValueError as exc:
                    errors.append(f"{path.name}: {exc}")
                    continue
                if get_statute_by_ref(connection, ref) is None:
                    errors.append(f"{path.name}: 引用未命中 {ref}")

        if playbook_count == 0:
            errors.append(f"未找到 playbook: {playbook_dir}")
        elif ref_count == 0:
            errors.append("所有 playbook 均未声明 legal_basis")
    finally:
        connection.close()
    return errors


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="校验所有 playbook 法条引用。")
    parser.add_argument(
        "--database",
        type=Path,
        default=PROJECT_ROOT / "data" / "statutes.db",
    )
    parser.add_argument(
        "--playbooks",
        type=Path,
        default=PROJECT_ROOT / "app" / "playbooks",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        errors = verify_refs(args.database, args.playbooks)
    except (FileNotFoundError, OSError, ValueError) as exc:
        print(f"引用校验失败: {exc}", file=sys.stderr)
        return 1

    if errors:
        for error in errors:
            print(f"错误: {error}", file=sys.stderr)
        return 1

    print("引用校验通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

