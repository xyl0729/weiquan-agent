from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import ValidationError

from app.agent.errors import DataIntegrityError
from app.agent.models import (
    CaseAction,
    CaseCitation,
    CaseContinuationContext,
    CaseScenario,
    LockedCaseContext,
    RecentCaseTurn,
    TurnKind,
)
from app.db.models import TurnRecord
from app.playbooks.registry import PlaybookRegistry
from app.playbooks.schema import Playbook
from app.retrieval.database import StatuteRecord


_TURN_KINDS: set[str] = {
    "fact_collection",
    "initial_plan",
    "plan_update",
    "followup_answer",
    "new_case",
    "unverified_guidance",
    "emergency_guidance",
}


def has_historical_plan(turns: Sequence[TurnRecord]) -> bool:
    return any(_plan_parts(turn.response) is not None for turn in turns)


def build_case_continuation_context(
    *,
    playbook: Playbook,
    registry: PlaybookRegistry,
    existing_facts: dict[str, Any],
    statutes: Sequence[StatuteRecord],
    turns: Sequence[TurnRecord],
) -> CaseContinuationContext:
    plan_parts = next(
        (
            parts
            for turn in reversed(turns)
            if (parts := _plan_parts(turn.response)) is not None
        ),
        None,
    )
    if plan_parts is None:
        raise DataIntegrityError(
            "historical_plan_missing",
            "已有方案无法恢复",
        )
    plan, verdict, response = plan_parts

    statute_by_ref = {statute.ref: statute for statute in statutes}
    purpose_by_ref = {
        basis.ref: basis.purpose for basis in playbook.legal_basis
    }
    citation_refs = _historical_citation_refs(response, playbook)
    try:
        locked_case = LockedCaseContext(
            verdict_label=_required_text(verdict, "label"),
            key_point=_required_text(verdict, "key_point"),
            summary=_required_text(plan, "summary"),
            actions=[
                CaseAction(ref=f"A{index}", text=text)
                for index, text in enumerate(
                    _required_text_list(plan, "actions"),
                    start=1,
                )
            ],
            evidence=_required_text_list(plan, "evidence_now"),
            limitations=_required_text_list(plan, "limitations"),
            citations=[
                CaseCitation(
                    ref=ref,
                    law_name=statute_by_ref[ref].law_name,
                    article_no=statute_by_ref[ref].article_no,
                    content=statute_by_ref[ref].content,
                    purpose=purpose_by_ref[ref],
                )
                for ref in citation_refs
            ],
        )
        return CaseContinuationContext(
            current_scenario=_scenario(playbook),
            registered_scenarios=[
                _scenario(item) for item in registry.playbooks
            ],
            existing_facts=existing_facts,
            locked_case=locked_case,
            recent_turns=_recent_turns(turns),
        )
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        raise DataIntegrityError(
            "historical_plan_invalid",
            "已有方案无法恢复",
        ) from exc


def _scenario(playbook: Playbook) -> CaseScenario:
    return CaseScenario(
        id=playbook.id,
        name=playbook.name,
        aliases=list(playbook.aliases),
        slot_definitions={
            slot.name: slot.model_dump(exclude_none=True)
            for slot in playbook.slots.all
        },
    )


def _plan_parts(
    response: Mapping[str, Any],
) -> tuple[
    Mapping[str, Any],
    Mapping[str, Any],
    Mapping[str, Any],
] | None:
    plan = response.get("plan")
    verdict = response.get("verdict")
    if isinstance(plan, Mapping) and isinstance(verdict, Mapping):
        return plan, verdict, response
    return None


def _historical_citation_refs(
    response: Mapping[str, Any],
    playbook: Playbook,
) -> list[str]:
    all_refs = [basis.ref for basis in playbook.legal_basis]
    value = response.get("citations")
    if not isinstance(value, list) or not value:
        return all_refs
    refs = [
        item.get("ref")
        for item in value
        if isinstance(item, Mapping)
        and isinstance(item.get("ref"), str)
    ]
    if len(refs) != len(value) or len(refs) != len(set(refs)):
        raise ValueError("历史方案法条引用无效")
    if not set(refs).issubset(all_refs):
        raise ValueError("历史方案包含场景外法条引用")
    return refs


def _recent_turns(turns: Sequence[TurnRecord]) -> list[RecentCaseTurn]:
    selected: list[RecentCaseTurn] = []
    remaining = 4000
    for turn in reversed(turns):
        if len(selected) >= 4 or remaining <= 0:
            break
        user_message = turn.user_message[: min(500, remaining)].strip()
        if not user_message:
            continue
        remaining -= len(user_message)

        reply = _assistant_reply(turn.response)
        if reply is not None and remaining > 0:
            reply = reply[: min(1200, remaining)].strip() or None
            remaining -= len(reply or "")
        else:
            reply = None
        selected.append(
            RecentCaseTurn(
                user_message=user_message,
                turn_kind=_turn_kind(turn.response),
                assistant_reply=reply,
            )
        )
    selected.reverse()
    return selected


def _assistant_reply(response: Mapping[str, Any]) -> str | None:
    reply = response.get("reply")
    if not isinstance(reply, Mapping):
        return None
    text = reply.get("text")
    return text.strip() if isinstance(text, str) and text.strip() else None


def _turn_kind(response: Mapping[str, Any]) -> TurnKind:
    value = response.get("turn_kind")
    if isinstance(value, str) and value in _TURN_KINDS:
        return value  # type: ignore[return-value]
    if _plan_parts(response) is not None:
        return "initial_plan"
    questions = response.get("questions")
    if isinstance(questions, list) and questions:
        return "fact_collection"
    return "followup_answer"


def _required_text(source: Mapping[str, Any], key: str) -> str:
    value = source.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} 缺失")
    return value.strip()


def _required_text_list(
    source: Mapping[str, Any],
    key: str,
) -> list[str]:
    value = source.get(key)
    if not isinstance(value, list):
        raise ValueError(f"{key} 缺失")
    normalized = [
        item.strip()
        for item in value
        if isinstance(item, str) and item.strip()
    ]
    if len(normalized) != len(value) or not normalized:
        raise ValueError(f"{key} 无效")
    return normalized
