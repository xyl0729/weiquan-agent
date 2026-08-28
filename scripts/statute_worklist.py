"""生成待录入法条的人工核验清单。

这个脚本刻意不抓取法条正文，也不写数据库。它读 data/statute_wishlist.yaml
里「案情需要但库里还没有」的条目，剔除已经入库的，然后输出一份带官方
详情页链接的 Markdown 清单，供人工逐条录入核验。

为什么不自动抓正文：一条错误的法条在维权场景里比没有法条更糟——用户会
照着它去跟房东、商家或单位交涉。如果由脚本抓取、比对并落库，那么
placeholder_data 这道门禁就变成了自我认证，人工核验只剩形式。所以正文
录入和最终签署始终留给人。

为什么需要这份清单：扫描代码找不出这类缺口。缺失的条文正因为不在库里，
代码也无从引用——实测代码和 playbook 引用的 80 条全部在库，缺失为 0，
而甲醛案真正需要的第七百三十一条压根没被任何代码提到。

用法：
    python scripts/statute_worklist.py
    python scripts/statute_worklist.py --output docs/statute-worklist.md
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.retrieval.database import (  # noqa: E402
    connect_database,
    get_statute_by_ref,
    parse_ref,
)

_REQUIRED_FIELDS = (
    "ref",
    "law_name",
    "source_url",
    "topic",
    "why",
    "current_fallback",
)


def load_wishlist(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise FileNotFoundError(f"待录入清单不存在: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("待录入清单根节点必须是对象")
    entries = raw.get("statutes")
    if not isinstance(entries, list) or not entries:
        raise ValueError("待录入清单缺少 statutes 列表")

    seen: set[str] = set()
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise ValueError(f"第 {index} 条不是对象")
        missing = [name for name in _REQUIRED_FIELDS if not entry.get(name)]
        if missing:
            raise ValueError(
                f"第 {index} 条缺少字段: {', '.join(missing)}"
            )
        ref = str(entry["ref"])
        # 引用格式必须和检索层一致，否则录入后取不到。
        parse_ref(ref)
        if ref in seen:
            raise ValueError(f"待录入清单存在重复引用: {ref}")
        seen.add(ref)
    return entries


def split_by_presence(
    entries: list[dict[str, Any]],
    database_path: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """按是否已入库拆分清单。"""
    connection = connect_database(database_path)
    try:
        pending: list[dict[str, Any]] = []
        done: list[dict[str, Any]] = []
        for entry in entries:
            target = done if get_statute_by_ref(
                connection, str(entry["ref"])
            ) else pending
            target.append(entry)
    finally:
        connection.close()
    return pending, done


def _clean(value: Any) -> str:
    return " ".join(str(value).split())


def render(
    pending: list[dict[str, Any]],
    done: list[dict[str, Any]],
) -> str:
    lines: list[str] = [
        "# 待录入法条核验清单",
        "",
        "本文件由 `scripts/statute_worklist.py` 生成，请勿手工编辑；",
        "要增删条目请改 `data/statute_wishlist.yaml` 后重新生成。",
        "",
        "录入步骤（每条都要走完）：",
        "",
        "1. 打开该法规的官方详情页，确认法规状态为有效。",
        "2. 找到对应条号，逐字复制完整正文，不要改写、不要省略款项。",
        "3. 核对法律全称、简称、条号、数字序号、章节和施行日期。",
        "4. 把条目补进 `data/seed_statutes.yaml`，字段与既有条目保持一致。",
        "5. 全部录入完成并复核后，运行 `python scripts/ingest_statutes.py`。",
        "6. 在 `docs/legal-data-verification-checklist.md` 补记本次核验。",
        "",
        "> 正文必须人工从官方详情页录入。脚本不抓正文也不写库，",
        "> 因为一条录错的法条会被用户直接拿去跟房东、商家或单位交涉。",
        "",
        f"待录入 {len(pending)} 条；已入库 {len(done)} 条。",
        "",
    ]

    if pending:
        lines += ["## 待录入", ""]
        for entry in pending:
            lines += [
                f"### `{entry['ref']}`",
                "",
                f"- 法律全称：{_clean(entry['law_name'])}",
                f"- 官方详情页：{entry['source_url']}",
                f"- 录入后挂到主题：`{entry['topic']}`",
                f"- 为什么需要：{_clean(entry['why'])}",
                f"- 目前的次优解：{_clean(entry['current_fallback'])}",
                "- [ ] 已从官方详情页逐字录入并核对全部字段",
                "",
            ]

    if done:
        lines += [
            "## 已入库",
            "",
            "以下条目已在已核验法条库中，可从 wishlist 移除。",
            "",
        ]
        lines += [f"- [x] `{entry['ref']}`" for entry in done]
        lines.append("")

    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="生成待录入法条的人工核验清单（不抓正文、不写库）。",
    )
    parser.add_argument(
        "--wishlist",
        type=Path,
        default=PROJECT_ROOT / "data" / "statute_wishlist.yaml",
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=PROJECT_ROOT / "data" / "statutes.db",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="写入路径；省略时打印到标准输出。",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        entries = load_wishlist(args.wishlist)
        pending, done = split_by_presence(entries, args.database)
    except (FileNotFoundError, OSError, ValueError) as exc:
        print(f"生成清单失败: {exc}", file=sys.stderr)
        return 1

    document = render(pending, done)
    if args.output is None:
        sys.stdout.write(document)
    else:
        args.output.write_text(document, encoding="utf-8")
        print(
            f"已写入 {args.output}（待录入 {len(pending)} 条，"
            f"已入库 {len(done)} 条）"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
