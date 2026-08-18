from __future__ import annotations

import re
from collections import deque
from collections.abc import Iterable, Mapping
from datetime import date
from typing import Any
from uuid import uuid4

from app.agent.errors import ProviderError
from app.agent.models import (
    CaseContinuationContext,
    CaseContinuationResult,
    ExtractionResult,
    PolishingDraft,
    UsageInfo,
)
from app.agent.grounding import (
    GroundedAnswerComposition,
    GroundingPacket,
    build_local_answer,
)
from app.agent.progression import classify_turn_intent, is_direct_question
from app.agent.routing import GENERIC_FACT_NAMES, SafetySignalGate
from app.attachments.models import AttachmentEvidenceContext
from app.providers.base import scenario_definition


_SCENARIO_KEYWORDS: dict[str, tuple[str, ...]] = {
    "deposit_deduction": ("押金", "房东", "退租", "退房", "租房"),
    "prepaid_card": ("预付卡", "健身房", "储值", "余额", "商家停业"),
    "overtime_pay": ("加班", "工资", "劳动仲裁", "用人单位", "公司"),
    "return_refused": (
        "网购",
        "退款",
        "退货",
        "七天无理由",
        "换货",
        "质量问题",
        "与描述不符",
        "发错货",
        "错发",
        "补发",
        "重新发货",
    ),
    "counterfeit_goods": ("假货", "以假充真", "三倍赔偿", "掺假"),
    "training_refund": ("培训", "网课", "课程", "退学费"),
    "auto_renewal": ("自动续费", "连续包月", "自动扣款", "默认勾选"),
    "renovation_default": ("装修", "施工", "返工", "工程延期"),
    "small_claim_procedure": ("小额诉讼", "一审终审", "审结", "程序异议"),
}

_UNVERIFIED_TOPIC_KEYWORDS: dict[str, tuple[str, ...]] = {
    "education_minor_safety": (
        "老师打骂",
        "老师殴打",
        "校园欺凌",
        "同学欺凌",
        "同学霸凌",
        "学校不处理",
        "学校不管",
        "在校受伤",
        "未成年人",
    ),
    "medical_service_dispute": (
        "医院",
        "医生",
        "病历",
        "医疗收费",
        "诊疗",
        "手术",
    ),
    "traffic_accident": (
        "交通事故",
        "撞车",
        "追尾",
        "肇事逃逸",
        "保险理赔",
    ),
    "personal_injury": (
        "商场摔伤",
        "场所受伤",
        "被狗咬",
        "动物致害",
        "人身损害",
    ),
    "labor_termination": (
        "辞退",
        "解雇",
        "解除通知",
        "逼迫离职",
        "没签劳动合同",
        "试用期解除",
    ),
    "wage_social_insurance": (
        "拖欠工资",
        "欠薪",
        "少发工资",
        "没有缴社保",
        "没交社保",
        "未缴社保",
        "公积金",
    ),
    "workplace_harassment": (
        "职场骚扰",
        "性骚扰",
        "职场欺凌",
        "持续打压",
        "报复性管理",
    ),
    "debt_collection": (
        "借钱不还",
        "欠条",
        "借条",
        "民间借贷",
        "暴力催收",
        "骚扰催收",
    ),
    "payment_fraud": (
        "冒充客服",
        "骗我转账",
        "转账被骗",
        "网络诈骗",
        "账户被盗",
        "刷单被骗",
    ),
    "general_rental": (
        "提前退租",
        "一直不维修",
        "房东不维修",
        "涨租",
        "驱赶",
        "转租",
        "租房合同",
    ),
    "property_neighbor": (
        "物业不处理",
        "物业不维修",
        "楼上漏水",
        "邻居噪音",
        "停车纠纷",
        "公共区域",
    ),
    "privacy_reputation": (
        "公开我的个人信息",
        "个人信息泄露",
        "隐私泄露",
        "偷拍",
        "造谣",
        "人肉",
        "网络骚扰",
    ),
    "family_support_property": (
        "抚养费",
        "探望孩子",
        "夫妻财产",
        "婚姻财产",
        "家庭财产",
        "赡养",
    ),
    "service_contract": (
        "会员服务",
        "服务没有履行",
        "服务未履行",
        "服务合同",
        "中介服务",
        "平台规则",
    ),
    "logistics_travel_food": (
        "快递损坏",
        "快递丢失",
        "旅游合同",
        "旅行社",
        "食品安全",
        "食物中毒",
        "外卖异物",
        "食品异物",
        "吃出异物",
        "外卖",
        "虫子",
    ),
    "game_account_dispute": (
        "游戏账号",
        "游戏账户",
        "借号",
        "共享账号",
        "开挂",
        "游戏封号",
        "游戏封禁",
        "游戏充值",
    ),
}

_TOPIC_KEYWORDS = {
    **_SCENARIO_KEYWORDS,
    **_UNVERIFIED_TOPIC_KEYWORDS,
}

_DATE_PATTERN = re.compile(
    r"(?P<year>20\d{2})[-年/.](?P<month>\d{1,2})[-月/.](?P<day>\d{1,2})日?"
)
_MONEY_PATTERN = re.compile(
    r"(?:人民币|￥|¥)?\s*(\d+(?:\.\d+)?)\s*(?:元|块)"
)
_REPLACEMENT_GOAL_WORDS = ("补发", "重新发货", "重发", "换成正确")


class FakeProvider:
    name = "fake"
    model = "fake-deterministic-v1"

    def __init__(
        self,
        responses: Iterable[ExtractionResult] | None = None,
        *,
        continuation_responses: (
            Iterable[CaseContinuationResult] | None
        ) = None,
        composition_responses: (
            Iterable[GroundedAnswerComposition] | None
        ) = None,
        error: ProviderError | None = None,
        composition_error: ProviderError | None = None,
    ) -> None:
        self._responses = deque(responses or ())
        self._continuation_responses = deque(
            continuation_responses or ()
        )
        self._composition_responses = deque(
            composition_responses or ()
        )
        self._error = error
        self._composition_error = composition_error
        self.extraction_calls = 0
        self.continuation_calls = 0
        self.composition_calls = 0
        self.extraction_evidence_calls: list[
            tuple[AttachmentEvidenceContext, ...]
        ] = []
        self.extraction_context_calls: list[dict[str, object]] = []
        self.continuation_evidence_calls: list[
            tuple[AttachmentEvidenceContext, ...]
        ] = []
        self.composition_packet_calls: list[GroundingPacket] = []

    async def extract_facts(
        self,
        message: str,
        context: dict[str, object],
        evidence: tuple[AttachmentEvidenceContext, ...] = (),
        *,
        timeout_seconds: float | None = None,
    ) -> ExtractionResult:
        del timeout_seconds
        self.extraction_calls += 1
        self.extraction_evidence_calls.append(tuple(evidence))
        self.extraction_context_calls.append(dict(context))
        if self._error is not None:
            raise self._error
        if self._responses:
            return self._responses.popleft()

        candidate_topic_id = self._classify(message, context)
        definition = scenario_definition(context, candidate_topic_id)
        if definition:
            facts = self._extract_known_facts(
                candidate_topic_id,
                message,
            )
        else:
            facts = _extract_generic_facts(message)
        allowed_slots = {
            str(name)
            for name in definition.get("allowed_slot_names", [])
            if isinstance(name, str)
        }
        if not definition:
            allowed_slots = {
                str(name)
                for name in context.get(
                    "generic_fact_names",
                    GENERIC_FACT_NAMES,
                )
                if isinstance(name, str)
            }
        if allowed_slots:
            facts = {
                name: value
                for name, value in facts.items()
                if name in allowed_slots
            }
        required_slots = [
            str(name)
            for name in definition.get("required_slot_names", [])
            if isinstance(name, str)
        ]
        existing = context.get("existing_facts", {})
        existing_names = set(existing) if isinstance(existing, dict) else set()
        unknown = [
            name
            for name in required_slots
            if name not in facts and name not in existing_names
        ]
        turn_intent = classify_turn_intent(message)
        explicit_question, bounded_answer, facts_to_verify = (
            _bounded_question_answer(
                message,
                candidate_topic_id=candidate_topic_id,
                turn_intent=turn_intent,
            )
        )
        return ExtractionResult(
            candidate_topic_id=candidate_topic_id,
            topic_label=_topic_label(context, candidate_topic_id),
            turn_intent=turn_intent,
            facts=facts,
            unknown_slots=unknown,
            risk_flags=list(SafetySignalGate().detect(message)),
            explicit_question=explicit_question,
            bounded_answer=bounded_answer,
            facts_to_verify=facts_to_verify,
            confidence=(
                0.99
                if candidate_topic_id not in {"unknown", "unsupported"}
                else 0.0
            ),
            provider=self.name,
            model=self.model,
            request_id=f"fake-{uuid4()}",
            usage=UsageInfo(),
        )

    async def continue_case(
        self,
        message: str,
        context: CaseContinuationContext,
        evidence: tuple[AttachmentEvidenceContext, ...] = (),
        *,
        timeout_seconds: float | None = None,
    ) -> CaseContinuationResult:
        del timeout_seconds
        self.continuation_calls += 1
        self.continuation_evidence_calls.append(tuple(evidence))
        if self._error is not None:
            raise self._error
        if self._continuation_responses:
            return self._continuation_responses.popleft()

        new_scenario_id = self._classify_new_case(message, context)
        if new_scenario_id is not None:
            return CaseContinuationResult(
                route="new_case",
                scenario_id=new_scenario_id,
                confidence=0.99,
                provider=self.name,
                model=self.model,
                request_id=f"fake-{uuid4()}",
                usage=UsageInfo(),
            )

        action_refs = [
            action.ref for action in context.locked_case.actions[:2]
        ]
        citation_refs = [
            citation.ref for citation in context.locked_case.citations[:1]
        ]
        if action_refs:
            answer = (
                "先保留对方不配合的记录，再按现有方案中的下一步操作"
                "推进；如平台或对方仍拒绝处理，保存新的书面反馈。"
            )
        else:
            answer = "先保存对方不配合的记录，并继续按现有方案推进。"
        return CaseContinuationResult(
            route="same_case",
            scenario_id=context.current_scenario.id,
            facts={},
            cleared_slots=[],
            answer=answer,
            action_refs=action_refs,
            citation_refs=citation_refs,
            confidence=0.99,
            provider=self.name,
            model=self.model,
            request_id=f"fake-{uuid4()}",
            usage=UsageInfo(),
        )

    async def compose_grounded_answer(
        self,
        packet: GroundingPacket,
        *,
        timeout_seconds: float | None = None,
    ) -> GroundedAnswerComposition:
        del timeout_seconds
        self.composition_calls += 1
        self.composition_packet_calls.append(packet)
        if self._composition_error is not None:
            raise self._composition_error
        if self._error is not None:
            raise self._error
        if self._composition_responses:
            return self._composition_responses.popleft()
        draft = build_local_answer(packet)
        return GroundedAnswerComposition(
            **draft.model_dump(mode="python"),
            provider=self.name,
            model=self.model,
            request_id=f"fake-{uuid4()}",
            usage=UsageInfo(),
        )

    async def polish_text(
        self,
        draft: PolishingDraft,
        *,
        timeout_seconds: float | None = None,
    ) -> str:
        del timeout_seconds
        if self._error is not None:
            raise self._error
        return draft.text

    @staticmethod
    def _classify_new_case(
        message: str,
        context: CaseContinuationContext,
    ) -> str | None:
        current_id = context.current_scenario.id
        registered_ids = {
            scenario.id for scenario in context.registered_scenarios
        }
        best_id: str | None = None
        best_score = 0
        for scenario_id, keywords in _SCENARIO_KEYWORDS.items():
            if (
                scenario_id == current_id
                or scenario_id not in registered_ids
            ):
                continue
            score = sum(
                len(keyword)
                for keyword in keywords
                if keyword in message
            )
            if score > best_score:
                best_id = scenario_id
                best_score = score
        return best_id

    @staticmethod
    def _classify(message: str, context: dict[str, object]) -> str:
        current = context.get("current_scenario_id")
        if isinstance(current, str) and current:
            return current

        candidate_contract = "allowed_topic_ids" in context
        allowed_key = (
            "allowed_topic_ids"
            if candidate_contract
            else "allowed_scenario_ids"
        )
        allowed = {
            str(item)
            for item in context.get(allowed_key, [])
            if isinstance(item, str)
        }
        best_id = "unknown" if candidate_contract else "unsupported"
        best_score = 0
        keywords_by_topic = (
            _TOPIC_KEYWORDS
            if candidate_contract
            else _SCENARIO_KEYWORDS
        )
        for topic_id, keywords in keywords_by_topic.items():
            if allowed and topic_id not in allowed:
                continue
            score = sum(
                len(keyword)
                for keyword in keywords
                if keyword in message
            )
            if score > best_score:
                best_id = topic_id
                best_score = score
        if best_score == 0 and candidate_contract:
            previous = context.get("previous_topic_id")
            if (
                isinstance(previous, str)
                and previous
                and (not allowed or previous in allowed)
            ):
                return previous
        return best_id

    @staticmethod
    def _extract_known_facts(
        scenario_id: str,
        message: str,
    ) -> dict[str, Any]:
        facts: dict[str, Any] = {}
        amounts = [float(value) for value in _MONEY_PATTERN.findall(message)]
        parsed_date = _parse_date(message)

        if scenario_id == "deposit_deduction":
            if amounts:
                facts["deposit_amount"] = amounts[0]
            if len(amounts) > 1:
                facts["withheld_amount"] = amounts[1]
            elif amounts and any(word in message for word in ("扣", "不退")):
                facts["withheld_amount"] = amounts[0]
            if parsed_date is not None:
                facts["lease_end_date"] = parsed_date.isoformat()
            facts.update(_deposit_reason(message))
            facts["has_checkout_photos"] = _positive(
                message,
                ("退房照片", "交房照片", "拍了照片"),
            )
            facts["landlord_has_receipt"] = _positive(
                message,
                ("发票", "收据", "维修单"),
            )
            contract_term = _contract_deduction_term(message)
            if contract_term is not None:
                facts["contract_has_deduction_term"] = contract_term
            facts["has_written_notice"] = _positive(
                message,
                ("书面通知", "扣款通知", "短信通知", "微信通知"),
            )
            if len(amounts) > 2:
                facts["repair_cost"] = amounts[2]
        elif scenario_id == "overtime_pay":
            facts["issue_type"] = _overtime_issue(message)
            if parsed_date is not None:
                facts["dispute_date"] = parsed_date.isoformat()
            if amounts:
                facts["claimed_amount"] = amounts[0]
        elif scenario_id == "return_refused":
            facts["issue_type"] = _return_issue(message)
            if parsed_date is not None:
                facts["purchase_date"] = parsed_date.isoformat()
            if amounts:
                facts["purchase_amount"] = amounts[0]
        elif scenario_id == "prepaid_card":
            facts["issue_type"] = _prepaid_issue(message)
            if amounts:
                facts["remaining_amount"] = amounts[-1]
        elif scenario_id == "counterfeit_goods":
            facts["issue_type"] = _counterfeit_issue(message)
            if amounts:
                facts["purchase_amount"] = amounts[0]
        elif scenario_id == "training_refund":
            facts["issue_type"] = _training_issue(message)
            if amounts:
                facts["remaining_amount"] = amounts[-1]
        elif scenario_id == "auto_renewal":
            facts["issue_type"] = _renewal_issue(message)
            if parsed_date is not None:
                facts["charge_date"] = parsed_date.isoformat()
            if amounts:
                facts["charge_amount"] = amounts[0]
        elif scenario_id == "renovation_default":
            facts["issue_type"] = _renovation_issue(message)
            if amounts:
                facts["contract_amount"] = amounts[0]
        elif scenario_id == "small_claim_procedure":
            facts["issue_type"] = _procedure_issue(message)
            if parsed_date is not None:
                facts["filing_date"] = parsed_date.isoformat()
            if amounts:
                facts["claim_amount"] = amounts[0]
        return facts


def _extract_generic_facts(message: str) -> dict[str, Any]:
    facts: dict[str, Any] = {}
    amounts = [float(value) for value in _MONEY_PATTERN.findall(message)]
    parsed_date = _parse_date(message)
    if parsed_date is not None:
        facts["event_time"] = parsed_date.isoformat()
    if amounts:
        facts["amount"] = amounts[0]
    return facts


def _bounded_question_answer(
    message: str,
    *,
    candidate_topic_id: str,
    turn_intent: str,
) -> tuple[str | None, str | None, list[str]]:
    if turn_intent == "stated_goal":
        if candidate_topic_id == "return_refused" and any(
            word in message for word in _REPLACEMENT_GOAL_WORDS
        ):
            return (
                None,
                (
                    "可以先把补发正确商品作为当前明确诉求。通过平台"
                    "聊天写明订单号、错发商品、应补发商品和答复期限，"
                    "并保存发送及已读记录。"
                ),
                ["商家是否已经收到补发要求并给出答复"],
            )
        return (
            None,
            "可以先把这项要求作为当前明确诉求，并通过可留痕渠道告知对方。",
            ["对方是否已经收到该诉求并作出书面回应"],
        )
    if not is_direct_question(message):
        return None, None, []

    question = message.strip()[:300]
    if candidate_topic_id == "game_account_dispute":
        facts_to_verify = [
            "对方真实身份及可用于送达的联系信息",
            "双方关于借用账号和使用范围的约定",
            "平台封禁原因、实际损失及其与对方行为的关系",
        ]
        if any(word in message for word in ("起诉", "法院", "立案")):
            return (
                question,
                (
                    "现有信息暂时不足以判断能否直接起诉。能否起诉以及"
                    "应向哪里提出，取决于能否确认对方身份和送达信息、"
                    "双方的借号约定，以及封禁原因、实际损失与对方行为"
                    "之间的关系。"
                ),
                facts_to_verify,
            )
        return (
            question,
            (
                "可以先修改密码、退出其他登录并保护验证码，再向游戏"
                "平台申诉封禁并索取处罚和登录记录。现有信息不足以直接"
                "判断对方责任或最终处理结果。"
            ),
            facts_to_verify,
        )

    return (
        question,
        (
            "现有信息暂时不足以直接判断结论。需要先核对完整经过、"
            "对方身份或负责处理的主体，以及现有证据和实际影响，"
            "再确定下一步。"
        ),
        [
            "事件的完整经过和当前状态",
            "对方身份或负责处理的主体",
            "现有证据和已经发生的实际影响",
        ],
    )


def _topic_label(
    context: dict[str, object],
    topic_id: str,
) -> str | None:
    definitions = context.get("topic_definitions")
    if not isinstance(definitions, Mapping):
        return None
    selected = definitions.get(topic_id)
    if not isinstance(selected, Mapping):
        return None
    label = selected.get("label")
    return label if isinstance(label, str) and label.strip() else None


def _parse_date(message: str) -> date | None:
    match = _DATE_PATTERN.search(message)
    if not match:
        return None
    try:
        return date(
            int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
        )
    except ValueError:
        return None


def _positive(message: str, phrases: tuple[str, ...]) -> bool:
    return any(phrase in message for phrase in phrases)


def _deposit_reason(message: str) -> dict[str, object]:
    if any(word in message for word in ("正常损耗", "自然损耗", "住旧")):
        return {"landlord_reason": "normal_wear"}
    if any(word in message for word in ("钉孔", "钉子眼")):
        return {"landlord_reason": "nail_holes"}
    if any(word in message for word in ("划痕", "掉漆", "磕碰")):
        return {"landlord_reason": "minor_scuff"}
    if any(word in message for word in ("损坏", "弄坏", "破损")):
        return {"landlord_reason": "real_damage"}
    if any(word in message for word in ("水电", "燃气", "物业欠费")):
        return {"landlord_reason": "unpaid_bill"}
    if any(word in message for word in ("提前退租", "违约退租")):
        return {"landlord_reason": "early_exit"}
    if any(word in message for word in ("没理由", "没有理由", "说不清")):
        return {"landlord_reason": "no_reason"}
    return {}


def _contract_deduction_term(message: str) -> bool | None:
    negative_phrases = (
        "合同没写",
        "合同没有写",
        "没有约定",
        "未约定",
        "没约定",
    )
    if any(phrase in message for phrase in negative_phrases):
        return False
    positive_phrases = (
        "合同写了",
        "合同约定",
        "明确约定",
        "约定可以扣",
        "约定能扣",
    )
    if any(phrase in message for phrase in positive_phrases):
        return True
    return None


def _overtime_issue(message: str) -> str:
    if any(word in message for word in ("仲裁管辖", "合同履行地", "单位所在地")):
        return "arbitration_jurisdiction"
    if "时效" in message:
        return "arbitration_limit"
    if "举证" in message or "证据" in message:
        return "evidence_burden"
    if "支付令" in message:
        return "payment_order"
    if "劳动部门" in message or "行政投诉" in message:
        return "administrative_remedy"
    if any(word in message for word in ("调解", "处理路径", "怎么维权")):
        return "dispute_path"
    if "强迫" in message or "强制加班" in message:
        return "forced_overtime"
    if "工资" in message and ("拖欠" in message or "没发" in message):
        return "wage_arrears"
    if any(word in message for word in ("时长", "上限", "每月")):
        return "working_time_limit"
    return "overtime_compensation"


def _return_issue(message: str) -> str:
    if any(word in message for word in ("发错货", "错发", "发错", "补发")):
        return "wrong_delivery"
    if "拆包" in message or "开箱" in message:
        return "unpacked_inspection"
    if "默认勾选" in message or "默认同意" in message:
        return "default_no_return_option"
    if "耐用" in message or "六个月" in message or "举证" in message:
        return "durable_goods_evidence"
    if "质量" in message or "瑕疵" in message:
        return "quality_problem"
    if "订单" in message or "合同成立" in message:
        return "order_formation"
    return "seven_day_return"


def _prepaid_issue(message: str) -> str:
    if "停业" in message or "跑路" in message:
        return "closure"
    if "书面合同" in message or "没签合同" in message:
        return "missing_written_contract"
    if "经营风险" in message or "经营异常" in message:
        return "business_risk"
    if "不退款" in message or "概不退款" in message:
        return "unfair_term"
    if "投诉" in message or "部门" in message:
        return "dispute_channel"
    return "service_not_provided"


def _counterfeit_issue(message: str) -> str:
    if "行政处罚" in message or "罚款" in message:
        return "administrative_penalty"
    if any(word in message for word in ("卖家信息", "真实名称", "联系方式")):
        return "seller_identity"
    if "平台" in message:
        return "platform_responsibility"
    if "三倍" in message or "欺诈" in message:
        return "consumer_fraud"
    if any(word in message for word in ("修理", "更换", "质量不合格")):
        return "quality_remedy"
    return "counterfeit"


def _training_issue(message: str) -> str:
    if "停业" in message:
        return "closure"
    if any(word in message for word in ("质量", "宣传", "师资", "不一样")):
        return "service_quality"
    if "解除后" in message or "恢复原状" in message:
        return "termination_refund"
    if "延期" in message or "不开课" in message:
        return "delayed_service"
    if "合同" in message and "不退" in message:
        return "unfair_term"
    return "service_not_provided"


def _renewal_issue(message: str) -> str:
    if "默认勾选" in message or "搭售" in message:
        return "default_option"
    if "小字" in message or "格式条款" in message:
        return "hidden_term"
    return "no_reminder"


def _renovation_issue(message: str) -> str:
    if "承揽" in message or "交付成果" in message:
        return "contract_nature"
    if "质量" in message or "返工" in message:
        return "quality_problem"
    if "完工前" in message or "随时解除" in message:
        return "owner_termination"
    if "全面履行" in message or "按约履行" in message:
        return "full_performance"
    if "可得利益" in message or "损失赔偿" in message:
        return "damages"
    if "违约金" in message:
        return "penalty_adjustment"
    if any(word in message for word in ("延期", "催告", "不施工", "解除")):
        return "delayed_termination"
    return "other"


def _procedure_issue(message: str) -> str:
    if "鉴定" in message or "评估" in message:
        return "excluded_case"
    if "几个月" in message or "审结" in message:
        return "time_limit"
    if "异议" in message:
        return "objection"
    return "eligibility"
