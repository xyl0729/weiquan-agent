from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.agent.models import (
    CommunicationGuide,
    CoverageResult,
    GuidanceResult,
    RiskFlag,
)
from app.agent.routing import is_unknown_fact_placeholder
from app.retrieval.expansion import infer_topic


@dataclass(frozen=True, slots=True)
class _GuidanceProfile:
    recipient: str
    channels: tuple[str, ...]
    objective: str
    evidence: tuple[str, ...]
    actions: tuple[str, ...]
    escalation: tuple[str, ...]
    question: str


@dataclass(frozen=True, slots=True)
class GuidanceProgress:
    stage: int
    text: str
    action: str
    next_question: str | None


_PROFILES: dict[str, _GuidanceProfile] = {
    "education_minor_safety": _GuidanceProfile(
        recipient="学校校长、年级负责人或指定的学生保护负责人",
        channels=("学校官方邮箱或校务平台", "当面提交并要求签收"),
        objective="要求确认收到情况反映，并说明保护、核查和反馈安排",
        evidence=(
            "记录事件时间、地点、在场人员和孩子的原话",
            "保存伤情照片、就医材料、通知和学校沟通记录",
        ),
        actions=(
            "先确认孩子目前是否安全，并避免继续接触可能造成伤害的人",
            "以书面方式要求学校说明临时保护措施、核查负责人和反馈时间",
        ),
        escalation=("学校未采取保护措施时，向上级教育主管机构咨询",),
        question="孩子目前是否仍处在可能继续受到伤害的环境中？",
    ),
    "medical_service_dispute": _GuidanceProfile(
        recipient="医疗机构医务部门、病案管理部门或投诉处理负责人",
        channels=("医疗机构官方投诉渠道", "书面申请并要求回执"),
        objective="要求核对诊疗记录，并说明材料提供和争议处理安排",
        evidence=(
            "保存挂号、缴费、检查、用药和出院材料",
            "按时间顺序记录沟通对象、诊疗经过和目前影响",
        ),
        actions=(
            "书面申请与本人诊疗有关的可提供材料并保留回执",
            "把收费、记录、说明或服务争议分别列出，要求逐项回应",
        ),
        escalation=("无法取得说明时，向当地卫生健康主管机构咨询",),
        question="当前最需要解决的是病历材料、收费问题，还是诊疗后果？",
    ),
    "traffic_accident": _GuidanceProfile(
        recipient="事故处理联系人、承保机构或维修服务负责人",
        channels=("可留痕的事故处理平台", "短信、电子邮件或书面材料"),
        objective="确认事故信息，并明确查勘、材料提交和处理节点",
        evidence=(
            "保存现场全景、车辆位置、损坏部位和道路环境影像",
            "保存事故记录、就医材料、维修报价和全部联系记录",
        ),
        actions=(
            "如现场仍有风险，先转移到安全位置并根据伤情寻求救助",
            "向处理方逐项确认查勘、医疗、维修和材料提交要求",
        ),
        escalation=("处理长期停滞或存在人伤争议时，向专业人员咨询",),
        question="事故中是否有人受伤或仍需要立即就医？",
    ),
    "personal_injury": _GuidanceProfile(
        recipient="场所管理方、相关服务方或事件处理负责人",
        channels=("现场服务台并要求登记", "官方邮箱或投诉工单"),
        objective="要求保存现场记录，并说明核查和后续联系安排",
        evidence=(
            "保存伤情、现场环境、警示设施和物品状态的影像",
            "保存就医票据、诊断材料、证人线索和沟通记录",
        ),
        actions=(
            "先根据伤情就医，并如实向医务人员说明受伤经过",
            "尽快书面通知场所或相关方保存监控和事件登记",
        ),
        escalation=("伤情明显或事实争议较大时，向专业人员咨询",),
        question="受伤后是否已经就医并留下诊疗记录？",
    ),
    "labor_termination": _GuidanceProfile(
        recipient="用人单位人力资源部门或有权作出决定的负责人",
        channels=("工作邮箱或内部人事系统", "书面送达并要求签收"),
        objective="确认用工安排、决定内容、生效时间和材料依据",
        evidence=(
            "保存劳动合同、入职材料、工资记录和工作安排",
            "保存解除通知、谈话录音、聊天记录和交接要求",
        ),
        actions=(
            "要求单位书面说明当前决定、生效日期和交接安排",
            "在未核对文件内容前，不签署含义不明或与事实不符的材料",
        ),
        escalation=("沟通无结果时，向当地劳动公共服务机构咨询",),
        question="单位是否已经提供书面的解除或离职文件？",
    ),
    "wage_social_insurance": _GuidanceProfile(
        recipient="用人单位人力资源、财务部门或负责薪酬的负责人",
        channels=("工作邮箱或内部工单", "书面清单并要求确认收到"),
        objective="核对欠付项目、计算期间和明确支付或补正安排",
        evidence=(
            "保存劳动合同、考勤、工资条和银行入账记录",
            "整理每个期间应付、实付及差额的对照表",
        ),
        actions=(
            "把工资和社会保险问题分项列明，并要求书面核对",
            "保存单位对金额、期间和处理日期的全部回复",
        ),
        escalation=("未得到明确安排时，向当地劳动公共服务机构咨询",),
        question="争议主要是工资未付、少付，还是社会保险缴纳问题？",
    ),
    "workplace_harassment": _GuidanceProfile(
        recipient="用人单位合规、人力资源或指定投诉受理负责人",
        channels=("保密投诉邮箱或内部合规渠道", "书面提交并保存回执"),
        objective="要求确认收到，并说明保密、保护和调查安排",
        evidence=(
            "按时间记录具体言行、地点、在场人员和后续影响",
            "保存原始聊天、邮件、工作安排变化和已有投诉记录",
        ),
        actions=(
            "优先确保自身安全，并避免单独进入可能再次发生侵害的场景",
            "通过可留痕渠道提出具体事实和希望采取的保护措施",
        ),
        escalation=("内部渠道无法提供保护时，向主管机构或专业人员咨询",),
        question="相关行为是否仍在持续，或已经影响到你的人身安全？",
    ),
    "debt_collection": _GuidanceProfile(
        recipient="借款相对方、债务处理联系人或催收投诉负责人",
        channels=("可留痕的短信或即时通信", "电子邮件或书面函件"),
        objective="核对款项、形成过程、已付款项和后续沟通安排",
        evidence=(
            "保存转账凭证、借据、聊天记录和还款记录",
            "整理本金、已付款、约定时间和每次催告的时间线",
        ),
        actions=(
            "先核对对方身份和款项明细，不向不明账户继续付款",
            "以书面方式确认争议金额和希望对方回复的具体事项",
        ),
        escalation=("出现威胁、持续骚扰或金额复杂时，向专业人员咨询",),
        question="是否有转账凭证、借据或对方确认欠款的记录？",
    ),
    "payment_fraud": _GuidanceProfile(
        recipient="付款机构的风险处理部门、交易平台或账户安全负责人",
        channels=("官方账户安全或交易申诉入口", "官方客服工单"),
        objective="要求登记异常交易，并说明止付、核查和材料提交步骤",
        evidence=(
            "保存订单、转账流水、收款账户和完整聊天记录",
            "记录发现异常的时间以及已联系过的机构和处理编号",
        ),
        actions=(
            "停止继续转账、共享验证码或安装对方要求的软件",
            "立即通过付款机构的官方渠道报告异常交易并保存受理记录",
        ),
        escalation=("存在持续损失风险时，尽快联系能够处理紧急风险的机构",),
        question="这笔款项是否刚刚转出，当前还能否在付款渠道申请止付？",
    ),
    "general_rental": _GuidanceProfile(
        recipient="出租人、房屋管理人或租赁服务机构负责人",
        channels=("租赁平台消息或电子邮件", "书面通知并保留送达记录"),
        objective="核对租赁事实，并明确维修、退租或费用处理安排",
        evidence=(
            "保存租赁合同、支付记录、交接材料和房屋现状影像",
            "保存维修申请、费用通知和双方沟通记录",
        ),
        actions=(
            "把维修、费用、退租或使用问题分别列明并附现有材料",
            "要求对方确认收到并给出处理联系人和预计时间",
        ),
        escalation=("影响基本居住安全或长期不处理时，向主管机构咨询",),
        question="当前最紧迫的是居住安全、维修、费用，还是退租安排？",
    ),
    "property_neighbor": _GuidanceProfile(
        recipient="物业服务负责人、业主组织或相关管理联系人",
        channels=("物业报修或投诉工单", "书面提交并要求登记编号"),
        objective="要求登记问题、现场核查并说明处理计划",
        evidence=(
            "按时间保存漏水、噪音、停车或公共区域问题的影像",
            "保存报修编号、检测材料、损失清单和每次回复",
        ),
        actions=(
            "通过正式渠道登记问题，并要求提供工单编号",
            "明确希望现场核查的事项、可联系时间和反馈方式",
        ),
        escalation=("涉及持续安全风险或多次不处理时，向主管机构咨询",),
        question="问题是否仍在持续，并可能继续扩大损失或影响安全？",
    ),
    "privacy_reputation": _GuidanceProfile(
        recipient="发布者、平台投诉处理或个人信息保护负责人",
        channels=("平台举报或申诉入口", "官方隐私投诉邮箱或工单"),
        objective="要求保存后台记录、限制继续传播并说明处理结果",
        evidence=(
            "保存包含账号、链接、发布时间和完整上下文的截图",
            "记录传播范围、投诉编号和对生活工作的实际影响",
        ),
        actions=(
            "先调整账号安全和公开范围，避免进一步暴露敏感信息",
            "通过平台正式入口提交具体链接和希望采取的处理措施",
        ),
        escalation=("出现现实威胁或持续扩散时，向能够提供保护的机构咨询",),
        question="相关内容目前是否仍在公开传播或带来现实人身威胁？",
    ),
    "family_support_property": _GuidanceProfile(
        recipient="相关家庭成员、共同财产管理人或调解服务联系人",
        channels=("可留痕的书面沟通", "调解或公共服务预约渠道"),
        objective="确认争议事项、现状安排和下一次沟通节点",
        evidence=(
            "整理身份关系、共同生活和费用支付等基础材料",
            "保存财产凭证、转账记录、照护安排和书面沟通",
        ),
        actions=(
            "把抚养、探望、费用和财产问题分开整理，避免混在一次争论中",
            "先提出能够被明确回应的当前安排，并保存回复",
        ),
        escalation=("涉及未成年人安全或重大财产处置时，向专业人员咨询",),
        question="当前最需要先确定的是孩子安排、生活费用，还是财产保全？",
    ),
    "service_contract": _GuidanceProfile(
        recipient="服务提供方、平台客服或合同履行负责人",
        channels=("平台正式客服工单", "电子邮件或书面投诉渠道"),
        objective="核对服务约定、已履行内容和明确后续处理安排",
        evidence=(
            "保存订单、合同、付款记录和服务页面说明",
            "整理承诺内容、实际履行、差异和历次沟通",
        ),
        actions=(
            "把未履行或不一致的项目逐项列出，并附对应材料",
            "提出具体可执行请求，要求确认收到和回复时间",
        ),
        escalation=("服务方持续不回应时，向相应行业主管机构咨询",),
        question="你希望对方优先完成服务、重新处理，还是核对退款？",
    ),
    "logistics_travel_food": _GuidanceProfile(
        recipient="承运、旅行或食品服务方的投诉处理负责人",
        channels=("订单内售后或投诉入口", "官方客服工单或电子邮件"),
        objective="登记订单问题，并明确核查、材料和处理时间",
        evidence=(
            "保存订单、包装、商品或现场状态的完整影像",
            "保存支付记录、检测或就医材料以及全部客服记录",
        ),
        actions=(
            "涉及身体不适时先就医，并保留剩余物品和购买信息",
            "通过订单对应的正式入口登记问题并取得受理编号",
        ),
        escalation=("出现人身健康风险或重大损失时，向主管机构咨询",),
        question="当前是否有人身体不适，或物品仍需立即保全？",
    ),
    "game_account_dispute": _GuidanceProfile(
        recipient="游戏平台账号安全、封禁申诉或投诉处理负责人",
        channels=("游戏平台官方账号申诉入口", "官方客服工单或电子邮件"),
        objective="核对账号登录和处罚记录，并说明封禁申诉、账号保护及材料提交安排",
        evidence=(
            "保存账号归属信息、借号约定、完整聊天记录和对方身份线索",
            "保存充值记录、异常登录记录、封禁通知、处罚原因和平台申诉记录",
        ),
        actions=(
            "立即修改密码、退出其他设备登录并保护验证码和绑定信息",
            "通过游戏平台官方入口申诉封禁，要求核对登录、违规和处罚记录",
        ),
        escalation=("平台申诉无结果或损失争议较大时，整理材料后向专业人员咨询",),
        question="账号目前是否仍可能被对方登录，平台是否已经提供具体封禁原因？",
    ),
    "unknown": _GuidanceProfile(
        recipient="与事件直接相关、能够登记和处理问题的负责人",
        channels=("对方官方投诉或服务渠道", "可保存送达记录的书面方式"),
        objective="确认收到情况说明，并告知处理联系人和下一步安排",
        evidence=(
            "按时间顺序记录发生经过、人物、地点和当前影响",
            "保存合同、付款、照片、消息、工单等原始材料",
        ),
        actions=(
            "先确认是否存在人身、财产或证据方面的紧急风险",
            "把最希望解决的一件事写成明确请求，通过可留痕渠道发送",
        ),
        escalation=("无法判断处理机构或风险较高时，向专业人员咨询",),
        question="目前最希望先解决的问题是什么，是否有正在扩大的风险？",
    ),
}

_EMERGENCY_FIRST_ACTION: dict[RiskFlag, str] = {
    "immediate_danger": (
        "立即离开危险现场，前往有其他人在场的安全地点并寻求现实帮助"
    ),
    "minor_harm": (
        "立即设法停止伤害并让未成年人到可信任成年人陪同的安全地点"
    ),
    "urgent_medical": "立即就医或联系能够提供紧急医疗救助的机构",
    "suspected_crime": "先确保人身安全，避免单独接触或质问可能实施侵害的人",
    "fraud_loss": "立即停止转账、共享验证码或继续按对方指示操作",
    "evidence_loss": "先在确保安全的前提下保存即将消失的原始记录",
}



# 路由定不出主题时写入 topic_id 的哨兵值，同时也是兜底档案的键。
_UNKNOWN_TOPIC = "unknown"


def _select_profile(
    coverage: CoverageResult,
    message: str,
) -> _GuidanceProfile:
    """选主题档案，topic_id 认不出时用消息本身再推断一次。

    _PROFILES 里 16 套档案的键与 infer_topic 的返回值域一一对应
    （也与 GENERAL_BASIS_REFS 的键一致），所以路由没定出主题时，
    直接拿消息问 infer_topic 就能落到已有档案上，不需要另写兜底文案。

    只在路由没定出主题时才推断：路由已经识别出主题的轮次不应被触发词
    覆盖，那是路由的判断，比单句触发词看得全。

    注意 "unknown" 本身就是 _PROFILES 的一个键，判定不能写成
    `_PROFILES.get(topic_id) is not None`——那样 topic_id="unknown"
    会直接命中兜底档案并返回，infer_topic 永远不会被调用。
    第一版就是这么写的，904 个测试全绿但档案一个都没变，
    是实测探针发现的。

    背景：甲醛案的 topic_id 至今是 unknown，法条靠 infer_topic 兜底
    才拿到，但 recipient/objective/escalation 仍走通用档案——同一条
    消息在依据侧被认成租赁纠纷、在文案侧却当作不明主题。这里补的是
    这个不一致。
    """
    if coverage.topic_id != _UNKNOWN_TOPIC:
        profile = _PROFILES.get(coverage.topic_id)
        if profile is not None:
            return profile
    if message:
        inferred = infer_topic(message)
        if inferred is not None:
            fallback = _PROFILES.get(inferred)
            if fallback is not None:
                return fallback
    return _PROFILES[_UNKNOWN_TOPIC]


class GuidanceBuilder:
    def build(
        self,
        coverage: CoverageResult,
        *,
        facts: dict[str, Any],
        message: str = "",
    ) -> GuidanceResult:
        if coverage.mode == "formal":
            raise ValueError("正式覆盖应由 Playbook 构建方案")
        profile = _select_profile(coverage, message)
        if coverage.mode == "emergency_guidance":
            return self._emergency(coverage, profile, facts)
        return self._unverified(coverage, profile, facts)

    def build_unverified_stage(
        self,
        coverage: CoverageResult,
        *,
        stage: int,
        message: str = "",
    ) -> GuidanceProgress:
        if coverage.mode != "unverified_guidance":
            raise ValueError("阶段推进只适用于未核验主题")
        if not 1 <= stage <= 7:
            raise ValueError("未核验主题阶段必须在 1 到 7 之间")
        profile = _select_profile(coverage, message)
        if stage == 1:
            return GuidanceProgress(
                stage=stage,
                text="先确认当前安全与损失状态，再开始后续处理。",
                action=profile.actions[0],
                next_question=profile.question,
            )
        if stage == 2:
            return GuidanceProgress(
                stage=stage,
                text=(
                    "接下来先整理现有证据。"
                    "把材料按事件发生顺序归档，保留原始文件。"
                ),
                action="；".join(profile.evidence),
                next_question=(
                    "现有材料是否已经按时间顺序整理，并保留了原始版本？"
                ),
            )
        if stage == 3:
            return GuidanceProgress(
                stage=stage,
                text=(
                    "现有材料整理好后，进行首次书面联系，"
                    "让对方能够逐项回应并留下送达记录。"
                ),
                action=(
                    f"通过{profile.channels[0]}向{profile.recipient}"
                    f"发送书面请求：{profile.objective}"
                ),
                next_question=(
                    "是否已经发送，并取得发送记录、工单号或签收凭证？"
                ),
            )
        if stage == 4:
            return GuidanceProgress(
                stage=stage,
                text=(
                    "书面联系已经发出后，重点记录对方回复，"
                    "不要只依赖口头承诺。"
                ),
                action=(
                    "保存受理编号、回复时间、处理人员以及对方承诺的处理期限"
                ),
                next_question=(
                    "对方是否已经回复，或给出了受理编号和处理期限？"
                ),
            )
        if stage == 5:
            return GuidanceProgress(
                stage=stage,
                text=(
                    "对方超过约定时间仍未回复时，在原渠道书面催办，"
                    "明确回复日期和仍未解决的事项。"
                ),
                action=(
                    "引用原工单或送达记录，列明未回复事项并设置明确回复日期"
                ),
                next_question="催办后，对方是否给出了明确回复日期？",
            )
        if stage == 6:
            escalation = profile.escalation[0]
            return GuidanceProgress(
                stage=stage,
                text=(
                    "对方明确拒绝处理，或原渠道仍未解决时，"
                    "通过上一级或主管渠道升级反映，"
                    "同时附上此前联系和催办记录。"
                ),
                action=escalation,
                next_question=(
                    "升级渠道是否已经受理，并提供了新的受理编号？"
                ),
            )
        return GuidanceProgress(
            stage=stage,
            text=(
                "现有处理渠道已经走完，"
                "下一步是寻求专业协助核对材料和后续方案。"
            ),
            action=(
                "整理一份包含时间线、核心材料、历次受理编号和当前请求的摘要，"
                "交由专业人员咨询核对"
            ),
            next_question=None,
        )

    def emergency_anchor(self, coverage: CoverageResult) -> str:
        if coverage.mode != "emergency_guidance":
            raise ValueError("安全动作只适用于紧急指导")
        return _EMERGENCY_FIRST_ACTION[coverage.risk_flags[0]]

    def _unverified(
        self,
        coverage: CoverageResult,
        profile: _GuidanceProfile,
        facts: dict[str, Any],
    ) -> GuidanceResult:
        return GuidanceResult(
            evidence_now=list(profile.evidence),
            actions=list(profile.actions),
            communication_guide=_communication_guide(
                coverage,
                profile,
                facts,
            ),
            limitations=[
                "当前提供的是该类问题的一般处理建议，具体责任和适用规则"
                "仍需结合事实与证据进一步核对。",
                "以下内容用于整理信息、留存材料和联系处理渠道，不代替"
                "针对个案的专业法律意见。",
            ],
            next_question=profile.question,
        )

    def _emergency(
        self,
        coverage: CoverageResult,
        profile: _GuidanceProfile,
        facts: dict[str, Any],
    ) -> GuidanceResult:
        primary_flag = coverage.risk_flags[0]
        first_action = _EMERGENCY_FIRST_ACTION[primary_flag]
        actions = [
            first_action,
            "联系身边可信任的人或能够提供现场保护、医疗、止损的机构",
            "安全稳定后，记录已经采取的措施、受理时间和后续联系人",
        ]
        evidence = [
            "在不增加风险的情况下保存原始消息、交易、照片或事件记录",
            *profile.evidence[:1],
        ]
        guide = _communication_guide(
            coverage,
            profile,
            facts,
            emergency=True,
        )
        return GuidanceResult(
            evidence_now=evidence,
            actions=actions,
            communication_guide=guide,
            limitations=[
                "不要为了取证继续置身危险，也不要单独与可能造成伤害的人对峙。",
                "本结果只给出安全优先的通用步骤，不替代现场救助、"
                "医疗判断或专业核验。",
            ],
            next_question=None,
        )


def _communication_guide(
    coverage: CoverageResult,
    profile: _GuidanceProfile,
    facts: dict[str, Any],
    *,
    emergency: bool = False,
) -> CommunicationGuide:
    details = _confirmed_details(facts)
    detail_text = "；".join(details)
    if detail_text:
        detail_sentence = f"目前能够确认的信息是：{detail_text}。"
    else:
        detail_sentence = "我正在整理事件经过和现有材料。"
    if emergency:
        opening = (
            f"您好，我需要报告一项与“{coverage.topic_label}”相关的"
            "紧急情况。"
        )
        objective = "请确认收到，并告知当前可以立即采取的保护或处置安排"
        when_to_send = "先确保人身安全并完成必要的紧急处置后立即发送"
    else:
        opening = f"您好，我想书面反映一项与“{coverage.topic_label}”相关的情况。"
        objective = profile.objective
        when_to_send = "整理好现有时间线和原始材料后尽快发送"
    stated_request = _render_fact(facts.get("request"))
    if stated_request:
        # 用户说明了自己的诉求时，正文提出该诉求，再单独请对方书面回复。
        message = (
            f"{opening}{detail_sentence}"
            f"我的当前请求是：{stated_request}。"
            "请确认收到，并以书面方式告知处理联系人、预计时间和"
            "下一步安排。"
        )
    else:
        # 没有明确诉求时，objective 本身就是「请对方确认收到并告知安排」，
        # 再加固定尾句会在同一段里重复两次，因此只保留一句。
        message = f"{opening}{detail_sentence}我的当前请求是：{objective}。"
    return CommunicationGuide(
        recipient=profile.recipient,
        channels=list(profile.channels),
        when_to_send=when_to_send,
        objective=objective,
        message=message,
        after_sending=[
            "保存发送页面、邮件回执、工单编号或签收凭证",
            "记录每次回复的时间、人员和具体处理内容",
        ],
        escalation=list(profile.escalation),
        required_before_send=[],
    )


def _confirmed_details(facts: dict[str, Any]) -> list[str]:
    labels = {
        "event_time": "发生时间",
        "location": "地点",
        "amount": "涉及金额",
        "harm": "目前影响",
        "counterparty": "相关方",
        "evidence_status": "现有材料",
    }
    details: list[str] = []
    for name, label in labels.items():
        value = facts.get(name)
        rendered = _render_fact(value)
        if rendered:
            details.append(f"{label}为{rendered}")
    return details


def _render_fact(value: Any) -> str:
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{value:g}" if isinstance(value, float) else str(value)
    if isinstance(value, str):
        normalized = value.strip()
        return "" if is_unknown_fact_placeholder(normalized) else normalized
    if isinstance(value, list):
        return "、".join(
            item.strip()
            for item in value
            if (
                isinstance(item, str)
                and item.strip()
                and not is_unknown_fact_placeholder(item)
            )
        )
    return ""
