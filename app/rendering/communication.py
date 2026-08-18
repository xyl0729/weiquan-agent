from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.agent.models import CommunicationGuide
from app.playbooks.evaluator import EvaluationResult
from app.playbooks.schema import Playbook


@dataclass(frozen=True, slots=True)
class _CommunicationProfile:
    recipient: str
    channels: tuple[str, ...]
    when_to_send: str
    objective: str
    request_text: str
    after_sending: tuple[str, ...]
    escalation: tuple[str, ...]
    fact_fields: tuple[str, ...] = ()


_PROFILES: dict[str, _CommunicationProfile] = {
    "auto_renewal": _CommunicationProfile(
        recipient="提供会员或订阅服务的经营者客服或投诉处理部门",
        channels=(
            "经营者官方客服工单",
            "经营者官方电子邮箱",
            "交易平台投诉入口",
        ),
        when_to_send=(
            "关闭后续自动续费并保存订购、提醒和扣款页面后尽快发送"
        ),
        objective="要求说明订购确认与提醒记录，并处理争议扣款",
        request_text=(
            "请提供我接受自动续费服务及扣款前提醒的记录，"
            "说明本次扣款依据，并对争议扣款给出退款或其他处理方案。"
        ),
        after_sending=(
            "保存工单编号、邮件回执、页面截图和每次回复时间",
            "核对自动续费是否已经关闭，并继续留意后续扣款记录",
        ),
        escalation=(
            "在合理期限内未回复时，向交易平台或支付渠道提交申诉",
            "仍无法解决时，携带完整材料选择消费者调解、行政投诉或诉讼",
        ),
        fact_fields=("charge_amount", "charge_date"),
    ),
    "counterfeit_goods": _CommunicationProfile(
        recipient="商品销售者；涉及平台交易时同时发送给平台投诉部门",
        channels=(
            "订单内商家客服",
            "交易平台投诉入口",
            "可留痕的电子邮件或书面函件",
        ),
        when_to_send=(
            "保存商品原物、包装、订单和初步核验材料后尽快发送"
        ),
        objective="要求核验商品情况、披露处理依据并提出明确处理方案",
        request_text=(
            "请核验商品来源和真伪，提供进货或授权等可核对材料，"
            "并书面说明退换修、退款及其他争议请求的处理方案。"
        ),
        after_sending=(
            "保存卖家和平台的送达状态、工单编号及完整回复",
            "继续妥善保存商品、包装、防伪信息和核验材料，不擅自处置原物",
        ),
        escalation=(
            "卖家身份不明或拒绝处理时，要求平台披露经营者真实信息并介入",
            "需要专业真伪判断或涉及人身损害时，及时联系合格检测机构、监管部门或医疗机构",
        ),
        fact_fields=("purchase_amount",),
    ),
    "deposit_deduction": _CommunicationProfile(
        recipient="房东、出租人或其书面授权的房屋管理人员",
        channels=(
            "双方常用且可导出的聊天工具",
            "电子邮件",
            "可证明送达的书面函件",
        ),
        when_to_send=(
            "备份租赁合同、押金凭证、退房影像和扣款说明后立即发送"
        ),
        objective="要求逐项说明扣款依据、返还无争议押金并明确回复期限",
        request_text=(
            "请逐项列明拟扣减的项目、金额、合同依据和对应票据，"
            "并先返还无争议部分；如不同意返还，请书面说明理由和证据。"
        ),
        after_sending=(
            "保存发送原文、送达状态、对方回复和押金返还记录",
            "将每一项扣款与合同条款、房屋影像及维修票据逐项核对",
        ),
        escalation=(
            "在合理期限内未说明或未返还时，携带材料申请调解",
            "协商或调解不成时，根据约定和管辖情况选择仲裁或诉讼",
        ),
        fact_fields=(
            "deposit_amount",
            "withheld_amount",
            "contract_has_deduction_term",
            "lease_end_date",
            "damage_description",
            "repair_cost",
        ),
    ),
    "overtime_pay": _CommunicationProfile(
        recipient="用人单位人力资源、工资核算负责人或有权处理劳动争议的负责人",
        channels=(
            "单位官方电子邮箱",
            "内部工单或办公系统",
            "可证明送达的书面函件",
        ),
        when_to_send=(
            "完成逐日工时与工资差额明细并核对仲裁时效后尽快发送"
        ),
        objective="要求核对考勤和工资记录，并书面处理劳动报酬争议",
        request_text=(
            "请核对并提供相关期间的考勤、排班和工资计算记录，"
            "对我提交的工时及工资差额明细逐项回复，并说明支付或更正安排。"
        ),
        after_sending=(
            "保存邮件、工单、签收凭证及单位提供的考勤和工资材料",
            "持续记录单位回复、支付情况以及每次主张权利的日期",
        ),
        escalation=(
            "单位拒绝核对或长期不回复时，向劳动行政部门咨询或投诉",
            "需要调解或仲裁时，尽快确认受理机构并再次核对仲裁时效",
        ),
        fact_fields=("claimed_amount", "dispute_date"),
    ),
    "prepaid_card": _CommunicationProfile(
        recipient="预付卡或预付款服务的经营者客服、门店负责人或投诉处理部门",
        channels=(
            "经营者官方客服工单",
            "门店书面签收",
            "经营者官方电子邮箱",
        ),
        when_to_send=(
            "导出合同、付款记录、消费明细和账户余额后尽快发送"
        ),
        objective="要求说明继续履行或退款安排及金额计算方式",
        request_text=(
            "请核对我的付款、已消费和未消费记录，"
            "书面说明后续履行安排或退款金额、扣减项目和计算方式。"
        ),
        after_sending=(
            "保存工单、签收凭证、账户页面及经营者的完整回复",
            "持续记录门店营业状态、履行情况和账户余额变化",
        ),
        escalation=(
            "经营异常、停业或拒绝处理时，尽快向消费者组织或主管部门求助",
            "协商无果时，携带合同和金额明细选择行政投诉、仲裁或诉讼",
        ),
        fact_fields=("remaining_amount",),
    ),
    "renovation_default": _CommunicationProfile(
        recipient="装修施工方项目负责人及签约公司的投诉或法务负责人",
        channels=(
            "项目工作群或双方常用聊天工具",
            "签约公司官方电子邮箱",
            "可证明送达的书面函件",
        ),
        when_to_send=(
            "完成现场影像、工程进度、缺陷和付款材料保全后尽快发送"
        ),
        objective="要求在明确期限内提出复工、修复、结算或解除方案",
        request_text=(
            "请对工程进度、质量问题和双方已确认的变更逐项回复，"
            "并在书面回复中提出复工、修复、结算或解除的具体方案和时间表。"
        ),
        after_sending=(
            "保存原文、送达凭证、现场变化及对方提出的处理时间表",
            "需要拆除或返工前，先记录现状并保存报价、检测或鉴定材料",
        ),
        escalation=(
            "存在施工安全风险时，先停止危险作业并联系有资质的专业人员核查",
            "逾期未处理时，根据合同约定和证据情况选择调解、仲裁或诉讼",
        ),
        fact_fields=("contract_amount", "claimed_amount"),
    ),
    "return_refused": _CommunicationProfile(
        recipient="商品销售者客服；平台交易时同时发送给平台售后或投诉部门",
        channels=(
            "订单售后入口",
            "商家客服工单",
            "交易平台投诉入口",
        ),
        when_to_send=(
            "保存订单、实际收货日期、商品现状和退货规则后立即发送"
        ),
        objective="落实用户已经选择的处理诉求，并要求书面说明处理安排或拒绝理由",
        request_text=(
            "请按我在本次沟通中已经明确选择的处理诉求，结合商品现状和"
            "订单材料书面确认补救方式、履行期限、商品是否需要寄回及必要"
            "运输费用；如不能按该诉求处理，请说明具体理由和可执行替代方案。"
        ),
        after_sending=(
            "保存申请时间、售后编号、寄回凭证、物流状态和商家回复",
            "在争议解决前保持商品、包装、配件和赠品现状并继续留存影像",
        ),
        escalation=(
            "商家拒绝或超期不处理时，向交易平台提交完整材料要求介入",
            "平台处理后仍有争议时，选择消费者调解、行政投诉或诉讼",
        ),
        fact_fields=("purchase_amount", "purchase_date", "received_date"),
    ),
    "small_claim_procedure": _CommunicationProfile(
        recipient="受理案件的人民法院立案部门、承办法官或书记员",
        channels=(
            "法院诉讼服务平台",
            "法院指定的材料提交窗口",
            "可证明送达的邮寄渠道",
        ),
        when_to_send=(
            "收到适用小额诉讼的告知或发现程序排除情形后尽快提交"
        ),
        objective="请求确认程序适用依据、材料接收情况并审查书面异议",
        request_text=(
            "请确认本案适用程序及金额标准的依据，并核对我提交的案件类型、"
            "标的额和可能影响程序适用的材料；如本函包含程序异议，请依法审查并告知结果。"
        ),
        after_sending=(
            "保存提交页面、材料清单、签收凭证和法院回复",
            "记录立案、告知、异议提交及审查结果的时间线",
        ),
        escalation=(
            "材料未被接收或程序信息不清时，联系诉讼服务渠道核实办理方式",
            "程序影响重大且仍无法确认时，携带全部材料向专业法律服务人员求助",
        ),
        fact_fields=("claim_amount", "filing_date"),
    ),
    "training_refund": _CommunicationProfile(
        recipient="培训机构客服、校区负责人或有权处理退费的投诉部门",
        channels=(
            "培训机构官方客服工单",
            "校区书面签收",
            "培训机构官方电子邮箱",
        ),
        when_to_send=(
            "备份合同、宣传材料、付款和课时记录后尽快发送"
        ),
        objective="要求说明后续履行或退费方案及扣费计算依据",
        request_text=(
            "请核对合同、付款、已上和未上课时记录，"
            "书面说明后续履行安排或退费金额、扣费项目和计算方式。"
        ),
        after_sending=(
            "保存工单、签收凭证、课程账户页面和机构完整回复",
            "继续记录排课、停课、催告、解除通知及退款进度",
        ),
        escalation=(
            "机构停业、失联或拒绝处理时，尽快向消费者组织或主管部门求助",
            "协商无果时，携带合同和课时金额明细选择行政投诉、仲裁或诉讼",
        ),
        fact_fields=("remaining_amount",),
    ),
}

_FACT_LABELS: dict[tuple[str, str], str] = {
    ("auto_renewal", "charge_amount"): "争议扣款金额",
    ("auto_renewal", "charge_date"): "争议扣款日期",
    ("counterfeit_goods", "purchase_amount"): "购买金额",
    ("deposit_deduction", "deposit_amount"): "押金总额",
    ("deposit_deduction", "withheld_amount"): "拟扣或未返还金额",
    (
        "deposit_deduction",
        "contract_has_deduction_term",
    ): "合同是否明确约定该扣减情形",
    ("deposit_deduction", "lease_end_date"): "退租日期",
    ("deposit_deduction", "damage_description"): "对方所称损坏情况",
    ("deposit_deduction", "repair_cost"): "所称维修金额",
    ("overtime_pay", "claimed_amount"): "主张金额",
    ("overtime_pay", "dispute_date"): "争议发生日期",
    ("prepaid_card", "remaining_amount"): "可核实剩余金额",
    ("renovation_default", "contract_amount"): "合同金额",
    ("renovation_default", "claimed_amount"): "主张金额",
    ("return_refused", "purchase_amount"): "购买金额",
    ("return_refused", "purchase_date"): "购买日期",
    ("return_refused", "received_date"): "实际收货日期",
    ("small_claim_procedure", "claim_amount"): "诉讼标的额",
    ("small_claim_procedure", "filing_date"): "立案日期",
    ("training_refund", "remaining_amount"): "可核实剩余金额",
}

_MONEY_FIELDS = frozenset(
    {
        "charge_amount",
        "purchase_amount",
        "deposit_amount",
        "withheld_amount",
        "repair_cost",
        "claimed_amount",
        "remaining_amount",
        "contract_amount",
        "claim_amount",
    }
)


def build_communication_guide(
    playbook: Playbook,
    evaluation: EvaluationResult,
) -> CommunicationGuide:
    try:
        profile = _PROFILES[playbook.id]
    except KeyError as exc:
        raise ValueError(
            f"正式 Playbook 缺少沟通指南配置: {playbook.id}"
        ) from exc

    unknown_facts = set(evaluation.facts) - set(playbook.slot_names)
    if unknown_facts:
        raise ValueError(
            "沟通指南收到未声明事实槽位: "
            + ", ".join(sorted(unknown_facts))
        )
    undeclared_profile_fields = set(profile.fact_fields) - set(
        playbook.slot_names
    )
    if undeclared_profile_fields:
        raise ValueError(
            "沟通指南配置引用未声明事实槽位: "
            + ", ".join(sorted(undeclared_profile_fields))
        )

    fact_items = [f"事项类型：{playbook.name}"]
    fact_items.extend(
        _format_fact(playbook.id, name, evaluation.facts[name])
        for name in profile.fact_fields
        if name in evaluation.facts
        and evaluation.facts[name] is not None
    )
    facts_text = "；".join(fact_items)
    message = (
        f"您好，我就{playbook.name}事项正式提出书面处理请求。\n"
        f"已确认信息：{facts_text}。\n"
        f"当前基于已核验规则的整理结果为“{evaluation.verdict_label}”。"
        f"{_ensure_sentence(evaluation.key_point)}\n"
        f"{profile.request_text}\n"
        "请书面确认收到，并告知处理人、处理方案和预计回复时间。谢谢。"
    )
    return CommunicationGuide(
        recipient=profile.recipient,
        channels=list(profile.channels),
        when_to_send=profile.when_to_send,
        objective=profile.objective,
        message=message,
        after_sending=list(profile.after_sending),
        escalation=list(profile.escalation),
        required_before_send=[],
    )


def _format_fact(playbook_id: str, name: str, value: Any) -> str:
    label = _FACT_LABELS[(playbook_id, name)]
    if name in _MONEY_FIELDS:
        rendered = f"{_format_number(value)} 元"
    elif isinstance(value, bool):
        rendered = "是" if value else "否"
    else:
        rendered = str(value).strip()
        if len(rendered) > 300:
            rendered = rendered[:300].rstrip() + "（内容已截短）"
    return f"{label}：{rendered}"


def _format_number(value: Any) -> str:
    number = float(value)
    if number.is_integer():
        return f"{int(number):,}"
    return f"{number:,.2f}".rstrip("0").rstrip(".")


def _ensure_sentence(value: str) -> str:
    normalized = value.strip()
    if normalized.endswith(("。", "！", "？", ".", "!", "?")):
        return normalized
    return normalized + "。"
