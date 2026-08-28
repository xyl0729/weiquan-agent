"""路由阈值校准探针。

`app/agent/routing.py` 的 `semantic_confident` 用 `max(min_confidence, 0.80)`
判定「模型说了个主题，但该主题的关键词匹配对这条消息得分为 0，还要不要
信模型」。0.80 从未用真实数据校准过。

本脚本按第 0.40 号阈值的确定方法做：跑真实模型抽取，量出两簇 confidence
分布，取簇间空隙，把数据写进注释。不凭印象定。

用法：
    .venv/Scripts/python.exe scripts/calibrate_routing_threshold.py

结果写到 _threshold_calibration.json 和 _threshold_calibration.txt
（终端中文乱码，别看 stdout）。
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

# 语料分两簇，标注的是「模型给出的主题该不该被采信」。
#
# should_trust：口语表述、没有命中主题关键词，但案情本身指向明确。
#   这类正是查询扩展要救的场景——用户不会说「租赁物危及健康」，
#   他说「住进去就头痛」。这簇被误拒的代价是掉回未核验路径。
#
# should_drop：信息量不足以定主题的话。用户只说了情绪或极模糊的处境，
#   任何具体主题都是模型在猜。这簇被误采信的代价更大：拿着错主题去
#   给依据和文案，用户照着做可能白跑一趟。
PROBES: list[tuple[str, str]] = [
    # ---- should_trust ----
    ("住进去就头痛咳嗽，屋里味儿特别冲", "should_trust"),
    ("房东收了钱一直不给我修，都两个月了", "should_trust"),
    ("我押金到现在都没还我", "should_trust"),
    ("干了三个月一分钱没拿到", "should_trust"),
    ("公司说不要我了，让我明天别来了", "should_trust"),
    ("邻居家那条狗把我腿咬破了", "should_trust"),
    ("在商场地上滑倒摔断了胳膊", "should_trust"),
    ("健身房关门跑了，卡里还有八千", "should_trust"),
    ("快递说给我送到了，其实东西根本没影", "should_trust"),
    ("外卖吃完上吐下泻进医院了", "should_trust"),
    ("医院死活不给我复印病历", "should_trust"),
    ("对方全责撞了我的车，现在不认账", "should_trust"),
    ("借给他的钱说好半年还，现在人躲着不见", "should_trust"),
    ("天天有人打电话到我单位说我欠钱", "should_trust"),
    ("我照片被人发到群里配了难听的话", "should_trust"),
    ("孩子在学校被同学推下楼梯摔了", "should_trust"),
    ("离婚了他一分抚养费都不给", "should_trust"),
    ("游戏号突然被封了，里面充了不少钱", "should_trust"),
    ("楼上装修把我家天花板砸漏了", "should_trust"),
    ("领导总在办公室说些让我很不舒服的话", "should_trust"),
    # ---- should_drop ----
    ("我遇到点事，心里挺乱的", "should_drop"),
    ("这事该怎么办啊", "should_drop"),
    ("他们太欺负人了", "should_drop"),
    ("我想问一下我的情况", "should_drop"),
    ("有点麻烦，不知道找谁", "should_drop"),
    ("你能帮我吗", "should_drop"),
    ("最近碰到一些不顺心的事", "should_drop"),
    ("我觉得他们做得不对", "should_drop"),
    ("想咨询个问题", "should_drop"),
    ("这种情况正常吗", "should_drop"),
    ("我朋友让我来问问", "should_drop"),
    ("对方一直不回复我", "should_drop"),
    ("我该准备些什么", "should_drop"),
    ("这样下去我很被动", "should_drop"),
    ("我想知道我有没有道理", "should_drop"),
    ("事情已经拖很久了", "should_drop"),
    ("我不太懂这些", "should_drop"),
    ("需要花很多钱吗", "should_drop"),
    ("能不能给点建议", "should_drop"),
    ("我还有别的办法吗", "should_drop"),
]


async def main() -> int:
    settings = get_settings()
    playbooks = PlaybookRegistry.from_directory(settings.playbooks_path)
    topics = TopicRegistry.from_playbooks(playbooks)
    context = topics.provider_context()
    provider = create_provider(settings)

    records: list[dict[str, object]] = []
    for message, label in PROBES:
        row: dict[str, object] = {"message": message, "label": label}
        try:
            extraction = await provider.extract_facts(message, context)
        except Exception as exc:  # noqa: BLE001 - 探针要记下失败而不是中断
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
                "risk_flags": list(extraction.risk_flags),
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
        print(f"{message[:20]} -> {candidate_id} {extraction.confidence}")

    pathlib.Path("_threshold_calibration.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    print(f"wrote {len(records)} records")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
