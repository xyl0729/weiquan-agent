from __future__ import annotations

import json
import re
from datetime import date
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    field_validator,
    model_validator,
)

from app.agent.models import CoverageMode, TurnIntent, UsageInfo
from app.agent.progression import normalize_visible_text
from app.retrieval.database import StatuteRecord


BasisScope = Literal["case_specific", "general"]

GENERAL_BASIS_REFS: dict[str, tuple[str, ...]] = {
    "education_minor_safety": (
        "民法典.第一千一百九十九条",
        "民法典.第一千二百条",
        "民法典.第一千二百零一条",
    ),
    "medical_service_dispute": (
        "民法典.第一千二百一十八条",
        "民法典.第一千二百一十九条",
        "民法典.第一千二百二十二条",
        "民法典.第一千二百二十五条",
        "民法典.第一千二百二十六条",
    ),
    "traffic_accident": (
        "民法典.第一千二百零八条",
        "民法典.第一千一百七十九条",
    ),
    "personal_injury": (
        "民法典.第一千一百六十五条",
        "民法典.第一千一百七十九条",
        "民法典.第一千一百九十八条",
        "民法典.第一千二百四十五条",
    ),
    "service_contract": (
        "民法典.第五百零九条",
        "民法典.第五百七十七条",
        "消费者权益保护法.第三十九条",
    ),
    "logistics_travel_food": (
        "食品安全法.第一百四十八条",
        "消费者权益保护法.第三十九条",
        "民法典.第五百七十七条",
    ),
    "wage_social_insurance": (
        "劳动合同法.第三十条",
        "劳动法.第五十条",
        "劳动争议调解仲裁法.第六条",
    ),
    "labor_termination": (
        "劳动争议调解仲裁法.第二条",
        "劳动争议调解仲裁法.第五条",
        "劳动争议调解仲裁法.第六条",
    ),
    "workplace_harassment": (
        "民法典.第一千零一十条",
        "民法典.第一千零二十四条",
        "劳动争议调解仲裁法.第二条",
        "劳动争议调解仲裁法.第六条",
    ),
    "debt_collection": (
        "民法典.第六百六十七条",
        "民法典.第六百七十五条",
        "民法典.第六百八十条",
        "民法典.第九百九十五条",
        "民法典.第一千零三十二条",
    ),
    "payment_fraud": (
        "民法典.第一千一百六十五条",
        "消费者权益保护法.第四十四条",
        "消费者权益保护法.第五十五条",
    ),
    "general_rental": ("民法典.第五百零九条",),
    "property_neighbor": (
        "民法典.第二百八十六条",
        "民法典.第二百八十八条",
        "民法典.第九百四十二条",
    ),
    "privacy_reputation": (
        "民法典.第九百九十五条",
        "民法典.第一千零二十四条",
        "民法典.第一千零三十二条",
        "民法典.第一千零三十四条",
        "民法典.第一千零三十五条",
    ),
    "family_support_property": (
        "民法典.第一千零六十二条",
        "民法典.第一千零六十七条",
        "民法典.第一千零八十六条",
    ),
    "game_account_dispute": (
        "民法典.第五百零九条",
        "消费者权益保护法.第三十九条",
    ),
}
# 分支逻辑按案情追加的条文不在 GENERAL_BASIS_REFS 的静态映射里，
# 必须单独并入白名单，否则会被「引用了依据包之外的法条」拦下。
GENERAL_BASIS_REF_SET = frozenset(
    ref for refs in GENERAL_BASIS_REFS.values() for ref in refs
) | {
    "民法典.第五百七十八条",
    "民法典.第七百一十三条",
    "民法典.第七百零三条",
    "民法典.第七百二十一条",
    "民法典.第七百二十九条",
    "民法典.第七百三十一条",
    "民法典.第七百三十四条",
}

# 报修类问题落在出租人的维修义务上（第七百一十三条：可请求限期维修）。
_REPAIR_MARKERS = (
    "维修",
    "修理",
    "漏水",
    "渗水",
    "坏了",
    "损坏",
    "不能使用",
    "无法使用",
    "不给修",
    "不修",
)

# 危及安全或健康的居住条件另给第七百三十一条：租赁物危及承租人安全或
# 健康时可随时解除合同，即使订立合同时已知质量不合格。
# 这两类要分开——维修义务给的是「可以要求修」，而甲醛超标已经在损害
# 健康，用户真正需要的是「可以直接解约搬走」，让他继续住着等维修
# 并不是这类案情的答案。
_HABITABILITY_MARKERS = (
    "甲醛",
    "空气质量",
    "异味",
    "发霉",
    "霉变",
    "有毒",
    "危及健康",
    "危及安全",
    "住不了",
    "没法住",
    "不能住",
)

# 借车、租车导致车主与使用人不是同一人（第一千二百零九条）。
_BORROWED_VEHICLE_MARKERS = (
    "借车",
    "借了车",
    "借我车",
    "借朋友的车",
    "借了朋友的车",
    "借来的车",
    "租车",
    "租的车",
    "开别人的车",
    "开朋友的车",
    "车主",
    "不是我的车",
)

# 预期违约：对方明确表示或以行为表明不履行（第五百七十八条）。
# 与第五百七十七条的区别是时点——本条适用于履行期限还没到，
# 对方已经明说不办了的情形。
_ANTICIPATORY_BREACH_MARKERS = (
    "明确表示不",
    "明说不",
    "说不退",
    "说不干",
    "不干了",
    "拒绝退",
    "拒不履行",
    "还没到期",
    "没到期",
    "提前说",
    "直接说不",
    "回复说不",
)

# 租赁物毁损、灭失致使不能使用时可请求减租或免租（第七百二十九条）。
# 「还要不要继续交租」是租房咨询里的高频问题，此前没有条文能回答。
_RENT_REDUCTION_MARKERS = (
    "减租",
    "减免租金",
    "少交租",
    "不交租",
    "还要交租",
    "还用交租",
    "要不要交租",
    "断水",
    "断电",
    "查封",
    "泡了",
    "淹了",
    "住不了",
    "没法住",
    "不能住",
)
# 租赁关系是否成立的起点（第七百零三条：租赁合同的定义）。
# 没签书面合同、口头约定、二房东转租这类案情里，用户最先被质疑的
# 不是权利内容而是「你算不算承租人」。先把关系认定立住，后面的
# 维修、减租、解约条文才用得上。
# 触发词限定在关系本身有争议的说法上——正常的租房纠纷不需要回头
# 论证合同成立，无条件给出只会稀释真正对应案情的条文。
_RENTAL_RELATION_MARKERS = (
    "二房东",
    "转租",
    "没签合同",
    "未签合同",
    "没有合同",
    "没签书面",
    "口头说",
    "口头约定",
    "只是口头",
    "不承认租",
    "不承认我租",
    "说我不是租客",
)

# 租金支付期限有争议时的补充规则（第七百二十一条）。
# 本条的价值在没有约定或约定不明时才显现：租期不满一年的期满时付、
# 一年以上的每满一年付。约定清楚的「押一付三」争议靠合同本身解决，
# 因此触发词收在期限本身说不清或被单方改动的说法上。
_RENT_PAYMENT_TERM_MARKERS = (
    "提前交租",
    "提前收租",
    "押一付三",
    "押二付三",
    "交租时间",
    "付租时间",
    "什么时候交租",
    "何时交租",
    "催我交租",
    "没约定租金",
    "租金没约定",
    "没说什么时候交",
)

# 租期届满后继续居住而出租人未提异议的，原合同继续有效但转为不定期
# （第七百三十四条第一款）。
# 这条回答的是「到期了房东突然赶我走」——只要人还住着、房东此前没
# 提异议，合同并未随期限自动终止。
# 注意库内第七百三十四条只录了第一款，第二款的优先承租权不在正文里，
# 所以触发词不覆盖「涨房租后想续租」这类纯优先承租权的问题，避免用
# 一款的正文去答二款的问题。缺口已记入 data/statute_wishlist.yaml。
_RENTAL_HOLDOVER_MARKERS = (
    "到期了",
    "合同到期",
    "租期到期",
    "租期届满",
    "到期后还住",
    "继续住",
    "还住着",
    "不让续租",
    "不给续租",
    "不让我住",
    "赶我走",
)
_NO_RESPONSE_MARKERS = (
    "不理",
    "不回复",
    "没回复",
    "没有回复",
    "不处理",
    "一直不回应",
)
_WRONG_ITEM_MARKERS = ("发错货", "错发", "发错", "货不对", "商品不对")
_REPLACEMENT_MARKERS = ("补发", "重新发货", "重发", "换成正确")
_DEPOSIT_DAMAGE_MARKERS = (
    "划痕",
    "损坏",
    "损耗",
    "墙面",
    "扣全部",
    "全部押金",
    "全扣",
)
_AMOUNT_QUESTION_MARKERS = ("多少", "几倍", "赔多少", "要求多少")
_FOOD_EVIDENCE_CHANGE_MARKERS = ("吃掉", "吃了", "扔掉", "丢掉")
_FOOD_SAFETY_MARKERS = (
    "食品",
    "食物",
    "外卖",
    "餐费",
    "餐品",
    "虫子",
    "异物",
    "中毒",
)
_SCHOOL_NO_CAPACITY_MARKERS = (
    "幼儿园",
    "学前",
    "不满八周岁",
    "无民事行为能力",
)
_SCHOOL_THIRD_PARTY_MARKERS = (
    "同学",
    "校外人员",
    "第三人",
    "欺凌",
    "霸凌",
)
_MEDICAL_RECORD_MARKERS = ("病历", "病案", "诊疗记录", "复制材料")
_MEDICAL_RECORD_WRONGDOING_MARKERS = (
    "不给",
    "不肯",
    "拒绝",
    "隐匿",
    "遗失",
    "伪造",
    "篡改",
    "销毁",
)
_MEDICAL_CONSENT_MARKERS = (
    "知情同意",
    "没有告知",
    "未告知",
    "医疗风险",
    "替代方案",
    "手术",
    "特殊检查",
    "特殊治疗",
)
_MEDICAL_PRIVACY_MARKERS = ("隐私", "个人信息", "公开病历", "泄露病历")
_MEDICAL_DAMAGE_MARKERS = (
    "损害",
    "受伤",
    "后果",
    "误诊",
    "漏诊",
    "治疗",
    "诊疗",
    "赔偿",
)
_INJURY_MARKERS = (
    "受伤",
    "人伤",
    "摔伤",
    "咬伤",
    "医疗费",
    "住院",
    "伤残",
    "死亡",
)
_PUBLIC_PLACE_MARKERS = (
    "宾馆",
    "酒店",
    "商场",
    "银行",
    "车站",
    "机场",
    "体育场馆",
    "娱乐场所",
    "公共场所",
    "公共区域",
)
_ANIMAL_DAMAGE_MARKERS = ("狗咬", "猫咬", "动物", "宠物", "饲养")
_SEXUAL_HARASSMENT_MARKERS = ("性骚扰", "猥亵", "性暗示")
_REPUTATION_MARKERS = ("造谣", "诽谤", "侮辱", "名誉")
_PRIVACY_MARKERS = (
    "隐私",
    "偷拍",
    "人肉",
    "骚扰",
    "生活安宁",
    "爆通讯录",
    "公开欠款",
)
_PERSONAL_INFO_MARKERS = (
    "个人信息",
    "联系方式",
    "电话号码",
    "住址",
    "身份证",
    "邮箱",
)
_LOAN_MARKERS = ("借款", "借钱", "借条", "欠条", "欠款", "民间借贷")
_INTEREST_MARKERS = ("利息", "利率", "高利", "高息")
_COLLECTION_HARASSMENT_MARKERS = (
    "催收",
    "骚扰",
    "爆通讯录",
    "公开欠款",
    "威胁",
)
_PROPERTY_SERVICE_MARKERS = ("物业", "公共区域", "共有部分")
_NEIGHBOR_MARKERS = ("邻居", "楼上", "楼下", "噪音", "漏水", "相邻")
_FAMILY_PROPERTY_MARKERS = ("夫妻财产", "婚姻财产", "共同财产", "财产")
_FAMILY_SUPPORT_MARKERS = ("抚养费", "抚养", "赡养费", "赡养")
_FAMILY_VISIT_MARKERS = ("探望", "看孩子", "见孩子")
_TOPIC_FALLBACK_DIRECT_REPLIES: dict[str, str] = {
    "education_minor_safety": (
        "先确认孩子目前是否安全，并保存事件经过、伤情和沟通记录；"
        "立即通过可留痕渠道要求学校采取临时保护措施、开展核查并明确"
        "负责人和反馈时间。学校仍不处理时，保留提交与回复记录，再向"
        "属地教育主管部门反映。"
    ),
    "medical_service_dispute": (
        "把病历、收费和诊疗后果分开列明，向医院病案管理部门、医务部门"
        "或投诉负责人书面提出具体请求并要求回执。没有书面答复或拒绝"
        "处理时，带上申请记录和诊疗材料向当地卫生健康主管部门咨询或投诉。"
    ),
    "traffic_accident": (
        "如有人受伤或现场仍有危险，先求助、就医并确保现场安全；随后保存"
        "现场影像、事故处理记录、就医材料和联系记录，书面确认事故处理、"
        "保险报案及材料提交节点。"
    ),
    "personal_injury": (
        "先根据伤情就医并如实记录受伤经过，同时保存现场环境、警示设施、"
        "伤情和证人线索；尽快书面通知相关方登记事件并保存监控，后续再按"
        "实际损害和证据核对责任与请求。"
    ),
    "labor_termination": (
        "先要求单位书面说明决定内容、生效时间、理由和交接安排，不要在未"
        "核对含义时签署材料；同时保存劳动合同、工资记录、工作安排和沟通"
        "记录，沟通无果时向当地劳动公共服务机构咨询。"
    ),
    "wage_social_insurance": (
        "先核对劳动合同、工资流水、考勤和参保记录，再通过可留痕渠道要求"
        "单位逐项说明并补正；对方拒绝或长期不处理时，保存送达记录并向"
        "当地劳动保障公共服务或主管机构咨询。"
    ),
    "workplace_harassment": (
        "先避免单独面对可能继续骚扰的人，保存原始消息、录音、证人和每次"
        "事件的时间线；通过单位正式投诉渠道要求受理、保护和调查，并保留"
        "回执。涉及现实威胁或人身危险时，优先求助和报警。"
    ),
    "debt_collection": (
        "把借贷本身与催收方式分开处理：保存借条、转账和还款约定，并通过"
        "书面方式确认欠款和履行安排；对骚扰、威胁、爆通讯录或公开信息的"
        "行为另行固定证据，要求停止并向相应平台或主管渠道投诉。"
    ),
    "payment_fraud": (
        "先联系银行或支付平台申请止付、冻结或争议处理，并尽快报警；完整"
        "保存转账凭证、对方账号、聊天记录、页面链接和受理编号，不要继续"
        "转账，也不要按对方指示删除记录或共享验证码。"
    ),
    "general_rental": (
        "先核对租赁合同、房屋现状、付款和报修记录，再通过可留痕渠道向"
        "房东或出租方明确提出维修、返还或其他具体请求并给出合理答复期限；"
        "保存"
        "送达和回复记录，便于后续选择调解、投诉或诉讼渠道。"
    ),
    "property_neighbor": (
        "先用照片、视频和时间线固定漏水、噪声或公共区域问题，并书面通知"
        "邻居和物业登记、查验、采取临时措施并明确反馈时间；持续不处理时，"
        "带着工单和损害材料向相应主管或调解渠道反映。"
    ),
    "privacy_reputation": (
        "先完整保存账号、链接、发布时间、传播内容和上下文，再要求发布者"
        "停止传播、删除相关内容并保留发送记录；同时向平台举报，要求保存"
        "后台记录并反馈处理结果。涉及持续威胁或现实人身风险时及时报警。"
    ),
    "family_support_property": (
        "先把身份关系、孩子或老人的实际需要、既往支付、探望沟通和财产"
        "来源分别整理，并通过可留痕方式提出具体请求；协商无果时，携带"
        "身份、支出和沟通材料向当地公共法律服务或有管辖权的法院咨询。"
    ),
    "service_contract": (
        "先核对合同、付款、服务承诺和实际履行情况，再书面要求对方在明确"
        "期限内继续履行、说明或处理退款，并保存送达和工单记录；对方拒绝"
        "后，再按合同关系选择平台投诉、调解或诉讼渠道。"
    ),
    "logistics_travel_food": (
        "先保存订单、付款、商品或服务现状和完整沟通记录，再通过订单售后"
        "或经营者正式渠道提出具体请求并取得工单；涉及人身不适时先就医，"
        "涉及物品状态时避免在完成取证前继续改变或丢弃。"
    ),
    "game_account_dispute": (
        "先保存游戏账号归属、实名或绑定信息、充值记录、封禁通知和申诉"
        "记录，通过平台正式渠道要求说明依据并复核；如另有借号、代练或"
        "他人操作，再单独保存双方约定和操作证据，不把两类责任混在一起。"
    ),
}
_URL_PATTERN = re.compile(r"(?:https?://|www\.)", re.IGNORECASE)
_ARTICLE_PATTERN = re.compile(
    r"第[零〇一二三四五六七八九十百千万两0-9]+条"
)
_NUMBER_PATTERN = re.compile(r"(?<![A-Za-z])\d+(?:\.\d+)?")
_UNSAFE_CONCLUSION_PATTERNS = (
    re.compile(r"(?:一定|必然|肯定|保证).{0,10}(?:胜诉|获赔|立案|赔偿)"),
    re.compile(r"(?:就是|已经|明确)(?:构成)?(?:诈骗|违法|犯罪)"),
    re.compile(r"(?:三倍|十倍|[0-9]+倍)赔偿"),
)

# 正文是用户真的会发给房东、商家或单位的文本，写错的后果比一段摘要重。
# 因此除了通用的网址、条号、数字校验外，另外禁止两类内容：
# 代对方许诺义务（对方并未作出承诺，写进信里会误导用户），
# 以及替用户放弃权利或作出让步。
_LETTER_FORBIDDEN_PATTERNS = (
    re.compile(r"(?:您|你|对方|贵(?:公司|方|司))?\s*(?:必须|应当|须|应)"
               r".{0,12}(?:赔偿|退款|退还|支付|承担全部)"),
    re.compile(r"(?:限|于).{0,8}(?:日内|小时内|个工作日内)"
               r".{0,10}(?:赔偿|退款|退还|支付|处理完毕)"),
    re.compile(r"(?:否则|不然).{0,15}(?:起诉|报警|曝光|投诉到|追究)"),
    re.compile(r"我(?:放弃|不再主张|同意不)"),
    re.compile(r"(?:依据|根据|违反了?)\s*《"),
)

# 直接发给对方的正文里不该出现执法或第三方机构名：向机构反映属于
# escalation 字段的后续升级路径，混进正文会把一封沟通信变成投诉举报信，
# 既可能提前激化关系，也可能让用户误以为已经启动了官方程序。
# 依据包草稿本身提到某机构时（例如对方就是该机构）不在此限。
_LETTER_AUTHORITY_MARKERS = (
    "法院",
    "起诉",
    "报警",
    "公安",
    "派出所",
    "检察",
    "仲裁",
    "劳动监察",
    "市场监管",
    "消协",
    "消费者协会",
    "信访",
    "举报",
    "媒体",
    "曝光",
)

# 动作与证据条目改写校验的两个参数。
# 片段长度 4：中文里 4 字片段已足以承载一个具体动作要素（「书面通知」
# 「保存凭证」），再短就会被通用词组大量误配。
# 覆盖率 0.40：实测三类正常改写（换措辞、调顺序、补案情细节）落在
# 0.541~0.565，而凭空新增和无关内容全是 0.000，两簇之间没有重叠。
# 阈值取 0.40 留出余量：宁可放过一点改写也不误拒正常成文，因为条目
# 被拒会导致整轮成文作废、退回模板，那正是要修的模板腔本身。
_ITEM_SHINGLE = 4
_ITEM_MIN_COVERAGE = 0.40

_GENERAL_NOTICE = (
    "这是该类纠纷的一般法律依据，是否适用于本案仍需结合具体关系、"
    "约定、履行情况和证据核对。"
)
_CASE_NOTICE = (
    "这是当前正式方案选用的本案依据，仍应按方案列明的事实条件和限制理解。"
)
_GENERAL_APPLICABILITY_NOTICES = {
    "食品安全法.第一百四十八条": (
        "本条中的增加赔偿须以食品不符合食品安全标准为前提，并需核对"
        "生产者生产该食品，或者经营者明知仍经营等法定条件；发现异物"
        "不等于自动适用，仍应结合食品或剩余物、包装、订单、原始照片、"
        "检测或就医材料和完整沟通记录证明。"
    ),
    "民法典.第一千一百九十九条": (
        "本条仅适用于无民事行为能力人在幼儿园、学校或者其他教育机构"
        "学习、生活期间受到人身损害的情形；是否属于无民事行为能力人、"
        "机构能否证明已尽教育管理职责，均需结合年龄、行为能力和证据核对。"
    ),
    "民法典.第一千二百条": (
        "本条适用于限制民事行为能力人在学校或者其他教育机构学习、生活"
        "期间受到人身损害的情形，仍需证明学校或者教育机构未尽教育、"
        "管理职责。"
    ),
    "民法典.第一千二百零一条": (
        "本条针对校外第三人等教育机构以外的第三人造成损害的情形；"
        "第三人责任与教育机构未尽管理职责时的补充责任应分别核对。"
    ),
    "民法典.第一千二百零八条": (
        "本条只是机动车交通事故责任的衔接规则，不能单独确定事故责任比例"
        "或赔偿金额，仍需结合事故认定、保险和道路交通安全法律核对。"
    ),
    "民法典.第一千一百六十五条": (
        "一般过错侵权责任仍需核对侵害行为、过错、损害和因果关系；"
        "仅有损失结果不等于责任当然成立。"
    ),
    "民法典.第一千一百七十九条": (
        "本条列明人身损害赔偿的一般项目，具体项目和金额需以实际发生、"
        "必要性、关联性及票据、诊疗和收入材料为基础核对。"
    ),
    "民法典.第一千一百九十八条": (
        "经营场所、公共场所或活动组织者是否承担责任，取决于其是否未尽"
        "安全保障义务；第三人造成损害时还需区分直接责任与补充责任。"
    ),
    "民法典.第一千二百四十五条": (
        "本条适用于饲养动物造成损害，仍需核对动物饲养人或管理人、"
        "损害经过，以及被侵权人是否存在故意或重大过失。"
    ),
    "民法典.第一千二百一十八条": (
        "诊疗结果不理想不等于医疗机构当然承担责任，通常仍需核对实际损害、"
        "医疗过错和因果关系。"
    ),
    "民法典.第一千二百一十九条": (
        "说明义务与明确同意规则是否适用，应结合具体医疗措施、告知对象、"
        "告知内容以及未尽义务是否造成损害核对。"
    ),
    "民法典.第一千二百二十二条": (
        "只有条文列明的违反诊疗规范、隐匿或拒绝提供相关病历、遗失伪造"
        "篡改或违法销毁病历等情形，才可能适用过错推定，不能仅凭争议推定。"
    ),
    "民法典.第一千二百二十五条": (
        "患者查阅、复制病历资料的范围和办理方式仍需结合本人身份、材料类别"
        "及医疗机构依法设置的流程核对，并保留申请与答复记录。"
    ),
    "民法典.第一千零一十条": (
        "是否构成性骚扰需核对行为是否违背本人意愿、具体方式和证据；"
        "单位还负有采取合理预防、受理投诉和调查处置措施的义务。"
    ),
    "民法典.第六百八十条": (
        "借款利息应先核对是否有约定、约定是否明确及利率是否违反国家有关"
        "规定；自然人之间没有约定利息的，不能事后直接按有息借款计算。"
    ),
    "民法典.第一千零六十二条": (
        "夫妻共同财产的认定需结合取得时间、财产来源、赠与或遗嘱内容及"
        "夫妻财产约定，不能只凭登记在一方名下作结论。"
    ),
    "民法典.第一千零六十七条": (
        "抚养费或赡养费请求需结合身份关系、生活和履行情况核对；未成年子女、"
        "不能独立生活的成年子女以及缺乏劳动能力或生活困难的父母条件不同。"
    ),
    "民法典.第一千零八十六条": (
        "探望方式和时间可以先协商，协商不成可依法处理；是否中止探望须以"
        "不利于子女身心健康为条件，不能由一方任意长期阻止。"
    ),
}


class GroundingStatute(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    statute_id: str = Field(min_length=3, max_length=200)
    law_name: str = Field(min_length=1, max_length=200)
    article_no: str = Field(min_length=1, max_length=100)
    verified_text: str = Field(min_length=1, max_length=10_000)
    official_url: HttpUrl
    effective_date: date | None = None
    basis_scope: BasisScope
    applicability_notice: str = Field(min_length=1, max_length=500)

    @classmethod
    def from_statute(
        cls,
        statute: StatuteRecord,
        *,
        basis_scope: BasisScope,
        applicability_notice: str | None = None,
    ) -> "GroundingStatute":
        if not isinstance(statute, StatuteRecord):
            raise TypeError("依据包法条只能来自 StatuteRecord")
        default_notice = (
            _CASE_NOTICE
            if basis_scope == "case_specific"
            else _GENERAL_APPLICABILITY_NOTICES.get(
                statute.ref,
                _GENERAL_NOTICE,
            )
        )
        return cls(
            statute_id=statute.ref,
            law_name=statute.law_name,
            article_no=statute.article_no,
            verified_text=statute.content,
            official_url=statute.source_url,
            effective_date=statute.effective_date,
            basis_scope=basis_scope,
            applicability_notice=applicability_notice or default_notice,
        )


class GroundingPacket(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    current_message: str = Field(min_length=1, max_length=4000)
    turn_intent: TurnIntent
    case_summary: str = Field(min_length=1, max_length=2400)
    confirmed_facts: dict[str, Any] = Field(default_factory=dict)
    current_goal: str | None = Field(default=None, min_length=1, max_length=500)
    completed_actions: list[str] = Field(default_factory=list, max_length=8)
    coverage_mode: CoverageMode
    topic_id: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[a-z][a-z0-9_]{1,99}$",
    )
    topic_label: str = Field(min_length=1, max_length=100)
    formal_findings: list[str] = Field(default_factory=list, max_length=8)
    allowed_actions: list[str] = Field(min_length=1, max_length=8)
    evidence_targets: list[str] = Field(min_length=1, max_length=8)
    verified_statutes: list[GroundingStatute] = Field(
        default_factory=list,
        max_length=12,
    )
    limitations: list[str] = Field(default_factory=list, max_length=12)
    previously_answered: list[str] = Field(default_factory=list, max_length=12)
    one_allowed_next_question: str | None = Field(
        default=None,
        min_length=1,
        max_length=500,
    )
    direct_answer_draft: str | None = Field(
        default=None,
        min_length=1,
        max_length=1200,
    )
    letter_recipient: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
    )
    letter_objective: str | None = Field(
        default=None,
        min_length=1,
        max_length=300,
    )
    letter_draft: str | None = Field(
        default=None,
        min_length=1,
        max_length=1500,
    )

    @field_validator(
        "completed_actions",
        "formal_findings",
        "allowed_actions",
        "evidence_targets",
        "limitations",
        "previously_answered",
    )
    @classmethod
    def text_lists_are_clean(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value for value in normalized):
            raise ValueError("依据包列表项不能为空")
        if len(normalized) != len(set(normalized)):
            raise ValueError("依据包列表项不得重复")
        return normalized

    @model_validator(mode="after")
    def statutes_are_unique_and_scoped(self) -> "GroundingPacket":
        statute_ids = [item.statute_id for item in self.verified_statutes]
        if len(statute_ids) != len(set(statute_ids)):
            raise ValueError("依据包法条不得重复")
        if self.coverage_mode != "formal" and any(
            item.basis_scope == "case_specific"
            for item in self.verified_statutes
        ):
            raise ValueError("未核验回答不得携带本案适用法条")
        return self


class GroundedAnswerDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    direct_reply: str = Field(min_length=1, max_length=1200)
    actions: list[str] = Field(min_length=1, max_length=5)
    evidence: list[str] = Field(min_length=1, max_length=5)
    legal_explanation: list[str] = Field(default_factory=list, max_length=3)
    limitations: list[str] = Field(default_factory=list, max_length=8)
    next_question: str | None = Field(default=None, min_length=1, max_length=500)
    used_statute_ids: list[str] = Field(default_factory=list, max_length=8)
    letter_body: str | None = Field(
        default=None,
        min_length=1,
        max_length=1500,
    )


class GroundedAnswerComposition(GroundedAnswerDraft):
    provider: str = Field(min_length=1, max_length=50)
    model: str = Field(min_length=1, max_length=200)
    request_id: str | None = Field(default=None, max_length=200)
    usage: UsageInfo = Field(default_factory=UsageInfo)


def general_basis_refs(topic_id: str, message: str) -> tuple[str, ...]:
    refs = GENERAL_BASIS_REFS.get(topic_id, ())
    if topic_id == "education_minor_safety":
        selected: list[str] = []
        if any(marker in message for marker in _SCHOOL_NO_CAPACITY_MARKERS):
            selected.append("民法典.第一千一百九十九条")
        else:
            selected.extend(
                (
                    "民法典.第一千一百九十九条",
                    "民法典.第一千二百条",
                )
            )
        if any(marker in message for marker in _SCHOOL_THIRD_PARTY_MARKERS):
            selected.append("民法典.第一千二百零一条")
        return tuple(selected[:3])
    if topic_id == "medical_service_dispute":
        selected = []
        has_records = any(
            marker in message for marker in _MEDICAL_RECORD_MARKERS
        )
        if any(marker in message for marker in _MEDICAL_DAMAGE_MARKERS):
            selected.append("民法典.第一千二百一十八条")
        if any(marker in message for marker in _MEDICAL_CONSENT_MARKERS):
            selected.append("民法典.第一千二百一十九条")
        if has_records:
            selected.append("民法典.第一千二百二十五条")
            if any(
                marker in message
                for marker in _MEDICAL_RECORD_WRONGDOING_MARKERS
            ):
                selected.append("民法典.第一千二百二十二条")
        if any(marker in message for marker in _MEDICAL_PRIVACY_MARKERS):
            selected.append("民法典.第一千二百二十六条")
        if not selected:
            selected.append("民法典.第一千二百一十八条")
        return tuple(dict.fromkeys(selected))[:3]
    if topic_id == "traffic_accident":
        selected = ["民法典.第一千二百零八条"]
        # 车主与使用人不是同一人时，赔偿主体的认定依据。
        # 这条不能无条件给：自己开自己的车出事故时它不适用，
        # 摆出来只会让用户以为还有别人该分担责任。
        if any(marker in message for marker in _BORROWED_VEHICLE_MARKERS):
            selected.append("民法典.第一千二百零九条")
        if any(marker in message for marker in _INJURY_MARKERS):
            selected.append("民法典.第一千一百七十九条")
        return tuple(selected)
    if topic_id == "personal_injury":
        if any(marker in message for marker in _ANIMAL_DAMAGE_MARKERS):
            selected = ["民法典.第一千二百四十五条"]
        elif any(marker in message for marker in _PUBLIC_PLACE_MARKERS):
            selected = ["民法典.第一千一百九十八条"]
        else:
            selected = ["民法典.第一千一百六十五条"]
        selected.append("民法典.第一千一百七十九条")
        return tuple(selected)
    if topic_id == "logistics_travel_food":
        if any(marker in message for marker in _FOOD_SAFETY_MARKERS):
            return refs
        return tuple(
            ref
            for ref in refs
            if ref != "食品安全法.第一百四十八条"
        )
    if topic_id == "service_contract":
        # 对方明确表示不履行时，可在履行期限届满前就主张违约责任
        # （第五百七十八条）。健身房、培训机构明说「不干了」「不退」
        # 但还没到约定履行日期的情形需要本条；已经违约的情形用
        # 第五百七十七条即可，因此这条按案情追加而不是无条件给出。
        if any(marker in message for marker in _ANTICIPATORY_BREACH_MARKERS):
            return (*refs, "民法典.第五百七十八条")
        return refs
    if topic_id == "general_rental":
        selected = list(refs)
        # 危及安全或健康时先给解约权：这类案情里用户已经在受损害，
        # 「可以随时解除合同」比「可以要求维修」更贴近他的处境。
        if any(marker in message for marker in _HABITABILITY_MARKERS):
            selected.append("民法典.第七百三十一条")
        if any(marker in message for marker in _REPAIR_MARKERS):
            selected.append("民法典.第七百一十三条")
        # 租赁物毁损、灭失导致无法使用时可主张减租或免租。
        if any(marker in message for marker in _RENT_REDUCTION_MARKERS):
            selected.append("民法典.第七百二十九条")
        # 关系认定、租金期限、到期续住三类争议此前在库里有条文但无
        # 代码引用，general_rental 只有第五百零九条一条通则兜底。
        if any(marker in message for marker in _RENTAL_RELATION_MARKERS):
            selected.append("民法典.第七百零三条")
        if any(marker in message for marker in _RENT_PAYMENT_TERM_MARKERS):
            selected.append("民法典.第七百二十一条")
        if any(marker in message for marker in _RENTAL_HOLDOVER_MARKERS):
            selected.append("民法典.第七百三十四条")
        return tuple(dict.fromkeys(selected))
    if topic_id == "workplace_harassment":
        selected = []
        if any(marker in message for marker in _SEXUAL_HARASSMENT_MARKERS):
            selected.append("民法典.第一千零一十条")
        if any(marker in message for marker in _REPUTATION_MARKERS):
            selected.append("民法典.第一千零二十四条")
        selected.extend(
            (
                "劳动争议调解仲裁法.第二条",
                "劳动争议调解仲裁法.第六条",
            )
        )
        return tuple(dict.fromkeys(selected))[:3]
    if topic_id == "debt_collection":
        selected = []
        if any(
            marker in message
            for marker in _COLLECTION_HARASSMENT_MARKERS
        ):
            selected.append("民法典.第九百九十五条")
            if any(marker in message for marker in _PRIVACY_MARKERS):
                selected.append("民法典.第一千零三十二条")
        if any(marker in message for marker in _LOAN_MARKERS):
            selected.extend(
                (
                    "民法典.第六百六十七条",
                    "民法典.第六百七十五条",
                )
            )
        if any(marker in message for marker in _INTEREST_MARKERS):
            selected.append("民法典.第六百八十条")
        if not selected:
            selected.extend(
                (
                    "民法典.第六百六十七条",
                    "民法典.第六百七十五条",
                )
            )
        return tuple(dict.fromkeys(selected))[:3]
    if topic_id == "property_neighbor":
        selected = []
        if any(marker in message for marker in _PROPERTY_SERVICE_MARKERS):
            selected.append("民法典.第九百四十二条")
        if any(marker in message for marker in _NEIGHBOR_MARKERS):
            selected.append("民法典.第二百八十八条")
        if any(
            marker in message
            for marker in ("噪音", "污染", "侵占", "违章", "公共区域")
        ):
            selected.append("民法典.第二百八十六条")
        return tuple(dict.fromkeys(selected or refs))[:3]
    if topic_id == "privacy_reputation":
        selected = []
        if any(marker in message for marker in _REPUTATION_MARKERS):
            selected.append("民法典.第一千零二十四条")
        if any(marker in message for marker in _PRIVACY_MARKERS):
            selected.append("民法典.第一千零三十二条")
        if any(marker in message for marker in _PERSONAL_INFO_MARKERS):
            selected.extend(
                (
                    "民法典.第一千零三十四条",
                    "民法典.第一千零三十五条",
                )
            )
        if not selected:
            selected.append("民法典.第九百九十五条")
        return tuple(dict.fromkeys(selected))[:3]
    if topic_id == "family_support_property":
        selected = []
        if any(marker in message for marker in _FAMILY_PROPERTY_MARKERS):
            selected.append("民法典.第一千零六十二条")
        if any(marker in message for marker in _FAMILY_SUPPORT_MARKERS):
            selected.append("民法典.第一千零六十七条")
        if any(marker in message for marker in _FAMILY_VISIT_MARKERS):
            selected.append("民法典.第一千零八十六条")
        return tuple(dict.fromkeys(selected))
    return refs


def build_local_answer(packet: GroundingPacket) -> GroundedAnswerDraft:
    direct_reply = _local_direct_reply(packet)
    used_statute_ids = [
        statute.statute_id for statute in packet.verified_statutes
    ]
    legal_explanation: list[str] = []
    if any(
        item.basis_scope == "case_specific"
        for item in packet.verified_statutes
    ):
        legal_explanation.append(
            "下列条文是当前正式方案选用的本案依据，应与方案中的事实条件和限制一并理解。"
        )
    if any(
        item.basis_scope == "general" for item in packet.verified_statutes
    ):
        legal_explanation.append(
            "下列条文只作为该类纠纷的一般法律依据，是否适用于本案仍需结合具体约定、履行情况和证据核对。"
        )

    question = packet.one_allowed_next_question
    if question is not None:
        normalized = normalize_visible_text(question)
        if any(
            normalize_visible_text(previous) == normalized
            for previous in packet.previously_answered
        ):
            question = None

    return GroundedAnswerDraft(
        direct_reply=direct_reply,
        actions=packet.allowed_actions[:5],
        evidence=packet.evidence_targets[:5],
        legal_explanation=legal_explanation,
        limitations=packet.limitations[:8],
        next_question=question,
        used_statute_ids=used_statute_ids[:8],
        # 本地兜底草稿原样带上依据包里的正文，不做任何改写：
        # 这条路径在模型不可用或被跳过时生效，此时保持模板正文即为正确行为。
        letter_body=packet.letter_draft,
    )


def should_compose_grounded_answer(
    packet: GroundingPacket,
    draft: GroundedAnswerDraft,
    *,
    is_followup: bool,
) -> bool:
    if packet.coverage_mode == "emergency_guidance":
        # 紧急情况下本地文案是固定的安全优先步骤，不交给模型改写。
        return False
    # is_followup 已经覆盖了所有追问轮，因此这道门实际只对首轮生效。
    # 原先白名单里没有 new_fact，而首轮最常见的形态恰恰是用户第一次
    # 描述自己的遭遇（「我租的房子甲醛超标，住进去以后一直头痛咳嗽」
    # 被归为 new_fact）。这类消息若又落在未核验主题上拿不到法条，
    # 五个条件会同时落空，模型完全不被调用，用户看到的就是纯模板。
    # 首轮描述恰恰是最需要按案情成文的一轮，因此纳入白名单。
    return bool(
        is_followup
        or packet.verified_statutes
        or packet.formal_findings
        or packet.turn_intent
        in {"question", "stated_goal", "correction", "new_fact"}
        or len(draft.direct_reply) < 40
    )


def _shingles(text: str, size: int) -> set[str]:
    if len(text) < size:
        return set()
    return {text[index : index + size] for index in range(len(text) - size + 1)}


def _coverage_ratio(text: str, candidate: str) -> float:
    """text 的字符片段有多大比例能在 candidate 中找到。

    用覆盖率而不是「命中任一片段」：实测单片段命中太松，一条凭空
    新增的动作只要含有「保存」「记录」这类通用搭配就能蒙过检查。
    覆盖率要求改写后的整句大部分内容都源自同一条已批准条目。
    """
    pieces = _shingles(text, _ITEM_SHINGLE)
    if not pieces:
        return 0.0
    matched = sum(1 for piece in pieces if piece in candidate)
    return matched / len(pieces)


def _validate_rewritten_items(
    items: list[str],
    approved: list[str],
    *,
    label: str,
) -> None:
    """校验模型改写后的动作或证据条目。

    原先这里用 issubset，模型只能整条保留或整条删除，无法按案情改写。
    这是模板腔的主要来源：不论用户描述的是甲醛还是欠薪，动作和证据
    都是逐字相同的模板句。

    现在允许改写措辞，但每条都必须大部分内容源自同一条已批准条目
    （按字符片段覆盖率判断），因此可以调整称谓、补入案情细节、合并
    表述，却无法凭空造出依据包里没有的动作。另外单独禁止引入机构名：
    向法院、监管部门反映属于 escalation 的后续升级路径，混进当前步骤
    会让用户以为现在就该去告，而依据包并没有批准这一步。
    """
    if not items:
        raise ValueError(f"成文结果的{label}为空")
    approved_normalized = [
        normalize_visible_text(item) for item in approved
    ]
    approved_source = " ".join(approved_normalized)
    for item in items:
        normalized = normalize_visible_text(item)
        if not normalized:
            raise ValueError(f"成文结果的{label}为空")
        for marker in _LETTER_AUTHORITY_MARKERS:
            if marker in item and marker not in approved_source:
                raise ValueError(
                    f"成文结果的{label}引入了依据包之外的升级途径"
                )
        if normalized in approved_source:
            continue
        best = max(
            (
                _coverage_ratio(normalized, candidate)
                for candidate in approved_normalized
            ),
            default=0.0,
        )
        if best < _ITEM_MIN_COVERAGE:
            raise ValueError(
                f"成文结果的{label}偏离了依据包批准的内容"
            )


def merge_grounded_answer(
    packet: GroundingPacket,
    draft: GroundedAnswerDraft,
    composition: GroundedAnswerComposition,
) -> GroundedAnswerDraft:
    allowed_statutes = {
        statute.statute_id for statute in packet.verified_statutes
    }
    if not set(composition.used_statute_ids).issubset(allowed_statutes):
        raise ValueError("成文结果引用了依据包之外的法条")
    if composition.next_question != draft.next_question:
        raise ValueError("成文结果改变了后端允许的唯一问题")
    _validate_rewritten_items(
        composition.actions,
        packet.allowed_actions,
        label="动作",
    )
    _validate_rewritten_items(
        composition.evidence,
        packet.evidence_targets,
        label="证据",
    )
    if not set(composition.legal_explanation).issubset(
        draft.legal_explanation
    ):
        raise ValueError("成文结果增加了未批准法律说明")
    if not set(composition.limitations).issubset(packet.limitations):
        raise ValueError("成文结果删除边界后又增加了新限制")
    if not composition.actions or not composition.evidence:
        raise ValueError("成文结果缺少具体动作或证据")

    letter = _merged_letter(packet, composition)

    visible_text = "\n".join(
        [
            composition.direct_reply,
            *composition.actions,
            *composition.evidence,
            *composition.legal_explanation,
            *composition.limitations,
            composition.next_question or "",
            letter or "",
        ]
    )
    if _URL_PATTERN.search(visible_text) or _ARTICLE_PATTERN.search(
        visible_text
    ):
        raise ValueError("成文结果直接生成了网址或条号")
    if any(
        pattern.search(visible_text)
        for pattern in _UNSAFE_CONCLUSION_PATTERNS
    ):
        raise ValueError("成文结果包含未允许的确定性结论")

    allowed_source = json.dumps(
        packet.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
    )
    for number in _NUMBER_PATTERN.findall(visible_text):
        if number not in allowed_source:
            raise ValueError("成文结果增加了依据包之外的数字")

    if not _has_context_anchor(composition.direct_reply, packet):
        raise ValueError("成文结果没有回应当前问题或诉求")

    return GroundedAnswerDraft(
        direct_reply=composition.direct_reply.strip(),
        actions=composition.actions,
        evidence=composition.evidence,
        legal_explanation=composition.legal_explanation,
        limitations=composition.limitations,
        next_question=composition.next_question,
        used_statute_ids=composition.used_statute_ids,
        letter_body=letter,
    )


def _merged_letter(
    packet: GroundingPacket,
    composition: GroundedAnswerComposition,
) -> str | None:
    """校验模型改写后的沟通正文。

    正文原先完全不进依据包，模型看不到也改不了，于是每个未核验主题
    都拿到同一段模板文字。现在允许模型按案情重写，但它是用户真的会
    发出去的文本，因此校验比其他字段更严：不得代对方许诺义务、
    不得替用户放弃权利、不得把信改寄给依据包之外的第三方机构。

    返回 None 表示不替换模板正文，调用方保留模板值；抛出 ValueError
    表示模型越界，整轮成文作废。
    """
    letter = composition.letter_body
    if letter is None:
        return None
    normalized = letter.strip()
    if not normalized:
        return None
    if packet.letter_draft is None:
        raise ValueError("依据包未提供正文草稿时不得生成沟通正文")
    for pattern in _LETTER_FORBIDDEN_PATTERNS:
        if pattern.search(normalized):
            raise ValueError("沟通正文包含代对方承诺或放弃权利的表述")
    # 正文不得把信改寄给依据包之外的第三方机构。原先这里比对
    # letter_recipient 是否出现在正文中，但 recipient 是角色描述
    # （「与事件直接相关、能够登记和处理问题的负责人」），模板正文
    # 本身也不含这串字，那道检查连未改动的模板都会拒绝。真正要防的
    # 是模型把发给房东的信写成投诉举报信：机构名属于 escalation
    # 字段的升级路径，不该出现在直接发给对方的正文里。
    reference = " ".join(
        part
        for part in (packet.letter_draft, packet.letter_recipient)
        if part
    )
    for marker in _LETTER_AUTHORITY_MARKERS:
        if marker in normalized and marker not in reference:
            raise ValueError("沟通正文出现依据包之外的第三方机构")
    return normalized


def _local_direct_reply(packet: GroundingPacket) -> str:
    message = packet.current_message
    combined = " ".join(
        value
        for value in (
            packet.current_message,
            packet.case_summary,
            packet.current_goal,
        )
        if value
    )

    if any(
        marker in message
        for marker in ("风险解除", "现在安全了", "已经安全了")
    ):
        return (
            f"既然你已明确说明当前紧急风险解除，可以回到“{packet.topic_label}”"
            "这件事继续处理。接下来先按目前能确认的事实安排取证和沟通；"
            "如果这些步骤已经做过，说明处理结果即可直接接着往下处理。"
        )

    if packet.topic_id == "logistics_travel_food":
        food_safety_context = any(
            marker in combined for marker in _FOOD_SAFETY_MARKERS
        )
        if food_safety_context:
            evidence_changed = any(
                marker in combined
                for marker in _FOOD_EVIDENCE_CHANGE_MARKERS
            )
            if any(
                marker in message for marker in _AMOUNT_QUESTION_MARKERS
            ):
                prefix = (
                    "已经吃掉一部分不等于当然放弃权利，已有照片、订单和"
                    "沟通记录仍可保留；但实物状态变化可能影响后续核验。"
                    if evidence_changed
                    else (
                        "可以先提出退款、赔偿或其他处理诉求，但要以现有材料"
                        "能够证明的事实为基础。"
                    )
                )
                return (
                    f"{prefix}可以先提出退还餐费，并按证据主张实际损失；"
                    "如果能够证明食品不符合食品安全标准，且生产者生产了该食品，"
                    "或者经营者明知仍经营，还可以主张按价款十倍或者损失三倍"
                    "计算增加赔偿，增加赔偿不足一千元时按一千元主张。"
                    "看到异物不等于这些条件自动成立，仍要结合剩余食品、包装、"
                    "订单、原始照片、检测或就医材料及完整沟通记录核对。"
                )
            if any(
                marker in message
                for marker in _FOOD_EVIDENCE_CHANGE_MARKERS
            ):
                return (
                    "已经拍照但吃掉一部分，不等于照片和订单失去作用；"
                    "不过实物状态改变可能影响商家、平台或后续机构核验。"
                    "先停止继续食用，保留剩余食物、包装、标签、订单和原始照片，"
                    "并书面说明发现异物及实物变化的时间经过。"
                )
            if any(
                marker in message for marker in ("虫", "异物", "中毒")
            ):
                return (
                    "外卖或食品中发现虫子、异物时，先停止食用并固定发现时的"
                    "原始状态。保存订单、包装标签、食物和异物的整体及近照，"
                    "再通过订单售后要求商家和平台书面登记；如有身体不适，"
                    "及时就医并保存诊疗材料。"
                )

    if packet.topic_id == "return_refused" or any(
        marker in combined for marker in _WRONG_ITEM_MARKERS
    ):
        if any(marker in message for marker in _NO_RESPONSE_MARKERS):
            return (
                "如果商家在你通过平台聊天提出补发并给出合理答复期限后仍不回复，"
                "可以直接转订单售后或平台客服，提交订单、错发商品、面单和催告记录；"
                "平台仍不处理时，再保留工单结果并向消费投诉渠道反映。"
            )
        if any(marker in combined for marker in _REPLACEMENT_MARKERS):
            return (
                "可以继续把补发正确商品作为首要诉求，不必先改成退款。"
                "如果还没正式提出，就通过平台聊天写明订单号、错发商品、"
                "应补发商品和合理答复期限，并保留发送记录。"
            )
        if any(marker in combined for marker in _WRONG_ITEM_MARKERS):
            return (
                "你描述的是实际收到的商品与订单不一致。先固定订单和错发事实，"
                "再通过平台留痕明确要求补发正确商品或退款。"
            )

    if packet.topic_id == "deposit_deduction" and any(
        marker in combined for marker in _DEPOSIT_DAMAGE_MARKERS
    ):
        return (
            "房东提出墙面划痕，不等于可以直接扣除全部押金。先要求房东书面"
            "说明具体损坏位置、形成时间、修复必要性、实际费用和对应凭证，"
            "再用入住与退租时的照片、视频、验收记录和合同约定逐项核对；"
            "没有依据或明显超出合理修复费用的部分，可以明确表示不认可并"
            "要求返还。"
        )

    if packet.direct_answer_draft:
        candidate = packet.direct_answer_draft.strip()
        if _has_context_anchor(candidate, packet):
            return candidate
    if packet.turn_intent == "stated_goal":
        goal = packet.current_goal or packet.topic_label
        return (
            f"可以把“{goal}”作为当前首要诉求。若还没有向对方正式提出，"
            "下一步通过可留痕渠道明确发送；如果已经提出，就根据对方的回复"
            "继续处理。"
        )
    if packet.turn_intent == "completed_action":
        return (
            "你已经完成了本轮提到的动作，下一步重点看对方是否回复、拒绝或"
            "已经受理；下面只列后续处理，不再重复同一步。"
        )
    if packet.turn_intent == "continue_case":
        return (
            "可以继续。当前先完成下面这一步并保留记录；如果其实已经做过，"
            "说明对方怎样回复，我会直接接着往下整理。"
        )
    if packet.turn_intent == "correction":
        return (
            "收到，以你刚刚更正的信息为准。下面的建议只依据目前仍能确认的"
            "事实。"
        )
    topic_reply = _topic_local_direct_reply(packet)
    if topic_reply is not None:
        return topic_reply
    if packet.turn_intent == "question":
        return (
            f"关于“{packet.topic_label}”这件事，现有信息还不能直接确定最终"
            "结果，但可以先按下面的动作保全材料并推动对方书面回应。"
        )
    return (
        f"你这次补充的是“{message[:120]}”。我先结合“{packet.topic_label}”"
        "整理它对当前处理的影响；尚未确认的责任、金额和处理结果暂不作结论。"
    )


def _topic_local_direct_reply(packet: GroundingPacket) -> str | None:
    message = packet.current_message
    if packet.topic_id == "education_minor_safety" and any(
        marker in message
        for marker in ("不处理", "不管", "没处理", "没有处理")
    ):
        return (
            "学校持续不处理时，不要只停留在口头催促。先书面要求学校立即"
            "采取保护措施、登记核查并明确负责人和反馈时间，保存送达与回复"
            "记录；仍无处理时，带上事件、伤情和沟通材料向属地教育主管部门"
            "反映。"
        )
    if packet.topic_id == "medical_service_dispute" and any(
        marker in message for marker in _MEDICAL_RECORD_MARKERS
    ):
        return (
            "口头解释不能替代对病历申请的正式处理。向医院病案管理部门或"
            "医务部门书面申请查阅、复制与本人诊疗有关的可提供材料并要求"
            "回执；如果拒绝或不书面答复，保存申请记录后向当地卫生健康主管"
            "部门咨询或投诉。"
        )
    if packet.topic_id == "privacy_reputation":
        if any(
            marker in message
            for marker in ("删除", "删掉", "停止发布", "停止传播")
        ):
            return (
                "可以先要求删除并停止继续传播。联系对方前先完整保存发布页面、"
                "账号、链接、时间和上下文，再通过可留痕方式发送删除要求；"
                "同时向平台举报并保存投诉编号，避免内容删除后反而缺少证据。"
            )
        if any(
            marker in message
            for marker in ("继续发", "还在发", "继续发布", "继续传播")
        ):
            return (
                "对方仍在继续发布时，先连续保存新增内容、账号、链接和时间，"
                "立即向平台投诉并要求限制传播、保存后台记录；再书面要求对方"
                "停止并删除。若出现威胁、跟踪或现实人身风险，及时报警。"
            )
    reply = _TOPIC_FALLBACK_DIRECT_REPLIES.get(packet.topic_id)
    if reply is not None:
        return reply
    if not packet.allowed_actions or not packet.evidence_targets:
        return None
    action = packet.allowed_actions[0].rstrip("。；; ")
    evidence = packet.evidence_targets[0].rstrip("。；; ")
    return (
        f"针对你问的“{message[:100]}”，目前不能可靠确定最终责任或结果，"
        f"但现在可以先{action}；同时{evidence}。完成后根据对方的书面回复"
        "再决定是否升级处理。"
    )


def _has_context_anchor(text: str, packet: GroundingPacket) -> bool:
    normalized = normalize_visible_text(text)
    if not normalized:
        return False
    sources = (
        packet.current_message,
        packet.current_goal or "",
        packet.topic_label,
        packet.case_summary,
    )
    keywords: set[str] = set()
    for source in sources:
        compact = normalize_visible_text(source)
        for width in (2, 3, 4):
            keywords.update(
                compact[index : index + width]
                for index in range(max(0, len(compact) - width + 1))
            )
    return any(keyword and keyword in normalized for keyword in keywords)
