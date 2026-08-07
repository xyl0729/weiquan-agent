from __future__ import annotations

from copy import deepcopy
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest

from app.agent.errors import DataIntegrityError
from app.api.schemas import ConsultResponse
from app.db.session import SessionStore
from app.history.service import SessionHistoryService, history_title
from app.playbooks.registry import PlaybookRegistry
from tests.test_pipeline import make_pipeline, run


PROJECT_ROOT = Path(__file__).resolve().parent.parent
LEGACY_REPEAT_REPLY = "本轮未记录到新的方案变化，前一份方案仍然有效。"


def _public_response(
    session_id: str,
    turn_id: str,
) -> dict[str, object]:
    return {
        "session_id": session_id,
        "turn_id": turn_id,
        "audit_id": str(uuid4()),
        "followup_round": 1,
        "can_ask_more": True,
        "status": "need_more_facts",
        "verdict": None,
        "plan": None,
        "questions": ["请补充合同约定"],
        "limitations": [],
        "citations": [],
        "usage": {
            "provider": "fake",
            "model": "fake-extractor-v1",
            "request_id": None,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "estimated_cost_usd": None,
        },
    }


def _registry() -> PlaybookRegistry:
    return PlaybookRegistry.from_directory(
        PROJECT_ROOT / "app" / "playbooks"
    )


def _plan_response(
    session_id: str,
    turn_id: str,
    *,
    summary: str,
    citations: list[dict[str, object]] | None = None,
    rendered_text: str | None = "旧版完整渲染文本",
) -> dict[str, object]:
    return {
        "session_id": session_id,
        "turn_id": turn_id,
        "audit_id": str(uuid4()),
        "followup_round": 0,
        "can_ask_more": False,
        "status": "ready",
        "verdict": {
            "code": "quality_remedies_available",
            "label": "质量不符合要求时可核对退换修等救济",
            "status": "ready",
            "rule_ids": ["quality_problem"],
            "key_point": "应按质量问题救济路径处理。",
        },
        "plan": {
            "summary": summary,
            "evidence_now": ["商品页面和订单记录"],
            "actions": ["保存商家拒绝处理的完整记录"],
            "communication_text": "请商家书面说明拒绝处理的理由。",
            "limitations": ["仍需结合商品和证据判断。"],
            "time_limit": None,
            "jurisdiction": {
                "code": "CN",
                "name": "中国大陆",
                "status": "supported",
                "small_claim_threshold_yuan": None,
                "notices": [],
            },
            "rendered_text": rendered_text,
            "evidence_request_text": "请提供订单和沟通记录。",
        },
        "questions": [],
        "limitations": [],
        "citations": citations or [],
        "usage": {
            "provider": "fake",
            "model": "legacy-fake-v1",
            "request_id": None,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "estimated_cost_usd": None,
        },
    }


def _citation(
    ref: str,
    *,
    law_name: str,
    article_no: str,
) -> dict[str, object]:
    return {
        "ref": ref,
        "law_name": law_name,
        "article_no": article_no,
        "content": "用于测试的法条正文。",
        "effective_date": "2026-01-01",
        "source_url": "https://example.gov.cn/statute",
    }


def test_history_title_normalizes_whitespace_and_unicode() -> None:
    assert history_title("  房东\n\n扣押金  ") == "房东 扣押金"

    source = "甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉戌亥天地玄黄"

    assert history_title(source) == f"{source[:24]}…"


def test_history_service_lists_and_restores_public_turns(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path / "app.db")
    store.initialize()
    session = store.create_session()
    turn_id = str(uuid4())
    store.update_session(
        session.id,
        scenario_id="deposit_deduction",
        status="need_more_facts",
    )
    store.add_turn(
        session.id,
        turn_id=turn_id,
        user_message="房东扣了押金",
        facts={"private_fact": "not public"},
        rule_matches=[],
        response=_public_response(session.id, turn_id),
    )
    service = SessionHistoryService(store, _registry())

    listed = service.list_sessions()
    detail = service.get_session(session.id)

    assert listed[0].title == "房东扣了押金"
    assert listed[0].scenario_id == "deposit_deduction"
    assert detail.session == listed[0]
    assert detail.turns[0].response.status == "need_more_facts"
    assert detail.turns[0].response.turn_kind == "fact_collection"
    assert not hasattr(detail.session, "facts")


def test_history_restores_new_response_without_changes(
    tmp_path: Path,
) -> None:
    pipeline, store = make_pipeline(tmp_path)
    result = run(
        pipeline.consult(
            message="网购商品有质量问题，商家拒绝退货，价款800元",
            jurisdiction="CN",
        )
    )
    expected = ConsultResponse.model_validate(result.public_payload())

    detail = SessionHistoryService(
        store,
        pipeline.registry,
    ).get_session(result.session_id)

    assert detail.turns[0].response == expected


def test_history_projects_all_legacy_turn_kinds_in_order(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path / "app.db")
    store.initialize()
    session = store.create_session()
    store.update_session(
        session.id,
        scenario_id="return_refused",
        status="ready",
    )

    fact_turn = str(uuid4())
    initial_turn = str(uuid4())
    update_turn = str(uuid4())
    repeat_turn = str(uuid4())
    payloads = [
        (
            fact_turn,
            "我网购的商品有问题",
            _public_response(session.id, fact_turn),
        ),
        (
            initial_turn,
            "商品与描述不符，商家拒绝退款",
            _plan_response(
                session.id,
                initial_turn,
                summary="第一份方案",
            ),
        ),
        (
            update_turn,
            "补充：商品金额800元",
            _plan_response(
                session.id,
                update_turn,
                summary="金额补充后的方案",
            ),
        ),
        (
            repeat_turn,
            "他还是不配合怎么办",
            _plan_response(
                session.id,
                repeat_turn,
                summary="金额补充后的方案",
                rendered_text="这一轮重新生成的旧渲染文本",
            ),
        ),
    ]
    for turn_id, message, response in payloads:
        store.add_turn(
            session.id,
            turn_id=turn_id,
            user_message=message,
            facts={},
            rule_matches=[],
            response=response,
        )

    detail = SessionHistoryService(store, _registry()).get_session(
        session.id
    )

    assert [
        turn.response.turn_kind for turn in detail.turns
    ] == [
        "fact_collection",
        "initial_plan",
        "plan_update",
        "followup_answer",
    ]
    repeated = detail.turns[-1].response
    assert repeated.reply is not None
    assert repeated.reply.text == LEGACY_REPEAT_REPLY
    assert repeated.plan is None
    assert repeated.verdict is None
    assert repeated.citations == []


def test_history_filters_legacy_citations_without_rewriting_storage(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path / "app.db")
    store.initialize()
    session = store.create_session()
    store.update_session(
        session.id,
        scenario_id="return_refused",
        status="ready",
    )
    turn_id = str(uuid4())
    allowed = _citation(
        "消费者权益保护法.第二十四条",
        law_name="中华人民共和国消费者权益保护法",
        article_no="第二十四条",
    )
    unrelated = _citation(
        "住房租赁条例.第十条",
        law_name="住房租赁条例",
        article_no="第十条",
    )
    stored_response = _plan_response(
        session.id,
        turn_id,
        summary="旧版混入跨场景法条的方案",
        citations=[allowed, unrelated],
        rendered_text="包含两条法条的旧版渲染文本",
    )
    store.add_turn(
        session.id,
        turn_id=turn_id,
        user_message="商品有问题，商家拒绝退款",
        facts={},
        rule_matches=[],
        response=stored_response,
    )

    detail = SessionHistoryService(store, _registry()).get_session(
        session.id
    )

    response = detail.turns[0].response
    assert [item.ref for item in response.citations] == [
        "消费者权益保护法.第二十四条"
    ]
    assert response.plan is not None
    assert response.plan.rendered_text is None
    assert store.list_turns(session.id)[0].response == stored_response


def test_history_filters_explicit_reply_citations_consistently(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path / "app.db")
    store.initialize()
    session = store.create_session()
    store.update_session(
        session.id,
        scenario_id="return_refused",
        status="ready",
    )
    turn_id = str(uuid4())
    response = _public_response(session.id, turn_id)
    response.update(
        {
            "status": "ready",
            "can_ask_more": False,
            "turn_kind": "followup_answer",
            "reply": {
                "text": "先固定商家拒绝处理的证据。",
                "suggested_actions": [],
                "citation_refs": [
                    "消费者权益保护法.第二十四条",
                    "住房租赁条例.第十条",
                ],
                "new_case": None,
            },
            "questions": [],
            "citations": [
                _citation(
                    "消费者权益保护法.第二十四条",
                    law_name="中华人民共和国消费者权益保护法",
                    article_no="第二十四条",
                ),
                _citation(
                    "住房租赁条例.第十条",
                    law_name="住房租赁条例",
                    article_no="第十条",
                ),
            ],
        }
    )
    original = deepcopy(response)
    store.add_turn(
        session.id,
        turn_id=turn_id,
        user_message="他还是不处理怎么办",
        facts={},
        rule_matches=[],
        response=response,
    )

    restored = SessionHistoryService(
        store,
        _registry(),
    ).get_session(session.id).turns[0].response

    assert restored.reply is not None
    assert restored.reply.citation_refs == [
        "消费者权益保护法.第二十四条"
    ]
    assert [item.ref for item in restored.citations] == [
        "消费者权益保护法.第二十四条"
    ]
    assert store.list_turns(session.id)[0].response == original


def test_history_service_revalidates_stored_public_response(
    tmp_path: Path,
) -> None:
    path = tmp_path / "app.db"
    store = SessionStore(path)
    store.initialize()
    session = store.create_session()
    store.update_session(session.id, status="need_more_facts")
    turn = store.add_turn(
        session.id,
        user_message="房东扣了押金",
        facts={},
        rule_matches=[],
        response={"status": "need_more_facts"},
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE turns SET response_json = ? WHERE id = ?",
            ('{"status":"ready"}', turn.id),
        )

    with pytest.raises(
        DataIntegrityError,
        match="历史咨询数据未通过完整性检查",
    ):
        SessionHistoryService(store, _registry()).get_session(session.id)
