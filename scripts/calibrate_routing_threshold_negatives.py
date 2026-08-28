"""路由阈值校准 · 第二簇:欠定消息。

第一簇(scripts/calibrate_routing_threshold.py)只量出了「该采信」那一侧:
16 个进入分支的样本全部标注为 should_trust,confidence 0.6~0.9。
那 20 条极模糊的消息模型直接回了 unknown,candidate 是 None,压根没进
分支——所以它们不构成 0.80 的辩护证据。

只凭一簇定阈值就是第 0.55 号阈值踩过的坑(凭印象取值,正好切在合法簇
中间)。本簇专门去找负样本:听起来具体、足以让模型说出一个主题,但消息
本身并不足以确定是哪类纠纷。这类被误采信的代价是拿错主题去给依据和
文案,用户照着做可能白跑一趟。

判定标准不是「模型答得对不对」——欠定消息没有唯一正确答案——而是
「模型有没有在信息不足时仍然给出具体主题」。给了就是负样本。

用法与第一簇相同,结果写到 _threshold_calibration_underdetermined.json。
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sys

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.agent.routing import TopicRegistry  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.playbooks.registry import PlaybookRegistry  # noqa: E402
from app.providers.factory import create_provider  # noqa: E402

# 每条都描述了一个具体动作或损失，但缺少定主题的关键信息:
# 谁扣的钱、什么关系下发生的、争的是哪一笔。模型若给出具体主题，
# 那是在猜。括号里是它至少能落到的几个互不相同的主题。
UNDERDETERMINED: list[str] = [
    "他们把我的钱扣了不给我",  # 押金 / 工资 / 退款
    "我交了钱他们东西一直不给",  # 预付卡 / 网购 / 装修
    "说好的事又反悔了",  # 服务合同 / 劳动 / 租赁
    "我被赶出来了",  # 租赁 / 劳动
    "签完字才发现不对劲",  # 任何合同
    "他把我东西弄坏了赔不赔",  # 相邻 / 租赁 / 一般侵权
    "我想把我的钱要回来",  # 借贷 / 退款 / 押金
    "对方拉黑我了怎么办",  # 借贷 / 网购 / 服务
    "孩子受伤了谁负责",  # 学校 / 交通 / 一般人身
    "他们要罚我钱",  # 劳动 / 服务合同
    "我这个还能退吗",  # 网购 / 预付卡 / 培训
    "东西送来跟说的不一样",  # 网购 / 装修 / 承揽
    "他一直拖着不处理",  # 全部
    "我签的时候没看清条款",  # 全部合同类
    "现在他说这不算数",  # 全部
    "我还能拿回多少",  # 借贷 / 退款 / 赔偿
    "他们说是我自己的问题",  # 全部
    "这个费用是不是不合理",  # 服务 / 物业 / 医疗
    "我当时是口头答应的",  # 租赁 / 借贷 / 劳动
    "对方换人了不认之前的话",  # 服务 / 劳动 / 租赁
]


async def main() -> int:
    settings = get_settings()
    playbooks = PlaybookRegistry.from_directory(settings.playbooks_path)
    topics = TopicRegistry.from_playbooks(playbooks)
    context = topics.provider_context()
    provider = create_provider(settings)

    records: list[dict[str, object]] = []
    for message in UNDERDETERMINED:
        row: dict[str, object] = {
            "message": message,
            "label": "underdetermined",
        }
        try:
            extraction = await provider.extract_facts(message, context)
        except Exception as exc:  # noqa: BLE001
            row["error"] = f"{type(exc).__name__}: {exc}"
            records.append(row)
            continue

        candidate_id = extraction.candidate_topic_id
        candidate = topics._by_id.get(candidate_id)
        row.update(
            {
                "candidate_topic_id": candidate_id,
                "confidence": extraction.confidence,
                "turn_intent": extraction.turn_intent,
                "candidate_score": (
                    candidate.match_score(message)
                    if candidate is not None
                    else None
                ),
                "explicit_match": (
                    getattr(topics.infer_from_text(message), "id", None)
                ),
            }
        )
        records.append(row)
        print(f"{message[:16]} -> {candidate_id} {extraction.confidence}")

    pathlib.Path("_threshold_calibration_underdetermined.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    print(f"wrote {len(records)} records")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
