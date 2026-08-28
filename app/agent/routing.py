from __future__ import annotations

import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal

from app.agent.models import (
    CoverageMode,
    CoverageResult,
    ExtractionResult,
    RiskFlag,
    RoutedExtraction,
)
from app.playbooks.registry import PlaybookRegistry


FORMAL_TOPIC_IDS = (
    "deposit_deduction",
    "prepaid_card",
    "overtime_pay",
    "return_refused",
    "counterfeit_goods",
    "training_refund",
    "auto_renewal",
    "renovation_default",
    "small_claim_procedure",
)

UNVERIFIED_TOPIC_IDS = (
    "education_minor_safety",
    "medical_service_dispute",
    "traffic_accident",
    "personal_injury",
    "labor_termination",
    "wage_social_insurance",
    "workplace_harassment",
    "debt_collection",
    "payment_fraud",
    "general_rental",
    "property_neighbor",
    "privacy_reputation",
    "family_support_property",
    "service_contract",
    "logistics_travel_food",
    "game_account_dispute",
)

GENERIC_FACT_NAMES = frozenset(
    {
        "people",
        "event_time",
        "location",
        "amount",
        "harm",
        "request",
        "counterparty",
        "ongoing",
        "evidence_status",
    }
)

_UNKNOWN_FACT_MARKERS = frozenset(
    {
        "unknown",
        "notknown",
        "notprovided",
        "unspecified",
        "n/a",
        "na",
        "null",
        "未知",
        "不详",
        "不清楚",
        "未提供",
        "未说明",
        "无法确认",
    }
)
_FACT_MARKER_EDGE_CHARS = " \t\r\n.,;:!?，。；：！？'\"`"

_RISK_ORDER: tuple[RiskFlag, ...] = (
    "immediate_danger",
    "minor_harm",
    "urgent_medical",
    "suspected_crime",
    "fraud_loss",
    "evidence_loss",
)


@dataclass(frozen=True, slots=True)
class TopicDefinition:
    id: str
    label: str
    aliases: tuple[str, ...]
    routing_signals: tuple[tuple[str, ...], ...]
    coverage_mode: Literal["formal", "unverified_guidance"]
    playbook_id: str | None = None

    def public_context(self) -> dict[str, object]:
        return {
            "label": self.label,
            "aliases": list(self.aliases),
            "coverage": self.coverage_mode,
        }

    def match_score(self, message: str) -> int:
        normalized = _normalize_alias(message)
        best_score = 0
        for group in self.routing_signals:
            phrases = tuple(
                phrase
                for phrase in (
                    _normalize_alias(value) for value in group
                )
                if phrase
            )
            if phrases and all(
                _contains_asserted_phrase(normalized, phrase)
                for phrase in phrases
            ):
                best_score = max(
                    best_score,
                    sum(len(phrase) for phrase in phrases),
                )
        return best_score


_ROUTING_SIGNALS: dict[str, tuple[tuple[str, ...], ...]] = {
    "deposit_deduction": (
        ("押金",),
        ("保证金", "租"),
    ),
    "prepaid_card": (
        ("预付卡",),
        ("储值卡",),
        ("预付款", "退"),
        ("健身房", "余额"),
    ),
    "overtime_pay": (
        ("加班",),
        ("加班费",),
        ("劳动仲裁", "加班"),
    ),
    "return_refused": (
        ("退货",),
        ("换货",),
        ("七天无理由",),
        ("质量问题", "退"),
        ("商品", "退"),
        ("网购", "退"),
        ("划痕", "退"),
        ("瑕疵", "退"),
        ("破损", "退"),
        ("拆封", "退"),
        ("发错货",),
        ("错发",),
        ("补发",),
        ("重新发货",),
    ),
    "counterfeit_goods": (
        ("假货",),
        ("以假充真",),
        ("掺假",),
        ("真伪", "商品"),
    ),
    "training_refund": (
        ("培训",),
        ("网课", "退款"),
        ("课程", "退费"),
        ("学费", "退"),
    ),
    "auto_renewal": (
        ("自动续费",),
        ("连续包月",),
        ("自动扣款",),
        ("默认勾选", "扣款"),
    ),
    "renovation_default": (
        ("装修",),
        ("施工", "返工"),
        ("工程", "延期"),
    ),
    "small_claim_procedure": (
        ("小额诉讼",),
        ("小额程序",),
        ("一审终审",),
    ),
    "education_minor_safety": (
        ("校园欺凌",),
        ("校园霸凌",),
        ("学校", "欺凌"),
        ("学校", "霸凌"),
        ("同学", "欺凌"),
        ("同学", "霸凌"),
        ("学校", "不处理"),
        ("学校", "不管"),
        ("老师", "学生"),
        ("学校", "学生", "伤"),
        ("未成年人", "校园"),
    ),
    "medical_service_dispute": (
        ("病历",),
        ("医疗收费",),
        ("诊疗",),
        ("知情同意",),
        ("医院", "手术"),
        ("医院", "治疗"),
    ),
    "traffic_accident": (
        ("交通事故",),
        ("车祸",),
        ("撞车",),
        ("追尾",),
        ("肇事逃逸",),
        ("车辆", "碰撞"),
    ),
    "personal_injury": (
        ("人身损害",),
        ("被狗咬",),
        ("动物致害",),
        ("场所", "摔伤"),
        ("公共区域", "受伤"),
        ("商场", "受伤"),
    ),
    "labor_termination": (
        ("辞退",),
        ("解雇",),
        ("开除",),
        ("逼迫离职",),
        ("解除通知",),
        ("未签劳动合同",),
        ("没签劳动合同",),
    ),
    "wage_social_insurance": (
        ("拖欠工资",),
        ("欠薪",),
        ("少发工资",),
        ("没交社保",),
        ("未缴社保",),
        ("没有缴社保",),
        ("公积金", "单位"),
    ),
    "workplace_harassment": (
        ("职场骚扰",),
        ("职场欺凌",),
        ("性骚扰", "工作"),
        ("领导", "骚扰"),
        ("报复性管理",),
    ),
    "debt_collection": (
        ("借钱不还",),
        ("欠款不还",),
        ("借条",),
        ("欠条",),
        ("民间借贷",),
        ("暴力催收",),
        ("骚扰催收",),
    ),
    "payment_fraud": (
        ("转账被骗",),
        ("被骗转账",),
        ("骗我转账",),
        ("骗着转账",),
        ("冒充客服",),
        ("盗刷",),
        ("刷单", "被骗"),
        ("诈骗", "转账"),
    ),
    "general_rental": (
        ("提前退租",),
        ("房东", "维修"),
        ("房东", "涨租"),
        ("租房合同",),
        ("房屋租赁",),
        ("房东", "房子"),
        ("房东", "房屋"),
    ),
    "property_neighbor": (
        ("物业", "不修"),
        ("物业", "不处理"),
        ("邻居噪音",),
        ("楼上漏水",),
        ("停车纠纷",),
        ("公共区域", "物业"),
    ),
    "privacy_reputation": (
        ("偷拍",),
        ("隐私泄露",),
        ("个人信息泄露",),
        ("人肉",),
        ("造谣",),
        ("公开", "个人信息"),
        ("公开", "姓名", "电话"),
        ("公开", "姓名", "住址"),
        ("公开", "联系方式"),
        ("网络骚扰",),
    ),
    "family_support_property": (
        ("抚养费",),
        ("探望孩子",),
        ("夫妻财产",),
        ("婚姻财产",),
        ("家庭财产",),
        ("赡养",),
    ),
    "service_contract": (
        ("会员服务",),
        ("服务未履行",),
        ("服务没有履行",),
        ("服务合同",),
        ("中介服务",),
        ("平台规则", "服务"),
    ),
    "logistics_travel_food": (
        ("快递损坏",),
        ("快递丢失",),
        ("旅游合同",),
        ("旅行社",),
        ("食品安全",),
        ("食物中毒",),
        ("外卖异物",),
        ("食品", "异物"),
        ("食物", "异物"),
        ("外卖", "虫"),
        ("饭", "虫"),
    ),
    "game_account_dispute": (
        ("游戏账号",),
        ("游戏账户",),
        ("借号",),
        ("共享账号",),
        ("开挂",),
        ("游戏封号",),
        ("游戏", "封禁"),
        ("游戏充值",),
    ),
}


_UNVERIFIED_DEFINITIONS: tuple[
    tuple[str, str, tuple[str, ...]], ...
] = (
    (
        "education_minor_safety",
        "教育、未成年人和校园安全",
        (
            "老师打学生",
            "老师打骂",
            "校园欺凌",
            "校园霸凌",
            "同学欺凌",
            "同学霸凌",
            "学校不处理",
            "在校受伤",
            "未成年人",
        ),
    ),
    (
        "medical_service_dispute",
        "医疗服务纠纷",
        (
            "医院",
            "医生",
            "诊疗",
            "病历",
            "医疗收费",
            "知情同意",
            "手术",
        ),
    ),
    (
        "traffic_accident",
        "交通事故",
        (
            "交通事故",
            "车祸",
            "撞车",
            "追尾",
            "保险理赔",
            "车辆碰撞",
        ),
    ),
    (
        "personal_injury",
        "一般人身损害",
        (
            "摔伤",
            "被狗咬",
            "动物致害",
            "公共区域受伤",
            "场所受伤",
            "人身损害",
        ),
    ),
    (
        "labor_termination",
        "劳动合同与解除",
        (
            "辞退",
            "开除",
            "不用来了",
            "逼迫离职",
            "未签劳动合同",
            "试用期解除",
        ),
    ),
    (
        "wage_social_insurance",
        "欠薪与社会保险",
        (
            "拖欠工资",
            "欠薪",
            "少发工资",
            "没交社保",
            "未缴社保",
            "公积金",
        ),
    ),
    (
        "workplace_harassment",
        "职场骚扰与职场侵害",
        (
            "职场骚扰",
            "性骚扰",
            "职场侮辱",
            "持续打压",
            "报复性管理",
            "领导骚扰",
        ),
    ),
    (
        "debt_collection",
        "民间借贷、欠款与催收",
        (
            "借钱不还",
            "欠条",
            "借条",
            "欠款",
            "民间借贷",
            "暴力催收",
            "骚扰催收",
        ),
    ),
    (
        "payment_fraud",
        "转账支付与网络诈骗",
        (
            "转账被骗",
            "被骗转账",
            "冒充客服",
            "网络诈骗",
            "账户被盗",
            "支付争议",
            "刷单被骗",
        ),
    ),
    (
        "general_rental",
        "一般房屋租赁",
        (
            "提前退租",
            "房东不维修",
            "涨租",
            "驱逐",
            "转租",
            "租房合同",
            "房屋租赁",
        ),
    ),
    (
        "property_neighbor",
        "物业与邻里",
        (
            "物业不修",
            "物业",
            "邻居噪音",
            "楼上漏水",
            "停车纠纷",
            "公共区域",
        ),
    ),
    (
        "privacy_reputation",
        "隐私、个人信息、名誉和网络骚扰",
        (
            "偷拍",
            "隐私泄露",
            "个人信息泄露",
            "人肉",
            "造谣",
            "公开联系方式",
            "网络骚扰",
        ),
    ),
    (
        "family_support_property",
        "婚姻家庭、抚养与财产",
        (
            "抚养费",
            "探望孩子",
            "家庭财产",
            "夫妻财产",
            "婚姻财产",
            "赡养",
        ),
    ),
    (
        "service_contract",
        "平台和一般服务合同",
        (
            "会员服务",
            "服务未履行",
            "平台规则",
            "服务退款",
            "服务合同",
            "中介服务",
        ),
    ),
    (
        "logistics_travel_food",
        "快递、旅游和食品安全消费服务",
        (
            "快递损坏",
            "快递丢失",
            "旅游合同",
            "旅行社",
            "食品安全",
            "食物中毒",
            "外卖异物",
            "食品异物",
            "吃出异物",
            "外卖里有虫子",
        ),
    ),
    (
        "game_account_dispute",
        "游戏账号借用、封禁与平台申诉",
        (
            "游戏账号",
            "借号",
            "共享账号",
            "开挂封号",
            "作弊封禁",
            "游戏账号申诉",
            "游戏充值损失",
        ),
    ),
)


class TopicRegistry:
    def __init__(self, topics: list[TopicDefinition]) -> None:
        if not topics:
            raise ValueError("Topic 注册表不能为空")
        by_id: dict[str, TopicDefinition] = {}
        aliases: dict[str, str] = {}
        for topic in topics:
            if topic.id in by_id:
                raise ValueError(f"Topic ID 重复: {topic.id}")
            if not topic.routing_signals:
                raise ValueError(f"Topic 缺少路由信号: {topic.id}")
            if (
                topic.coverage_mode == "formal"
                and topic.playbook_id is None
            ):
                raise ValueError("正式 Topic 必须关联 Playbook")
            if (
                topic.coverage_mode == "unverified_guidance"
                and topic.playbook_id is not None
            ):
                raise ValueError("未核验 Topic 不得关联 Playbook")
            by_id[topic.id] = topic
            for alias in (topic.id, topic.label, *topic.aliases):
                normalized = _normalize_alias(alias)
                existing = aliases.get(normalized)
                if existing is not None and existing != topic.id:
                    continue
                aliases[normalized] = topic.id
        self._by_id = MappingProxyType(by_id)
        self._aliases = MappingProxyType(aliases)

    @classmethod
    def from_playbooks(
        cls,
        playbooks: PlaybookRegistry,
    ) -> "TopicRegistry":
        formal_topics = [
            TopicDefinition(
                id=playbook.id,
                label=playbook.name,
                aliases=tuple(playbook.aliases),
                routing_signals=_routing_signals(playbook.id),
                coverage_mode="formal",
                playbook_id=playbook.id,
            )
            for playbook in playbooks.playbooks
        ]
        unverified_topics = [
            TopicDefinition(
                id=topic_id,
                label=label,
                aliases=aliases,
                routing_signals=_routing_signals(topic_id),
                coverage_mode="unverified_guidance",
            )
            for topic_id, label, aliases in _UNVERIFIED_DEFINITIONS
        ]
        return cls([*formal_topics, *unverified_topics])

    @property
    def topic_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_id))

    @property
    def formal_topic_ids(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                topic.id
                for topic in self._by_id.values()
                if topic.coverage_mode == "formal"
            )
        )

    @property
    def unverified_topic_ids(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                topic.id
                for topic in self._by_id.values()
                if topic.coverage_mode == "unverified_guidance"
            )
        )

    def get(self, topic_id: str) -> TopicDefinition:
        try:
            return self._by_id[topic_id]
        except KeyError as exc:
            raise LookupError(f"未知 Topic: {topic_id}") from exc

    def resolve(self, value: str) -> TopicDefinition | None:
        topic_id = self._aliases.get(_normalize_alias(value))
        return self._by_id.get(topic_id) if topic_id else None

    def infer_from_text(self, message: str) -> TopicDefinition | None:
        return self._infer_from_text(message)

    def infer_unverified_from_text(
        self,
        message: str,
    ) -> TopicDefinition | None:
        return self._infer_from_text(
            message,
            coverage_mode="unverified_guidance",
        )

    def _infer_from_text(
        self,
        message: str,
        *,
        coverage_mode: Literal[
            "formal",
            "unverified_guidance",
        ]
        | None = None,
    ) -> TopicDefinition | None:
        normalized = _normalize_alias(message)
        best: TopicDefinition | None = None
        best_score = 0
        for topic in self._by_id.values():
            if (
                coverage_mode is not None
                and topic.coverage_mode != coverage_mode
            ):
                continue
            score = topic.match_score(normalized)
            if score > best_score:
                best = topic
                best_score = score
        return best

    def provider_context(self) -> dict[str, object]:
        return {
            "allowed_topic_ids": list(self.topic_ids),
            "topic_definitions": {
                topic_id: self._by_id[topic_id].public_context()
                for topic_id in self.topic_ids
            },
            "generic_fact_names": sorted(GENERIC_FACT_NAMES),
        }


class SafetySignalGate:
    _patterns: dict[RiskFlag, tuple[str, ...]] = {
        "immediate_danger": (
            "拿刀威胁",
            "持刀威胁",
            "正在被打",
            "还在打我",
            "现在打我",
            "正在施暴",
            "威胁要杀",
            "人身危险",
            "无法离开现场",
        ),
        "minor_harm": (
            "\u8001\u5e08\u6bb4\u6253",
            "孩子正在被打",
            "学生正在被打",
            "正在殴打孩子",
            "老师殴打学生",
            "老师打学生",
            "未成年人正在受伤害",
            "孩子正在遭受伤害",
        ),
        "urgent_medical": (
            "大量出血",
            "已经昏迷",
            "人已经昏迷",
            "呼吸困难",
            "失去意识",
            "需要急救",
            "急需就医",
        ),
        "suspected_crime": (
            "遭到性侵",
            "被性侵",
            "遭到强奸",
            "被绑架",
            "遭到抢劫",
            "限制人身自由",
            "被非法拘禁",
        ),
        "fraud_loss": (
            "转账被骗",
            "被骗转账",
            "骗着转了钱",
            "骗着转账",
            "刚给骗子转账",
            "冒充客服骗",
            "账户被盗刷",
        ),
        "evidence_loss": (
            "监控今晚就会覆盖",
            "监控马上覆盖",
            "证据马上消失",
            "证据即将灭失",
            "聊天记录马上被删",
            "数据马上被删除",
        ),
    }

    def detect(
        self,
        message: str,
        candidate_flags: tuple[RiskFlag, ...] | list[RiskFlag] = (),
    ) -> tuple[RiskFlag, ...]:
        # Provider flags are candidates only. A local, explicit signal is
        # required before a risk label can affect routing.
        del candidate_flags
        detected: set[RiskFlag] = set()
        normalized = "".join(message.casefold().split())
        for risk_flag, phrases in self._patterns.items():
            if any(
                "".join(phrase.casefold().split()) in normalized
                for phrase in phrases
            ):
                detected.add(risk_flag)
        return tuple(flag for flag in _RISK_ORDER if flag in detected)


# 模型给出了具体主题、但该主题的关键词对这条消息一个都没命中时，
# 要不要仍然采信模型。这个下限只在那种情形下生效（见 semantic_confident）。
#
# 0.55 是量出来的，不是猜的。跑真实 DeepSeek 抽取 71 条探针
# （scripts/calibrate_routing_threshold*.py，三簇：口语化的明确案情、
# 欠定消息、表面像 A 实则是 B），按「模型弃权」和「模型下判断」分簇：
#
#   模型弃权（candidate_topic_id=unknown），n=30：0.1 ~ 0.5
#   模型下判断且关键词得分为 0，n=23：      0.6 ~ 0.9
#
# 两簇之间有 0.10 宽的空隙，取中点 0.55。含糊消息全部落在 0.5 及以下，
# 模型真正下判断时最低 0.6，所以 0.55 既放行全部 23 条判断，
# 又高于全部 30 条弃权。
#
# 原值 0.80 是从未校准的猜测，实测误拒簇 B 的 43%（10/23），
# 其中包括甲醛案的口语表述「住进去就头痛咳嗽，屋里味儿特别冲」
# （模型给 general_rental，confidence 0.6，正确）。被丢弃后 topic_id
# 落回 unknown，而 infer_topic 对这句返回 None（触发词表里没有
# 「头痛」「味儿冲」，只有「甲醛」），最终一条法条都给不出——正是
# 本项目要修的那个问题。
#
# 已知未解决的问题：这道门挡不住「模型答错但很自信」。71 条里唯一
# 一条在管辖分支内答错的样本（「在公司楼梯上摔伤了，公司说不算工伤」
# → personal_injury，应为 wage_social_insurance）confidence 是 0.7，
# 落在正确簇的正中间。任何阈值都分不开这两者，调高只会连正确的一起
# 拒掉。这类错误得靠别的机制（主题间的互斥判定），不是靠阈值。
_SEMANTIC_CONFIDENCE_FLOOR = 0.55


class ScenarioRouter:
    def __init__(
        self,
        registry: TopicRegistry,
        *,
        min_confidence: float,
        safety_gate: SafetySignalGate | None = None,
    ) -> None:
        if not 0 <= min_confidence <= 1:
            raise ValueError("最小置信度必须在 0 到 1 之间")
        self.registry = registry
        self.min_confidence = min_confidence
        self.safety_gate = safety_gate or SafetySignalGate()

    def emergency_coverage(
        self,
        message: str,
    ) -> CoverageResult | None:
        risk_flags = self.safety_gate.detect(message)
        if not risk_flags:
            return None
        topic = self.registry.infer_unverified_from_text(message)
        return self._coverage(
            topic=topic,
            mode="emergency_guidance",
            confidence=None,
            risk_flags=risk_flags,
        )

    def unverified_coverage(
        self,
        message: str,
        *,
        confidence: float | None = None,
        previous_topic_id: str | None = None,
    ) -> CoverageResult | None:
        explicit = self.registry.infer_from_text(message)
        if explicit is not None and explicit.coverage_mode == "formal":
            return None
        topic = (
            explicit
            if explicit is not None
            else self._previous_unverified(previous_topic_id)
        )
        if topic is None and previous_topic_id != "unknown":
            return None
        return self._coverage(
            topic=topic,
            mode="unverified_guidance",
            confidence=confidence,
            risk_flags=(),
        )

    def route(
        self,
        extraction: ExtractionResult,
        *,
        message: str,
        previous_topic_id: str | None = None,
        contextual_formal_topic_id: str | None = None,
    ) -> RoutedExtraction:
        risk_flags = self.safety_gate.detect(
            message,
            extraction.risk_flags,
        )
        candidate = self._candidate(extraction.candidate_topic_id)
        explicit = self.registry.infer_from_text(message)
        explicitly_low = (
            extraction.confidence is not None
            and extraction.confidence < self.min_confidence
        )
        if explicitly_low:
            candidate = None
        previous_unverified = self._previous_unverified(
            previous_topic_id
        )
        candidate_score = (
            candidate.match_score(message)
            if candidate is not None
            else 0
        )
        semantic_confident = (
            extraction.confidence is not None
            and extraction.confidence >= max(
                self.min_confidence,
                _SEMANTIC_CONFIDENCE_FLOOR,
            )
        )
        contextual_formal_match = (
            candidate is not None
            and candidate.coverage_mode == "formal"
            and candidate.id == contextual_formal_topic_id
            and extraction.confidence is not None
            and extraction.confidence >= self.min_confidence
        )
        if (
            candidate is not None
            and explicit is not None
            and explicit.id != candidate.id
            and explicit.match_score(message) > candidate_score
        ):
            candidate = (
                explicit
                if explicit.coverage_mode == "unverified_guidance"
                else None
            )
        elif candidate is not None and candidate_score == 0:
            if (
                previous_unverified is not None
                and explicit is None
                and previous_unverified.id != candidate.id
            ):
                candidate = previous_unverified
            elif contextual_formal_match:
                pass
            elif (
                semantic_confident
                and contextual_formal_topic_id is None
            ):
                pass
            else:
                candidate = None
        if candidate is None:
            if (
                explicit is not None
                and explicit.coverage_mode == "unverified_guidance"
            ):
                candidate = explicit
            elif explicit is None:
                candidate = previous_unverified

        if risk_flags:
            coverage = self._coverage(
                topic=candidate,
                mode="emergency_guidance",
                confidence=extraction.confidence,
                risk_flags=risk_flags,
                model_label=extraction.topic_label,
            )
            return RoutedExtraction(
                coverage=coverage,
                facts=_generic_facts(extraction.facts),
                unknown_slots=_generic_unknown_slots(
                    extraction.unknown_slots
                ),
            )

        if candidate is not None and candidate.coverage_mode == "formal":
            coverage = self._coverage(
                topic=candidate,
                mode="formal",
                confidence=extraction.confidence,
                risk_flags=(),
            )
            return RoutedExtraction(
                coverage=coverage,
                facts=extraction.facts,
                unknown_slots=extraction.unknown_slots,
            )

        coverage = self._coverage(
            topic=candidate,
            mode="unverified_guidance",
            confidence=extraction.confidence,
            risk_flags=(),
            model_label=extraction.topic_label,
        )
        return RoutedExtraction(
            coverage=coverage,
            facts=_generic_facts(extraction.facts),
            unknown_slots=_generic_unknown_slots(
                extraction.unknown_slots
            ),
        )

    def _candidate(self, candidate_topic_id: str) -> TopicDefinition | None:
        if candidate_topic_id in {"unknown", "unsupported"}:
            return None
        try:
            return self.registry.get(candidate_topic_id)
        except LookupError:
            return None

    def _previous_unverified(
        self,
        topic_id: str | None,
    ) -> TopicDefinition | None:
        if topic_id in {None, "unknown"}:
            return None
        try:
            topic = self.registry.get(topic_id)
        except LookupError:
            return None
        if topic.coverage_mode != "unverified_guidance":
            return None
        return topic

    @staticmethod
    def _coverage(
        *,
        topic: TopicDefinition | None,
        mode: CoverageMode,
        confidence: float | None,
        risk_flags: tuple[RiskFlag, ...],
        model_label: str | None = None,
    ) -> CoverageResult:
        topic_id = topic.id if topic is not None else "unknown"
        if topic is not None:
            topic_label = topic.label
        else:
            # 注册表只认预定义主题，超出词表时用模型给的标签，
            # 避免内部兜底串出现在用户可见文案里。
            topic_label = (
                sanitize_topic_label(model_label) or FALLBACK_TOPIC_LABEL
            )
        if mode == "formal":
            notice = "已进入本地核验的正式处理流程。"
            playbook_id = topic.playbook_id if topic is not None else None
        elif mode == "emergency_guidance":
            notice = (
                "当前信号需要优先处理安全、就医、止损或证据保护，"
                "本结果不作正式法律判断。"
            )
            playbook_id = None
        else:
            notice = (
                "该主题尚未纳入本地正式 Playbook，"
                "当前仅提供取证、沟通和求助指导。"
            )
            playbook_id = None
        return CoverageResult(
            mode=mode,
            topic_id=topic_id,
            topic_label=topic_label,
            confidence=confidence,
            playbook_id=playbook_id,
            notice=notice,
            risk_flags=list(risk_flags),
        )


_LABEL_URL_PATTERN = re.compile(
    r"(https?://|www\.|[a-z0-9-]+\.(com|cn|net|org|gov))",
    re.IGNORECASE,
)
_LABEL_ARTICLE_PATTERN = re.compile(r"第[一二三四五六七八九十百千零〇\d]+条")
_LABEL_INJECTION_MARKERS = (
    "prompt",
    "system",
    "instruction",
    "ignore",
    "assistant",
    "playbook",
    "coverage",
    "verdict",
    "api",
    "key",
    "指令",
    "提示词",
    "系统",
    "忽略",
)
FALLBACK_TOPIC_LABEL = "你描述的这件事"


def sanitize_topic_label(value: str | None) -> str | None:
    """校验模型给出的主题标签，供用户可见文案使用。

    模型标签是拓宽覆盖面的关键信息：注册表只认 25 个预定义主题，
    超出词表的问题拿不到任何标签。但它同时是不可信输入，展示前
    必须过滤网址、法条编号和 prompt 回显。
    """
    if value is None:
        return None
    # 结构字符必须在归一化之前检查：" ".join(split()) 会把换行和
    # 制表符变成空格，放到后面这道检查就永远不会触发。
    if any(char in value for char in "\n\r\t{}[]<>|"):
        return None
    normalized = " ".join(value.split()).strip("“”\"'`《》()（） \t")
    if not 2 <= len(normalized) <= 30:
        return None
    if _LABEL_URL_PATTERN.search(normalized):
        return None
    if _LABEL_ARTICLE_PATTERN.search(normalized):
        return None
    lowered = normalized.lower()
    if any(marker in lowered for marker in _LABEL_INJECTION_MARKERS):
        return None
    return normalized


def _generic_facts(values: dict[str, Any]) -> dict[str, Any]:
    filtered: dict[str, Any] = {}
    for name, value in values.items():
        if name not in GENERIC_FACT_NAMES:
            continue
        normalized = _generic_value(value)
        if normalized is not None:
            filtered[name] = normalized
    return filtered


def _generic_unknown_slots(values: list[str]) -> list[str]:
    return list(
        dict.fromkeys(
            value for value in values if value in GENERIC_FACT_NAMES
        )
    )


def _generic_value(value: Any) -> Any | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip()
        if not normalized or is_unknown_fact_placeholder(normalized):
            return None
        return normalized[:2000]
    if isinstance(value, list):
        normalized_items = [
            item.strip()[:500]
            for item in value[:10]
            if (
                isinstance(item, str)
                and item.strip()
                and not is_unknown_fact_placeholder(item)
            )
        ]
        return normalized_items or None
    return None


def is_unknown_fact_placeholder(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    normalized = "".join(
        value.strip(_FACT_MARKER_EDGE_CHARS).casefold().split()
    )
    return normalized in _UNKNOWN_FACT_MARKERS


def _normalize_alias(value: str) -> str:
    return "".join(value.strip().casefold().split())


def _routing_signals(topic_id: str) -> tuple[tuple[str, ...], ...]:
    try:
        return _ROUTING_SIGNALS[topic_id]
    except KeyError as exc:
        raise ValueError(f"Topic 缺少路由信号配置: {topic_id}") from exc


def _contains_asserted_phrase(text: str, phrase: str) -> bool:
    start = text.find(phrase)
    while start >= 0:
        prefix = text[max(0, start - 6) : start]
        if not any(
            prefix.endswith(negation)
            for negation in (
                "不是",
                "并非",
                "不属于",
                "不涉及",
                "不是什么",
            )
        ):
            return True
        start = text.find(phrase, start + len(phrase))
    return False
