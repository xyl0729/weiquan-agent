from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4

import pytest

from app.agent.errors import (
    ConsultationConflictError,
    SessionNotFoundError,
)
from app.agent.models import UsageInfo
from app.agent.progression import comparison_units, project_response
from app.db.contracts import (
    ConsultationCommitCommand,
    SessionUpdateCommand,
    TurnWriteCommand,
)
from app.trial.conversations import InMemoryTrialConversationStore


NOW = datetime(2026, 8, 15, 8, 0, tzinfo=UTC)
OWNER_A = "11111111-1111-4111-8111-111111111111"
OWNER_B = "22222222-2222-4222-8222-222222222222"


class MutableClock:
    def __init__(self, value: datetime = NOW) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


def _command(
    *,
    session_id: str,
    owner_id: str = OWNER_A,
    expected_latest_turn_id: str | None = None,
    reply: str = "先保存证据，再书面提出诉求。",
    guarded: bool = True,
    occurred_at: datetime | None = None,
) -> ConsultationCommitCommand:
    response = {
        "turn_kind": "followup_answer",
        "reply": {
            "text": reply,
            "suggested_actions": ["保存聊天和付款记录"],
        },
    }
    return ConsultationCommitCommand(
        owner_id=owner_id,
        session_id=session_id,
        session=SessionUpdateCommand(
            scenario_id=None,
            facts={"amount": 4000},
            followup_round=0,
            status="need_more_facts",
            jurisdiction="CN",
        ),
        turn=TurnWriteCommand(
            turn_id=str(uuid4()),
            user_message=f"用户追问：{reply}",
            facts={"amount": 4000},
            rule_matches=(),
            response=response,
            provider_name="deepseek",
            provider_model="deepseek-chat",
            provider_request_id=str(uuid4()),
            usage=UsageInfo(
                input_tokens=20,
                output_tokens=10,
                total_tokens=30,
            ),
        ),
        occurred_at=occurred_at,
        expected_latest_turn_id=expected_latest_turn_id,
        comparison_units=(
            comparison_units(project_response(response))
            if guarded
            else ()
        ),
    )


def test_trial_conversation_creates_restores_and_isolates_owner() -> None:
    store = InMemoryTrialConversationStore(now=MutableClock())
    session = store.create_session(owner_id=OWNER_A)
    turn = store.persist_session_turn(
        _command(session_id=session.id, occurred_at=NOW)
    )

    restored = store.require_session(
        session.id,
        owner_id=OWNER_A,
        now=NOW,
    )

    assert restored.facts == {"amount": 4000}
    assert store.list_turns(
        session.id,
        owner_id=OWNER_A,
    ) == [turn]
    with pytest.raises(SessionNotFoundError):
        store.require_session(
            session.id,
            owner_id=OWNER_B,
            now=NOW,
        )
    with pytest.raises(SessionNotFoundError):
        store.list_turns(session.id, owner_id=OWNER_B)


def test_trial_conversation_write_renews_idle_ttl() -> None:
    clock = MutableClock()
    store = InMemoryTrialConversationStore(
        ttl_seconds=60,
        now=clock,
    )
    session = store.create_session(owner_id=OWNER_A)
    clock.advance(50)
    store.persist_session_turn(
        _command(
            session_id=session.id,
            occurred_at=clock(),
        )
    )
    clock.advance(50)

    assert store.require_session(
        session.id,
        owner_id=OWNER_A,
    ).id == session.id

    clock.advance(11)
    with pytest.raises(SessionNotFoundError):
        store.require_session(session.id, owner_id=OWNER_A)


def test_trial_conversation_capacity_evicts_oldest_write() -> None:
    store = InMemoryTrialConversationStore(
        capacity=2,
        now=MutableClock(),
    )
    first = store.create_session(owner_id=OWNER_A, now=NOW)
    second = store.create_session(
        owner_id=OWNER_A,
        now=NOW + timedelta(seconds=1),
    )
    third = store.create_session(
        owner_id=OWNER_A,
        now=NOW + timedelta(seconds=2),
    )

    with pytest.raises(SessionNotFoundError):
        store.require_session(first.id, owner_id=OWNER_A, now=NOW)
    assert store.require_session(
        second.id,
        owner_id=OWNER_A,
        now=NOW,
    ).id == second.id
    assert store.require_session(
        third.id,
        owner_id=OWNER_A,
        now=NOW,
    ).id == third.id


def test_trial_conversation_retains_only_recent_turns() -> None:
    store = InMemoryTrialConversationStore(
        max_turns=3,
        now=MutableClock(),
    )
    session = store.create_session(owner_id=OWNER_A, now=NOW)
    latest_id = None
    all_ids: list[str] = []

    for index in range(5):
        turn = store.persist_session_turn(
            _command(
                session_id=session.id,
                expected_latest_turn_id=latest_id,
                reply=f"第 {index + 1} 轮回复",
                occurred_at=NOW + timedelta(seconds=index),
            )
        )
        latest_id = turn.id
        all_ids.append(turn.id)

    retained = store.list_turns(session.id, owner_id=OWNER_A)
    assert [turn.id for turn in retained] == all_ids[-3:]


def test_trial_conversation_rejects_stale_concurrent_commit() -> None:
    store = InMemoryTrialConversationStore(now=MutableClock())
    session = store.create_session(owner_id=OWNER_A, now=NOW)
    barrier = Barrier(2)

    def persist(index: int) -> str:
        command = _command(
            session_id=session.id,
            reply=f"并发回复 {index}",
            occurred_at=NOW,
        )
        barrier.wait(timeout=5)
        try:
            store.persist_session_turn(command)
            return "committed"
        except ConsultationConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(persist, (1, 2)))

    assert sorted(outcomes) == ["committed", "conflict"]
    assert len(store.list_turns(session.id, owner_id=OWNER_A)) == 1
