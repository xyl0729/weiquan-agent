from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Literal

from app.agent.models import TurnIntent
from app.db.models import TurnRecord


DuplicateReason = Literal[
    "exact",
    "near_text",
    "same_structure",
    "repeated_question",
]
ProgressionOutcome = Literal[
    "new_fact",
    "direct_question",
    "stated_goal",
    "completed_action",
    "continue_case",
    "more_precise_question",
    "new_case",
    "risk_changed",
    "no_progress",
]

UNVERIFIED_STAGE_COUNT = 7

_LIST_PREFIX = re.compile(
    r"(?m)^\s*(?:(?:第\s*)?[0-9一二三四五六七八九十]+\s*"
    r"[、.．:：)）]|[-*•·]+)\s*"
)
_VISIBLE_CHARS = re.compile(r"[^0-9a-z\u3400-\u9fff]+")
_QUESTION_WORDS = (
    "吗",
    "么",
    "呢",
    "怎么",
    "如何",
    "为什么",
    "为何",
    "是否",
    "能否",
    "可否",
    "能不能",
    "可不可以",
    "该不该",
    "要不要",
    "应不应该",
    "需不需要",
    "有没有",
    "是不是",
    "怎么办",
    "多少",
    "多久",
    "哪里",
    "哪个",
    "哪些",
    "什么",
    "何时",
    "何处",
    "谁",
)
_CONTINUATION_MESSAGES = frozenset(
    {
        "继续",
        "请继续",
        "接着",
        "接着说",
        "继续说",
        "然后",
        "然后呢",
        "下一步",
        "下一步怎么办",
        "接下来",
        "接下来呢",
        "往下",
    }
)
_NEW_CASE_PHRASES = (
    "另外一件事",
    "另一件事",
    "还有一件事",
    "再问一件事",
    "换个问题",
    "新的问题",
    "新情况",
)
_CORRECTION_PHRASES = (
    "不是",
    "并不是",
    "不对",
    "说错了",
    "我说错了",
    "更正",
    "纠正",
    "实际是",
    "应该是",
)
_GOAL_PHRASES = (
    "我的诉求",
    "我的要求",
    "我希望",
    "我想要",
    "我想让商家",
    "我要的是",
    "想让对方",
    "希望对方",
    "要求对方",
    "希望商家",
    "要求商家",
)
_COMPLETED_ACTION_MARKERS = (
    "已经",
    "已向",
    "已在",
    "已把",
    "已提交",
    "已发送",
    "已联系",
    "已投诉",
    "已举报",
    "已报警",
    "已拍照",
    "联系过",
    "发送过",
    "提交过",
    "投诉过",
    "举报过",
    "发过去",
    "刚联系",
    "刚提交",
    "刚发送",
    "拿到受理",
    "收到回复",
)
_COMPLETED_ACTION_RESULTS = (
    "明确拒绝",
    "拒绝处理",
    "被拒绝",
    "没回复",
    "没有回复",
    "未回复",
    "不回复",
    "一直不回应",
)
_EMERGENCY_RESOLVED_PHRASES = (
    "已经安全",
    "现在安全",
    "已到安全",
    "离开危险",
    "脱离危险",
    "已经就医",
    "已就医",
    "已经止付",
    "已经报警",
    "风险解除",
    "没有危险了",
    "不再危险",
)
_UNVERIFIED_JUMPS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (
        7,
        (
            "已经投诉",
            "已投诉",
            "投诉过",
            "向平台投诉",
            "已经举报",
            "已举报",
            "举报过",
        ),
    ),
    (
        6,
        (
            "明确拒绝",
            "拒绝处理",
            "被拒绝",
            "不处理",
            "驳回",
            "不同意",
        ),
    ),
    (5, ("没有回复", "没回复", "未回复", "不回复", "一直不回应")),
    (4, ("已经发送", "已发送", "已经联系", "已联系", "已经提交", "已提交")),
)
_STAGE_ANCHORS: dict[int, tuple[str, ...]] = {
    2: ("整理现有证据", "先把现有材料", "证据按时间"),
    3: ("首次书面联系", "现在书面联系", "通过可留痕渠道发送"),
    4: ("等待并记录对方回复", "记录对方回复", "保存受理编号"),
    5: ("书面催办", "明确回复日期", "设置回复日期"),
    6: ("升级反映", "升级渠道", "向主管机构"),
    7: ("寻求专业协助", "专业人员核对", "专业人员咨询"),
}
_COMMON_NON_PROGRESS_PHRASES = tuple(
    normalize
    for normalize in (
        "本结果只整理已提供事实",
        "不构成法律意见",
        "不替代专业法律意见",
        "不要为了取证继续置身危险",
        "本指导只用于整理信息",
        "该主题尚未经过本项目的本地法条与确定性规则核验",
        "已达到两轮追问上限",
    )
)
_COMPARISON_FIELDS: tuple[tuple[str, str], ...] = (
    ("replies", "reply"),
    ("actions", "action"),
    ("evidence", "evidence"),
    ("communications", "communication"),
    ("questions", "question"),
    ("stages", "stage"),
    ("notices", "notice"),
)
_COMPARISON_PREFIXES = {
    prefix: field for field, prefix in _COMPARISON_FIELDS
}
_COMPARISON_SEPARATOR = ":"


@dataclass(frozen=True, slots=True)
class VisibleTurnContent:
    turn_kind: str | None = None
    replies: tuple[str, ...] = ()
    actions: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    communications: tuple[str, ...] = ()
    questions: tuple[str, ...] = ()
    stages: tuple[str, ...] = ()
    notices: tuple[str, ...] = ()
    topic_id: str | None = None
    risk_flags: tuple[str, ...] = ()

    @property
    def core_units(self) -> tuple[str, ...]:
        return _unique(
            (
                *self.replies,
                *self.actions,
            *self.evidence,
            *self.communications,
            *self.questions,
            *self.stages,
        )
        )

    @property
    def all_units(self) -> tuple[str, ...]:
        return _unique((*self.core_units, *self.notices))

    @property
    def fingerprint(self) -> str:
        body = "\x1f".join(self.all_units)
        return hashlib.sha256(body.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class DuplicateCheck:
    duplicate: bool
    reason: DuplicateReason | None = None
    matched_turn: int | None = None
    novel_units: tuple[str, ...] = ()


def normalize_visible_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    text = unicodedata.normalize("NFKC", value).casefold()
    text = _LIST_PREFIX.sub("", text)
    return _VISIBLE_CHARS.sub("", text)


def is_continuation_message(message: str) -> bool:
    return normalize_visible_text(message) in {
        normalize_visible_text(value) for value in _CONTINUATION_MESSAGES
    }


def classify_turn_intent(message: str) -> TurnIntent:
    """Classify user-visible intent without inventing completed actions."""
    normalized = normalize_visible_text(message)
    if not normalized:
        return "new_fact"
    if any(
        normalize_visible_text(phrase) in normalized
        for phrase in _NEW_CASE_PHRASES
    ):
        return "new_case"
    if is_continuation_message(message):
        return "continue_case"
    has_correction = any(
        normalize_visible_text(phrase) in normalized
        for phrase in _CORRECTION_PHRASES
    )
    has_stated_goal = any(
        normalize_visible_text(phrase) in normalized
        for phrase in _GOAL_PHRASES
    )
    if has_stated_goal and not has_correction:
        return "stated_goal"
    if requires_direct_answer(message):
        return "question"
    if has_correction:
        return "correction"
    if any(
        normalize_visible_text(phrase) in normalized
        for phrase in (*_COMPLETED_ACTION_MARKERS, *_COMPLETED_ACTION_RESULTS)
    ):
        return "completed_action"
    return "new_fact"


def is_direct_question(message: str) -> bool:
    text = unicodedata.normalize("NFKC", message).strip()
    normalized = normalize_visible_text(text)
    return bool(
        normalized
        and (
            "?" in text
            or "？" in text
            or any(word in normalized for word in _QUESTION_WORDS)
        )
    )


def requires_direct_answer(message: str) -> bool:
    return is_direct_question(message) and not is_continuation_message(message)


def has_emergency_resolution(message: str) -> bool:
    normalized = normalize_visible_text(message)
    return any(
        normalize_visible_text(phrase) in normalized
        for phrase in _EMERGENCY_RESOLVED_PHRASES
    )


def project_response(response: Mapping[str, Any]) -> VisibleTurnContent:
    try:
        kind = _turn_kind(response)
        reply = _mapping(response.get("reply"))
        plan = _mapping(response.get("plan"))
        guidance = _mapping(response.get("guidance"))
        coverage = _mapping(response.get("coverage"))

        replies = [
            *_texts(reply.get("text")),
            *_texts(guidance.get("direct_answer")),
        ]
        actions = [
            *_texts(reply.get("suggested_actions")),
            *_texts(plan.get("actions")),
            *_texts(guidance.get("actions")),
        ]
        evidence = [
            *_texts(plan.get("evidence_now")),
            *_texts(guidance.get("evidence_now")),
        ]
        communications = [
            *_texts(plan.get("communication_text")),
            *_communication_texts(plan.get("communication_guide")),
            *_communication_texts(guidance.get("communication_guide")),
        ]
        questions = [
            *_texts(response.get("questions")),
            *_texts(guidance.get("next_question")),
        ]
        stages = []
        if (
            kind == "fact_collection"
            and not questions
            and _texts(response.get("limitations"))
            and response.get("can_ask_more") is False
        ):
            stages.append("fact_collection_exhausted")
        notices = [
            *_texts(response.get("limitations")),
            *_texts(plan.get("limitations")),
            *_texts(guidance.get("limitations")),
        ]
        topic_id = coverage.get("topic_id")
        risk_flags = tuple(
            value
            for value in coverage.get("risk_flags", ())
            if isinstance(value, str) and value
        ) if isinstance(coverage.get("risk_flags", ()), (list, tuple)) else ()
        return VisibleTurnContent(
            turn_kind=kind,
            replies=_normalized_units(replies),
            actions=_normalized_units(actions),
            evidence=_normalized_units(evidence),
            communications=_normalized_units(communications),
            questions=_normalized_units(questions),
            stages=_normalized_units(stages),
            notices=_normalized_units(notices),
            topic_id=(
                topic_id.strip()
                if isinstance(topic_id, str) and topic_id.strip()
                else None
            ),
            risk_flags=_unique(risk_flags),
        )
    except (AttributeError, TypeError, ValueError):
        return VisibleTurnContent(turn_kind=_turn_kind(response))


def project_turns(
    turns: Sequence[TurnRecord | Mapping[str, Any]],
) -> tuple[VisibleTurnContent, ...]:
    projected: list[VisibleTurnContent] = []
    for turn in turns:
        response: object
        if isinstance(turn, TurnRecord):
            response = turn.response
        elif isinstance(turn, Mapping) and isinstance(
            turn.get("response"), Mapping
        ):
            response = turn["response"]
        else:
            response = turn
        if isinstance(response, Mapping):
            projected.append(project_response(response))
        else:
            projected.append(VisibleTurnContent())
    return tuple(projected)


def find_duplicate(
    candidate: VisibleTurnContent,
    history: Sequence[VisibleTurnContent],
) -> DuplicateCheck:
    prior_by_field = {
        field: _unique(
            unit
            for item in history
            for unit in getattr(item, field)
        )
        for field, _prefix in _COMPARISON_FIELDS
        if field != "notices"
    }
    novel_by_field = {
        field: tuple(
            unit
            for unit in getattr(candidate, field)
            if not any(
                _units_equivalent(unit, old)
                for old in prior_by_field[field]
            )
        )
        for field in prior_by_field
    }
    novel = _unique(
        unit
        for field in prior_by_field
        for unit in novel_by_field[field]
    )

    for index, previous in enumerate(history):
        repeated_question = _repeated_question(
            candidate.questions,
            previous.questions,
        )
        if repeated_question and not novel:
            return DuplicateCheck(
                True,
                "repeated_question",
                index,
                novel,
            )
        if (
            candidate.all_units
            and _same_typed_units(candidate, previous)
        ):
            return DuplicateCheck(True, "exact", index, novel)
        if (
            candidate.core_units
            and previous.core_units
            and not novel
            and _near_joined(candidate.core_units, previous.core_units)
        ):
            return DuplicateCheck(True, "near_text", index, novel)
        if _same_structure(candidate, previous) and not novel:
            return DuplicateCheck(
                True,
                "same_structure",
                index,
                novel,
            )

    if (
        candidate.core_units
        and not novel
        and any(prior_by_field.values())
    ):
        return DuplicateCheck(
            True,
            "near_text",
            None,
            novel,
        )
    if not candidate.core_units:
        return DuplicateCheck(True, "same_structure", None, ())
    return DuplicateCheck(False, novel_units=novel)


def comparison_units(
    content: VisibleTurnContent,
) -> tuple[str, ...]:
    units: list[str] = []
    for field, prefix in _COMPARISON_FIELDS:
        for unit in getattr(content, field):
            units.append(
                f"{prefix}{_COMPARISON_SEPARATOR}{unit}"[:2000]
            )
            if len(units) == 48:
                return tuple(units)
    return tuple(units)


def comparison_is_equivalent(
    candidate_units: Sequence[str],
    response: Mapping[str, Any],
) -> bool:
    candidate = _content_from_comparison_units(candidate_units)
    if not candidate.all_units:
        return False
    previous = project_response(response)
    return find_duplicate(candidate, (previous,)).duplicate


def _content_from_comparison_units(
    units: Sequence[str],
) -> VisibleTurnContent:
    values: dict[str, list[str]] = {
        field: [] for field, _prefix in _COMPARISON_FIELDS
    }
    for raw_unit in units[:48]:
        if not isinstance(raw_unit, str):
            continue
        prefix, separator, value = raw_unit.partition(
            _COMPARISON_SEPARATOR
        )
        field = (
            _COMPARISON_PREFIXES.get(prefix)
            if separator
            else None
        )
        if field is None:
            # Compatibility with commands created before comparison units
            # carried field identity.
            field = "replies"
            value = raw_unit
        normalized = normalize_visible_text(value[:2000])
        if normalized:
            values[field].append(normalized)
    return VisibleTurnContent(
        replies=_unique(values["replies"]),
        actions=_unique(values["actions"]),
        evidence=_unique(values["evidence"]),
        communications=_unique(values["communications"]),
        questions=_unique(values["questions"]),
        stages=_unique(values["stages"]),
        notices=_unique(values["notices"]),
    )


def latest_coverage(
    turns: Sequence[TurnRecord],
    *,
    mode: str | None = None,
) -> dict[str, Any] | None:
    for turn in reversed(turns):
        coverage = turn.response.get("coverage")
        if not isinstance(coverage, Mapping):
            continue
        coverage_mode = coverage.get("mode")
        if mode is not None and coverage_mode != mode:
            continue
        topic_id = coverage.get("topic_id")
        if not isinstance(topic_id, str) or not topic_id.strip():
            continue
        return dict(coverage)
    return None


def derive_unverified_stage(
    turns: Sequence[TurnRecord],
    *,
    topic_id: str,
) -> int:
    start = -1
    stage = 0
    for index, turn in enumerate(turns):
        coverage = turn.response.get("coverage")
        if (
            isinstance(coverage, Mapping)
            and coverage.get("mode") == "unverified_guidance"
            and coverage.get("topic_id") == topic_id
        ):
            start = index
            stage = 1
    if start < 0:
        return 0

    for turn in turns[start + 1 :]:
        response = turn.response
        if response.get("turn_kind") != "followup_answer":
            continue
        visible = project_response(response)
        joined = "".join(visible.core_units)
        anchored = max(
            (
                candidate
                for candidate, anchors in _STAGE_ANCHORS.items()
                if any(
                    normalize_visible_text(anchor) in joined
                    for anchor in anchors
                )
            ),
            default=0,
        )
        if anchored:
            stage = max(stage, anchored)
    return stage


def requested_unverified_stage(
    message: str,
    current_stage: int,
    *,
    turn_intent: TurnIntent | None = None,
) -> int:
    intent = turn_intent or classify_turn_intent(message)
    if intent != "completed_action":
        return current_stage
    normalized = normalize_visible_text(message)
    for stage, phrases in _UNVERIFIED_JUMPS:
        if any(normalize_visible_text(value) in normalized for value in phrases):
            return max(current_stage + 1, stage)
    return min(UNVERIFIED_STAGE_COUNT, current_stage + 1)


def has_unverified_stage_signal(message: str) -> bool:
    return classify_turn_intent(message) == "completed_action"


def served_formal_action_indexes(
    turns: Sequence[TurnRecord],
    actions: Sequence[str],
) -> frozenset[int]:
    normalized_actions = [normalize_visible_text(action) for action in actions]
    served: set[int] = set()
    for turn in turns:
        if turn.response.get("turn_kind") not in {
            "followup_answer",
            "plan_update",
        }:
            continue
        reply = turn.response.get("reply")
        if not isinstance(reply, Mapping):
            continue
        suggested = _normalized_units(reply.get("suggested_actions"))
        for index, action in enumerate(normalized_actions):
            if action and any(_units_equivalent(action, item) for item in suggested):
                served.add(index)
    return frozenset(served)


def next_unfinished_action(
    actions: Sequence[str],
    turns: Sequence[TurnRecord],
) -> tuple[int, str] | None:
    served = served_formal_action_indexes(turns, actions)
    for index, action in enumerate(actions):
        if index not in served and isinstance(action, str) and action.strip():
            return index, action.strip()
    return None


def user_message_repeated(message: str, turns: Sequence[TurnRecord]) -> bool:
    normalized = normalize_visible_text(message)
    return bool(
        normalized
        and any(
            normalize_visible_text(turn.user_message) == normalized
            for turn in turns
        )
    )


def questions_asked(turns: Sequence[TurnRecord]) -> tuple[str, ...]:
    return _unique(
        question
        for turn in turns
        for question in project_response(turn.response).questions
    )


def limitation_already_served(
    turns: Sequence[TurnRecord],
    limitation: str,
) -> bool:
    normalized = normalize_visible_text(limitation)
    return any(
        normalized in project_response(turn.response).notices
        for turn in turns
    )


def more_precise_question(question: str, *, slot_type: str) -> str:
    base = question.rstrip("？?。 ")
    suffix = {
        "date": "请直接提供具体日期；若无法确认，请回答“无法确认”。",
        "number": "请直接提供具体金额或数字；若无法确认，请回答“无法确认”。",
        "integer": "请直接提供具体整数；若无法确认，请回答“无法确认”。",
        "boolean": "请直接回答“是”“否”或“无法确认”。",
        "enum": "请从问题列出的情况中选择最符合的一项。",
        "string": "请说明对应文件、记录或具体事实；若没有，请回答“没有”。",
    }.get(slot_type, "请给出一个具体事实；若无法确认，请回答“无法确认”。")
    return f"{base}？{suffix}"


def is_more_specific_question(candidate: str, previous: str) -> bool:
    new = normalize_visible_text(candidate)
    old = normalize_visible_text(previous)
    if not new or not old or new == old:
        return False
    specificity_markers = (
        "具体日期",
        "具体金额",
        "具体数字",
        "是否",
        "是或否",
        "无法确认",
        "文件",
        "记录",
        "哪一项",
    )
    return (
        old in new
        and len(new) >= len(old) + 6
        and any(normalize_visible_text(item) in new for item in specificity_markers)
    )


def emergency_status_questions(risk_flag: str) -> tuple[str, ...]:
    common = (
        "你现在是否已经到达安全地点，并有可信任的人陪同？",
        "是否已经联系能够提供现场保护或紧急帮助的机构？",
    )
    specialized: dict[str, tuple[str, ...]] = {
        "urgent_medical": (
            "是否已经获得医疗救助，并保留就诊或受理记录？",
        ),
        "fraud_loss": (
            "是否已经停止付款并通过官方渠道申请止付或冻结？",
            "是否已经保存交易流水、对方账号和受理编号？",
        ),
        "evidence_loss": (
            "是否已经在不增加风险的情况下保存原始证据？",
        ),
        "minor_harm": (
            "未成年人是否已经离开可能继续受伤害的环境？",
            "是否已经获得必要的医疗检查或现实保护？",
        ),
    }
    return _unique((*specialized.get(risk_flag, ()), *common))


def next_emergency_question(
    risk_flag: str,
    turns: Sequence[TurnRecord],
) -> str | None:
    asked = questions_asked(turns)
    reply_units = _unique(
        unit
        for turn in turns
        for unit in project_response(turn.response).replies
    )
    for question in emergency_status_questions(risk_flag):
        normalized = normalize_visible_text(question)
        if normalized not in asked and not any(
            normalized in unit for unit in reply_units
        ):
            return question
    return None


def _turn_kind(response: Mapping[str, Any]) -> str | None:
    value = response.get("turn_kind")
    if isinstance(value, str) and value:
        return value
    if isinstance(response.get("plan"), Mapping):
        return "initial_plan"
    if _texts(response.get("questions")):
        return "fact_collection"
    if isinstance(response.get("guidance"), Mapping):
        return "unverified_guidance"
    if isinstance(response.get("reply"), Mapping):
        return "followup_answer"
    return None


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _texts(value: object) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [
            item
            for item in value[:24]
            if isinstance(item, str) and item.strip()
        ]
    return []


def _communication_texts(value: object) -> list[str]:
    guide = _mapping(value)
    return [
        *_texts(guide.get("objective")),
        *_texts(guide.get("message")),
        *_texts(guide.get("after_sending")),
        *_texts(guide.get("escalation")),
    ]


def _normalized_units(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        source: Iterable[object] = (value,)
    elif isinstance(value, Iterable):
        source = value
    else:
        source = ()
    return _unique(
        normalized
        for item in source
        if (normalized := normalize_visible_text(item))
    )


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


def _comparison_text(value: str) -> str:
    result = value
    for phrase in _COMMON_NON_PROGRESS_PHRASES:
        normalized = normalize_visible_text(phrase)
        if normalized:
            result = result.replace(normalized, "")
    return result


def _units_equivalent(left: str, right: str) -> bool:
    if left == right:
        return True
    left_core = _comparison_text(left)
    right_core = _comparison_text(right)
    if not left_core or not right_core:
        return False
    if max(len(left_core), len(right_core)) < 24:
        return False
    ratio = SequenceMatcher(None, left_core, right_core).ratio()
    if ratio >= 0.90:
        return True
    return _trigram_jaccard(left_core, right_core) >= 0.82


def _near_joined(
    candidate: Sequence[str],
    previous: Sequence[str],
) -> bool:
    left = _comparison_text("".join(candidate))
    right = _comparison_text("".join(previous))
    if min(len(left), len(right)) < 24:
        return False
    return (
        SequenceMatcher(None, left, right).ratio() >= 0.90
        or _trigram_jaccard(left, right) >= 0.82
    )


def _trigram_jaccard(left: str, right: str) -> float:
    left_parts = _trigrams(left)
    right_parts = _trigrams(right)
    union = left_parts | right_parts
    if not union:
        return 0.0
    return len(left_parts & right_parts) / len(union)


def _trigrams(value: str) -> set[str]:
    if len(value) < 3:
        return {value} if value else set()
    return {value[index : index + 3] for index in range(len(value) - 2)}


def _repeated_question(
    candidate: Sequence[str],
    previous: Sequence[str],
) -> bool:
    for new in candidate:
        for old in previous:
            if new == old:
                return True
            if (
                _units_equivalent(new, old)
                and not is_more_specific_question(new, old)
            ):
                return True
    return False


def _same_structure(
    candidate: VisibleTurnContent,
    previous: VisibleTurnContent,
) -> bool:
    fields = (
        "actions",
        "evidence",
        "communications",
        "questions",
        "stages",
    )
    compared = False
    for field in fields:
        left = getattr(candidate, field)
        right = getattr(previous, field)
        if left or right:
            compared = True
            if set(left) != set(right):
                return False
    if not compared:
        return False
    if not candidate.replies and not previous.replies:
        return True
    return all(
        any(_units_equivalent(unit, old) for old in previous.replies)
        for unit in candidate.replies
    )


def _same_typed_units(
    candidate: VisibleTurnContent,
    previous: VisibleTurnContent,
) -> bool:
    return all(
        getattr(candidate, field) == getattr(previous, field)
        for field, _prefix in _COMPARISON_FIELDS
    )
