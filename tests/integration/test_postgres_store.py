from __future__ import annotations

import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4

import pytest

from app.agent.errors import (
    CaseNoProgressError,
    ConsultationConflictError,
)
from app.agent.progression import comparison_units, project_response
from app.attachments.errors import (
    AttachmentNotFoundError,
    AttachmentStateConflictError,
)
from app.attachments.models import ExtractionBlock, ExtractionResult
from app.db.contracts import (
    AttachmentBindingCommand,
    ConsultationCommitCommand,
    SessionUpdateCommand,
    TurnWriteCommand,
)


pytestmark = pytest.mark.integration
NOW = datetime(2026, 8, 10, 8, 0, tzinfo=UTC)


def test_postgres_store_enforces_owner_scoping_and_atomic_commit() -> None:
    database_url = os.getenv("TEST_POSTGRES_URL")
    if not database_url:
        pytest.skip("TEST_POSTGRES_URL is not configured")

    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, delete

    from app.db.postgres import PostgresApplicationStore
    from app.db.tables import users

    project_root = __import__("pathlib").Path(__file__).resolve().parents[2]
    config = Config(str(project_root / "alembic.ini"))
    config.set_main_option(
        "sqlalchemy.url",
        database_url.replace("%", "%%"),
    )
    command.upgrade(config, "head")

    engine = create_engine(database_url)
    store = PostgresApplicationStore(engine, now=lambda: NOW)
    owner_id = str(uuid4())
    other_owner_id = str(uuid4())
    try:
        with engine.begin() as connection:
            connection.execute(
                users.insert(),
                [
                    {"id": owner_id, "created_at": NOW},
                    {"id": other_owner_id, "created_at": NOW},
                ],
            )
        session = store.create_session(owner_id=owner_id)
        turn_id = str(uuid4())
        store.persist_session_turn(
            ConsultationCommitCommand(
                owner_id=owner_id,
                session_id=session.id,
                session=SessionUpdateCommand(
                    scenario_id="deposit_deduction",
                    facts={"deposit_amount": 2000},
                    followup_round=1,
                    status="need_more_facts",
                    jurisdiction="CN",
                ),
                turn=TurnWriteCommand(
                    turn_id=turn_id,
                    user_message="房东扣了押金",
                    facts={"deposit_amount": 2000},
                    rule_matches=(),
                    response={"status": "need_more_facts"},
                ),
                occurred_at=NOW,
            )
        )

        assert store.require_session(
            session.id,
            owner_id=owner_id,
        ).scenario_id == "deposit_deduction"
        assert store.get_session(
            session.id,
            owner_id=other_owner_id,
        ) is None
        assert [
            turn.id
            for turn in store.list_turns(
                session.id,
                owner_id=owner_id,
            )
        ] == [turn_id]
    finally:
        with engine.begin() as connection:
            connection.execute(
                delete(users).where(
                    users.c.id.in_([owner_id, other_owner_id])
                )
            )
        engine.dispose()


def test_postgres_attachment_reservation_is_atomic_and_owner_scoped() -> None:
    database_url = os.getenv("TEST_POSTGRES_URL")
    if not database_url:
        pytest.skip("TEST_POSTGRES_URL is not configured")

    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, delete

    from app.db.postgres import PostgresApplicationStore
    from app.db.tables import users

    project_root = __import__("pathlib").Path(__file__).resolve().parents[2]
    config = Config(str(project_root / "alembic.ini"))
    config.set_main_option(
        "sqlalchemy.url",
        database_url.replace("%", "%%"),
    )
    command.upgrade(config, "head")

    engine = create_engine(database_url)
    store = PostgresApplicationStore(engine, now=lambda: NOW)
    owner_id = str(uuid4())
    other_owner_id = str(uuid4())
    successful_reservation: str | None = None
    try:
        with engine.begin() as connection:
            connection.execute(
                users.insert(),
                [
                    {"id": owner_id, "created_at": NOW},
                    {"id": other_owner_id, "created_at": NOW},
                ],
            )
        processing = store.create_processing(
            owner_id=owner_id,
            original_name="reservation.pdf",
            media_type="application/pdf",
            size_bytes=128,
            sha256="a" * 64,
        )
        extracted = store.save_extraction(
            processing.id,
            ExtractionResult(
                media_type="application/pdf",
                page_count=1,
                extraction_method="direct_text",
                blocks=(
                    ExtractionBlock(
                        page_number=1,
                        block_index=0,
                        text="并发预留测试",
                        confidence=1,
                    ),
                ),
            ),
            owner_id=owner_id,
        )
        confirmed = store.confirm(
            extracted.id,
            "并发预留测试",
            owner_id=owner_id,
        )

        barrier = Barrier(2)
        reservation_ids = (str(uuid4()), str(uuid4()))

        def reserve(reservation_id: str) -> tuple[str, str]:
            barrier.wait(timeout=5)
            try:
                return (
                    "reserved",
                    store.reserve(
                        [confirmed.id],
                        owner_id=owner_id,
                        reservation_id=reservation_id,
                    ),
                )
            except AttachmentStateConflictError as exc:
                return ("conflict", exc.code)

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(reserve, reservation_ids))

        assert sorted(status for status, _ in outcomes) == [
            "conflict",
            "reserved",
        ]
        successful_reservation = next(
            value
            for status, value in outcomes
            if status == "reserved"
        )
        assert next(
            value
            for status, value in outcomes
            if status == "conflict"
        ) == "attachment_already_bound"

        with pytest.raises(AttachmentNotFoundError):
            store.delete(
                confirmed.id,
                owner_id=other_owner_id,
            )
        assert (
            store.get(confirmed.id, owner_id=owner_id).id
            == confirmed.id
        )
    finally:
        if successful_reservation is not None:
            store.release(
                successful_reservation,
                owner_id=owner_id,
            )
        with engine.begin() as connection:
            connection.execute(
                delete(users).where(
                    users.c.id.in_([owner_id, other_owner_id])
                )
            )
        engine.dispose()


def test_postgres_consultation_commit_rechecks_concurrent_progress() -> None:
    database_url = os.getenv("TEST_POSTGRES_URL")
    if not database_url:
        pytest.skip("TEST_POSTGRES_URL is not configured")

    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, delete

    from app.db.postgres import PostgresApplicationStore
    from app.db.tables import users

    project_root = __import__("pathlib").Path(__file__).resolve().parents[2]
    config = Config(str(project_root / "alembic.ini"))
    config.set_main_option(
        "sqlalchemy.url",
        database_url.replace("%", "%%"),
    )
    command.upgrade(config, "head")

    engine = create_engine(database_url)
    store = PostgresApplicationStore(engine, now=lambda: NOW)
    owner_id = str(uuid4())
    reservation_id: str | None = None
    response = {
        "turn_kind": "followup_answer",
        "reply": {
            "text": "先保存扣款依据。",
            "suggested_actions": ["书面要求房东说明扣款依据"],
        },
    }
    encoded = comparison_units(project_response(response))

    def commit(
        turn_id: str,
        *,
        candidate_response: dict = response,
        expected_latest_turn_id: str | None = None,
        binding: AttachmentBindingCommand | None = None,
        scenario_id: str = "deposit_deduction",
        facts: dict | None = None,
    ) -> None:
        safe_facts = facts or {"deposit_amount": 2000}
        store.persist_session_turn(
            ConsultationCommitCommand(
                owner_id=owner_id,
                session_id=session.id,
                session=SessionUpdateCommand(
                    scenario_id=scenario_id,
                    facts=safe_facts,
                    followup_round=1,
                    status="need_more_facts",
                    jurisdiction="CN",
                ),
                turn=TurnWriteCommand(
                    turn_id=turn_id,
                    user_message="房东扣了押金",
                    facts=safe_facts,
                    rule_matches=(),
                    response=candidate_response,
                ),
                attachment_binding=binding,
                occurred_at=NOW,
                expected_latest_turn_id=expected_latest_turn_id,
                comparison_units=comparison_units(
                    project_response(candidate_response)
                ),
            )
        )

    try:
        with engine.begin() as connection:
            connection.execute(
                users.insert(),
                {"id": owner_id, "created_at": NOW},
            )
        session = store.create_session(owner_id=owner_id)
        barrier = Barrier(2)

        def concurrent_commit(turn_id: str) -> str:
            barrier.wait(timeout=5)
            try:
                commit(turn_id)
                return "committed"
            except CaseNoProgressError:
                return "no_progress"

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(
                executor.map(
                    concurrent_commit,
                    (str(uuid4()), str(uuid4())),
                )
            )

        assert sorted(outcomes) == ["committed", "no_progress"]
        turns = store.list_turns(session.id, owner_id=owner_id)
        assert len(turns) == 1
        assert comparison_units(project_response(turns[0].response)) == encoded

        processing = store.create_processing(
            owner_id=owner_id,
            original_name="stale.pdf",
            media_type="application/pdf",
            size_bytes=128,
            sha256="b" * 64,
        )
        extracted = store.save_extraction(
            processing.id,
            ExtractionResult(
                media_type="application/pdf",
                page_count=1,
                extraction_method="direct_text",
                blocks=(
                    ExtractionBlock(
                        page_number=1,
                        block_index=0,
                        text="陈旧提交附件",
                        confidence=1,
                    ),
                ),
            ),
            owner_id=owner_id,
        )
        confirmed = store.confirm(
            extracted.id,
            "陈旧提交附件",
            owner_id=owner_id,
        )
        reservation_id = store.reserve(
            [confirmed.id],
            owner_id=owner_id,
        )
        different_response = {
            "turn_kind": "followup_answer",
            "reply": {
                "text": "下一步核对租赁合同中的押金条款。",
                "suggested_actions": ["核对租赁合同"],
            },
        }

        with pytest.raises(ConsultationConflictError):
            commit(
                str(uuid4()),
                candidate_response=different_response,
                expected_latest_turn_id=str(uuid4()),
                binding=AttachmentBindingCommand(
                    reservation_id=reservation_id,
                    attachment_ids=(confirmed.id,),
                ),
                scenario_id="return_refused",
                facts={"purchase_amount": 999},
            )

        restored_session = store.require_session(
            session.id,
            owner_id=owner_id,
        )
        restored_attachment = store.get(
            confirmed.id,
            owner_id=owner_id,
        )
        assert restored_session.scenario_id == "deposit_deduction"
        assert restored_session.facts == {"deposit_amount": 2000}
        assert len(store.list_turns(session.id, owner_id=owner_id)) == 1
        assert restored_attachment.status == "confirmed"
        assert restored_attachment.reservation_id == reservation_id
        assert restored_attachment.session_id is None
        assert restored_attachment.turn_id is None
    finally:
        if reservation_id is not None:
            store.release(reservation_id, owner_id=owner_id)
        with engine.begin() as connection:
            connection.execute(
                delete(users).where(users.c.id == owner_id)
            )
        engine.dispose()


def test_postgres_trial_activation_is_idempotent_and_bounded() -> None:
    database_url = os.getenv("TEST_POSTGRES_URL")
    if not database_url:
        pytest.skip("TEST_POSTGRES_URL is not configured")

    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, delete, select

    from app.db.tables import trial_identities, trial_ip_grants
    from app.trial.identity import PostgresTrialIdentityStore
    from app.trial.models import TrialIdentityLimitError

    project_root = __import__("pathlib").Path(__file__).resolve().parents[2]
    config = Config(str(project_root / "alembic.ini"))
    config.set_main_option(
        "sqlalchemy.url",
        database_url.replace("%", "%%"),
    )
    command.upgrade(config, "head")

    engine = create_engine(database_url)
    store = PostgresTrialIdentityStore(engine)
    identity_ids: list[str] = []
    unique_seed = uuid4().hex
    ip_digest = hashlib.sha256(
        f"trial-ip:{unique_seed}".encode()
    ).hexdigest()

    def create_identity(created_at: datetime):
        token_digest = hashlib.sha256(
            f"trial-token:{uuid4().hex}".encode()
        ).hexdigest()
        identity = store.create(
            token_digest=token_digest,
            ip_digest=ip_digest,
            policy_version="2026-08-10",
            now=created_at,
            expires_at=created_at + timedelta(days=365),
            max_ip_grants=3,
            ip_grant_expires_at=created_at + timedelta(minutes=15),
        )
        identity_ids.append(identity.id)
        return identity

    try:
        identities = [create_identity(NOW) for _ in range(3)]
        second_batch_at = NOW + timedelta(minutes=16)
        identities.extend(
            create_identity(second_batch_at) for _ in range(3)
        )
        activated_at = NOW + timedelta(minutes=32)
        barrier = Barrier(len(identities))

        def activate(identity_id: str) -> tuple[str, str]:
            barrier.wait(timeout=5)
            try:
                store.activate_ip_grant(
                    identity_id=identity_id,
                    ip_digest=ip_digest,
                    now=activated_at,
                    max_ip_grants=3,
                    pending_ip_grant_ttl=timedelta(minutes=15),
                    ip_grant_expires_at=(
                        activated_at + timedelta(days=30)
                    ),
                )
            except TrialIdentityLimitError:
                return ("limited", identity_id)
            return ("activated", identity_id)

        with ThreadPoolExecutor(max_workers=len(identities)) as executor:
            outcomes = list(
                executor.map(
                    activate,
                    [identity.id for identity in identities],
                )
            )

        assert [status for status, _ in outcomes].count("activated") == 3
        assert [status for status, _ in outcomes].count("limited") == 3
        activated_id = next(
            identity_id
            for status, identity_id in outcomes
            if status == "activated"
        )
        with engine.connect() as connection:
            original_expiry = connection.execute(
                select(trial_ip_grants.c.expires_at).where(
                    trial_ip_grants.c.identity_id == activated_id
                )
            ).scalar_one()
            grant_count = connection.execute(
                select(trial_ip_grants.c.id).where(
                    trial_ip_grants.c.ip_digest == ip_digest,
                    trial_ip_grants.c.expires_at > activated_at,
                )
            ).all()
        assert len(grant_count) == 3

        store.activate_ip_grant(
            identity_id=activated_id,
            ip_digest=ip_digest,
            now=activated_at + timedelta(days=1),
            max_ip_grants=3,
            pending_ip_grant_ttl=timedelta(minutes=15),
            ip_grant_expires_at=activated_at + timedelta(days=31),
        )
        with engine.connect() as connection:
            repeated_expiry = connection.execute(
                select(trial_ip_grants.c.expires_at).where(
                    trial_ip_grants.c.identity_id == activated_id
                )
            ).scalar_one()
        assert repeated_expiry == original_expiry
    finally:
        if identity_ids:
            with engine.begin() as connection:
                connection.execute(
                    delete(trial_identities).where(
                        trial_identities.c.id.in_(identity_ids)
                    )
                )
        engine.dispose()
