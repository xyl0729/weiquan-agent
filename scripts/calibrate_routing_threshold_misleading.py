"""路由阈值校准 · 第三簇:表面像 A、实则是 B。

前两簇的结论是:进入 `semantic_confident` 分支的样本里,没有一条
「有数值 confidence 且不该采信」——模型对信息不足的消息会自己回
unknown 或不给 confidence,轮不到 0.80 去挡。

但那还不足以判 0.80 无用。剩下的关键问题是:模型会不会在**答错**的
时候仍然自信?本簇专门造这种消息:字面强烈指向一个主题,实际争点是
另一个。这正是 BM25 判不出来的那类(交接文档里「被狗咬」被算成相邻
关系、方向相反的第七百一十一条拿 50 分,就是同一个毛病)。

如果模型在这簇上给出 >=0.80 的高 confidence 却答错,那 0.80 挡不住
这类错误,阈值再高也没意义;如果它答错时 confidence 明显偏低,那
0.80 就是有保护作用的,该保留。两种结果都能定案。

每条都标了 expect(法律上正确的主题)和 surface(字面容易误导到的
主题),判定看模型给的是哪个。

用法与前两簇相同,结果写到 _threshold_calibration_misleading.json。
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

# (消息, 法律上正确的主题, 字面误导向的主题)
MISLEADING: list[tuple[str, str, str]] = [
    (
        "在小区里被邻居家的狗咬了",
        "personal_injury",
        "property_neighbor",
    ),
    (
        "租的房子里我自己不小心把门弄坏了，房东要我赔",
        "general_rental",
        "general_rental",
    ),
    (
        "开的是朋友的车，出了事故对方要我全赔",
        "traffic_accident",
        "traffic_accident",
    ),
    (
        "在公司楼梯上摔伤了，公司说不算工伤",
        "wage_social_insurance",
        "personal_injury",
    ),
    (
        "上班时间被同事打了",
        "personal_injury",
        "workplace_harassment",
    ),
    (
        "美容院办的卡，做完脸过敏了",
        "service_contract",
        "medical_service_dispute",
    ),
    (
        "网上买的药吃了不舒服",
        "logistics_travel_food",
        "medical_service_dispute",
    ),
    (
        "房东把我押金转给了二房东，现在两个人互相推",
        "general_rental",
        "debt_collection",
    ),
    (
        "学校门口被电动车撞了，学校说不管",
        "traffic_accident",
        "education_minor_safety",
    ),
    (
        "催收的人把我欠钱的事告诉了我父母",
        "debt_collection",
        "privacy_reputation",
    ),
    (
        "前妻不让我见孩子",
        "family_support_property",
        "family_support_property",
    ),
    (
        "游戏里买的装备卖家收钱不发货",
        "game_account_dispute",
        "logistics_travel_food",
    ),
    (
        "装修公司干了一半跑了，钱付了七成",
        "service_contract",
        "renovation_default",
    ),
    (
        "医院开的药太贵了，我觉得是乱收费",
        "medical_service_dispute",
        "medical_service_dispute",
    ),
    (
        "外卖员把我的餐洒了还骂我",
        "logistics_travel_food",
        "privacy_reputation",
    ),
    (
        "领导以绩效为名扣了我三个月工资",
        "wage_social_insurance",
        "labor_termination",
    ),
]


async def main() -> int:
    settings = get_settings()
    playbooks = PlaybookRegistry.from_directory(settings.playbooks_path)
    topics = TopicRegistry.from_playbooks(playbooks)
    context = topics.provider_context()
    provider = create_provider(settings)

    records: list[dict[str, object]] = []
    for message, expect, surface in MISLEADING:
        row: dict[str, object] = {
            "message": message,
            "label": "misleading",
            "expect": expect,
            "surface": surface,
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
                "correct": candidate_id == expect,
            }
        )
        records.append(row)
        print(f"{message[:16]} -> {candidate_id} {extraction.confidence}")

    pathlib.Path("_threshold_calibration_misleading.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    print(f"wrote {len(records)} records")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
