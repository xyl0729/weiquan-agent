from __future__ import annotations

import re
from collections import deque
from collections.abc import Iterable
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
    ),
    "counterfeit_goods": ("假货", "以假充真", "三倍赔偿", "掺假"),
    "training_refund": ("培训", "网课", "课程", "退学费"),
    "auto_renewal": ("自动续费", "连续包月", "自动扣款", "默认勾选"),
    "renovation_default": ("装修", "施工", "返工", "工程延期"),
    "small_claim_procedure": ("小额诉讼", "一审终审", "审结", "程序异议"),
}

_DATE_PATTERN = re.compile(
    r"(?P<year>20\d{2})[-年/.](?P<month>\d{1,2})[-月/.](?P<day>\d{1,2})日?"
)
_MONEY_PATTERN = re.compile(
    r"(?:人民币|￥|¥)?\s*(\d+(?:\.\d+)?)\s*(?:元|块)"
)


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
        error: ProviderError | None = None,
    ) -> None:
        self._responses = deque(responses or ())
        self._continuation_responses = deque(
            continuation_responses or ()
        )
        self._error = error
        self.extraction_calls = 0
        self.continuation_calls = 0
        self.extraction_evidence_calls: list[
            tuple[AttachmentEvidenceContext, ...]
        ] = []
        self.continuation_evidence_calls: list[
            tuple[AttachmentEvidenceContext, ...]
        ] = []

    async def extract_facts(
        self,
        message: str,
        context: dict[str, object],
        evidence: tuple[AttachmentEvidenceContext, ...] = (),
    ) -> ExtractionResult:
        self.extraction_calls += 1
        self.extraction_evidence_calls.append(tuple(evidence))
        if self._error is not None:
            raise self._error
        if self._responses:
            return self._responses.popleft()

        scenario_id = self._classify(message, context)
        facts = self._extract_known_facts(scenario_id, message)
        definition = scenario_definition(context, scenario_id)
        allowed_slots = {
            str(name)
            for name in definition.get("allowed_slot_names", [])
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
        return ExtractionResult(
            scenario_id=scenario_id,
            facts=facts,
            unknown_slots=unknown,
            confidence=0.99 if scenario_id != "unsupported" else 0.0,
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
    ) -> CaseContinuationResult:
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

    async def polish_text(self, draft: PolishingDraft) -> str:
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
            score = sum(keyword in message for keyword in keywords)
            if score > best_score:
                best_id = scenario_id
                best_score = score
        return best_id

    @staticmethod
    def _classify(message: str, context: dict[str, object]) -> str:
        current = context.get("current_scenario_id")
        if isinstance(current, str) and current:
            return current

        allowed = {
            str(item)
            for item in context.get("allowed_scenario_ids", [])
            if isinstance(item, str)
        }
        best_id = "unsupported"
        best_score = 0
        for scenario_id, keywords in _SCENARIO_KEYWORDS.items():
            if allowed and scenario_id not in allowed:
                continue
            score = sum(keyword in message for keyword in keywords)
            if score > best_score:
                best_id = scenario_id
                best_score = score
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
