from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from threading import RLock
from typing import Protocol
from uuid import UUID, uuid4

from sqlalchemy import and_, delete, func, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.engine import Engine, RowMapping

from app.auth.models import (
    AuthContext,
    AuthSessionRecord,
    PrivacyContext,
    RegistrationDecision,
    TokenPurpose,
    UserRecord,
    UserRole,
)
from app.db.tables import (
    auth_sessions,
    auth_tokens,
    privacy_acceptances,
    registration_capacity,
    security_rate_limits,
    users,
)


class AuthStore(Protocol):
    def register_pending(
        self,
        *,
        email: str,
        password_hash: str,
        policy_version: str,
        now: datetime,
        pending_ttl: timedelta,
        capacity_limit: int | None = None,
    ) -> RegistrationDecision: ...

    def get_user_by_email(self, email: str) -> UserRecord | None: ...

    def get_user_by_id(self, user_id: str) -> UserRecord | None: ...

    def list_users(self) -> tuple[UserRecord, ...]: ...

    def disable_user(
        self,
        *,
        user_id: str,
        now: datetime,
    ) -> UserRecord | None: ...

    def create_verified_user(
        self,
        *,
        email: str,
        password_hash: str,
        role: UserRole,
        policy_version: str,
        now: datetime,
        pending_ttl: timedelta,
    ) -> RegistrationDecision: ...

    def replace_token(
        self,
        *,
        user_id: str,
        purpose: TokenPurpose,
        token_digest: str,
        now: datetime,
        expires_at: datetime,
    ) -> None: ...

    def consume_email_verification(
        self,
        *,
        token_digest: str,
        now: datetime,
    ) -> UserRecord | None: ...

    def consume_password_reset(
        self,
        *,
        token_digest: str,
        new_password_hash: str,
        now: datetime,
    ) -> UserRecord | None: ...

    def update_password_hash(
        self,
        *,
        user_id: str,
        password_hash: str,
        now: datetime,
    ) -> None: ...

    def create_session(
        self,
        *,
        user_id: str,
        token_digest: str,
        now: datetime,
        expires_at: datetime,
    ) -> AuthSessionRecord: ...

    def get_auth_context(
        self,
        *,
        token_digest: str,
        now: datetime,
    ) -> AuthContext | None: ...

    def set_csrf_digest(
        self,
        *,
        token_digest: str,
        csrf_digest: str,
        now: datetime,
    ) -> bool: ...

    def validate_csrf_digest(
        self,
        *,
        token_digest: str,
        csrf_digest: str,
        now: datetime,
    ) -> AuthContext | None: ...

    def revoke_session(
        self,
        *,
        token_digest: str,
        now: datetime,
    ) -> None: ...

    def revoke_user_sessions(
        self,
        *,
        user_id: str,
        now: datetime,
    ) -> int: ...

    def change_password_and_create_session(
        self,
        *,
        user_id: str,
        password_hash: str,
        token_digest: str,
        now: datetime,
        expires_at: datetime,
    ) -> AuthSessionRecord: ...

    def record_privacy_acceptance(
        self,
        *,
        user_id: str,
        context: PrivacyContext,
        policy_version: str,
        accepted_at: datetime,
    ) -> None: ...

    def has_privacy_acceptance(
        self,
        *,
        user_id: str,
        context: PrivacyContext,
        policy_version: str,
    ) -> bool: ...

    def consume_rate_limit(
        self,
        *,
        action: str,
        key_digest: str,
        window_started_at: datetime,
        expires_at: datetime,
        limit: int,
    ) -> bool: ...


@dataclass(slots=True)
class _TokenState:
    user_id: str
    purpose: TokenPurpose
    digest: str
    created_at: datetime
    expires_at: datetime
    consumed_at: datetime | None = None
    invalidated_at: datetime | None = None


class InMemoryAuthStore:
    def __init__(self, *, capacity_limit: int = 100) -> None:
        if capacity_limit < 1:
            raise ValueError("注册容量必须大于 0")
        self.capacity_limit = capacity_limit
        self._users: dict[str, UserRecord] = {}
        self._email_index: dict[str, str] = {}
        self._tokens: dict[str, _TokenState] = {}
        self._sessions: dict[str, AuthSessionRecord] = {}
        self._privacy: set[tuple[str, str, str]] = set()
        self._rates: dict[tuple[str, str, datetime], int] = {}
        self._lock = RLock()

    def register_pending(
        self,
        *,
        email: str,
        password_hash: str,
        policy_version: str,
        now: datetime,
        pending_ttl: timedelta,
        capacity_limit: int | None = None,
    ) -> RegistrationDecision:
        with self._lock:
            effective_limit = _effective_capacity_limit(
                self.capacity_limit,
                capacity_limit,
            )
            self._expire_pending(now=now, pending_ttl=pending_ttl)
            existing_id = self._email_index.get(email)
            if existing_id is not None:
                return RegistrationDecision(
                    status="duplicate",
                    user=self._users[existing_id],
                )
            if (
                self.capacity_used(now=now, pending_ttl=pending_ttl)
                >= effective_limit
            ):
                return RegistrationDecision(status="capacity_full")
            user = UserRecord(
                id=str(uuid4()),
                email=email,
                password_hash=password_hash,
                status="pending_verification",
                role="user",
                created_at=now,
                updated_at=now,
            )
            self._users[user.id] = user
            self._email_index[email] = user.id
            self._privacy.add(
                (user.id, "registration", policy_version)
            )
            return RegistrationDecision(status="created", user=user)

    def create_verified_user(
        self,
        *,
        email: str,
        password_hash: str,
        role: UserRole,
        policy_version: str,
        now: datetime,
        pending_ttl: timedelta = timedelta(hours=24),
    ) -> RegistrationDecision:
        with self._lock:
            self._expire_pending(now=now, pending_ttl=pending_ttl)
            existing_id = self._email_index.get(email)
            if existing_id is not None:
                return RegistrationDecision(
                    status="duplicate",
                    user=self._users[existing_id],
                )
            if self.capacity_used(
                now=now,
                pending_ttl=pending_ttl,
            ) >= self.capacity_limit:
                return RegistrationDecision(status="capacity_full")
            user = UserRecord(
                id=str(uuid4()),
                email=email,
                password_hash=password_hash,
                status="active",
                role=role,
                created_at=now,
                updated_at=now,
                verified_at=now,
            )
            self._users[user.id] = user
            self._email_index[email] = user.id
            self._privacy.add(
                (user.id, "registration", policy_version)
            )
            return RegistrationDecision(status="created", user=user)

    def capacity_used(
        self,
        *,
        now: datetime,
        pending_ttl: timedelta = timedelta(hours=24),
    ) -> int:
        with self._lock:
            cutoff = now - pending_ttl
            return sum(
                user.status in {"active", "disabled"}
                or (
                    user.status == "pending_verification"
                    and user.created_at > cutoff
                )
                for user in self._users.values()
            )

    def get_user_by_email(self, email: str) -> UserRecord | None:
        with self._lock:
            user_id = self._email_index.get(email)
            return self._users.get(user_id) if user_id else None

    def get_user_by_id(self, user_id: str) -> UserRecord | None:
        with self._lock:
            user = self._users.get(user_id)
            if user is None or user.status == "deleted":
                return None
            return user

    def list_users(self) -> tuple[UserRecord, ...]:
        with self._lock:
            return tuple(
                sorted(
                    (
                        user
                        for user in self._users.values()
                        if user.status != "deleted"
                    ),
                    key=lambda user: (user.created_at, user.id),
                )
            )

    def disable_user(
        self,
        *,
        user_id: str,
        now: datetime,
    ) -> UserRecord | None:
        with self._lock:
            user = self._users.get(user_id)
            if user is None or user.status == "deleted":
                return None
            disabled = replace(
                user,
                status="disabled",
                disabled_at=now,
                updated_at=now,
            )
            self._users[user_id] = disabled
            self._revoke_user_sessions(user_id, now=now)
            return disabled

    def replace_token(
        self,
        *,
        user_id: str,
        purpose: TokenPurpose,
        token_digest: str,
        now: datetime,
        expires_at: datetime,
    ) -> None:
        with self._lock:
            for state in self._tokens.values():
                if (
                    state.user_id == user_id
                    and state.purpose == purpose
                    and state.consumed_at is None
                    and state.invalidated_at is None
                ):
                    state.invalidated_at = now
            self._tokens[token_digest] = _TokenState(
                user_id=user_id,
                purpose=purpose,
                digest=token_digest,
                created_at=now,
                expires_at=expires_at,
            )

    def consume_email_verification(
        self,
        *,
        token_digest: str,
        now: datetime,
    ) -> UserRecord | None:
        with self._lock:
            state = self._active_token(
                token_digest,
                purpose="email_verification",
                now=now,
            )
            if state is None:
                return None
            user = self._users.get(state.user_id)
            if user is None or user.status != "pending_verification":
                return None
            state.consumed_at = now
            activated = replace(
                user,
                status="active",
                verified_at=now,
                updated_at=now,
            )
            self._users[user.id] = activated
            return activated

    def consume_password_reset(
        self,
        *,
        token_digest: str,
        new_password_hash: str,
        now: datetime,
    ) -> UserRecord | None:
        with self._lock:
            state = self._active_token(
                token_digest,
                purpose="password_reset",
                now=now,
            )
            if state is None:
                return None
            user = self._users.get(state.user_id)
            if user is None or user.status != "active":
                return None
            state.consumed_at = now
            updated_user = replace(
                user,
                password_hash=new_password_hash,
                updated_at=now,
            )
            self._users[user.id] = updated_user
            self._revoke_user_sessions(user.id, now=now)
            return updated_user

    def update_password_hash(
        self,
        *,
        user_id: str,
        password_hash: str,
        now: datetime,
    ) -> None:
        with self._lock:
            user = self._users[user_id]
            self._users[user_id] = replace(
                user,
                password_hash=password_hash,
                updated_at=now,
            )

    def create_session(
        self,
        *,
        user_id: str,
        token_digest: str,
        now: datetime,
        expires_at: datetime,
    ) -> AuthSessionRecord:
        with self._lock:
            session = AuthSessionRecord(
                id=str(uuid4()),
                user_id=user_id,
                token_digest=token_digest,
                csrf_digest=None,
                created_at=now,
                last_seen_at=now,
                expires_at=expires_at,
            )
            self._sessions[token_digest] = session
            return session

    def get_auth_context(
        self,
        *,
        token_digest: str,
        now: datetime,
    ) -> AuthContext | None:
        with self._lock:
            return self._auth_context(token_digest=token_digest, now=now)

    def set_csrf_digest(
        self,
        *,
        token_digest: str,
        csrf_digest: str,
        now: datetime,
    ) -> bool:
        with self._lock:
            context = self._auth_context(
                token_digest=token_digest,
                now=now,
            )
            if context is None:
                return False
            self._sessions[token_digest] = replace(
                context.session,
                csrf_digest=csrf_digest,
                last_seen_at=now,
            )
            return True

    def validate_csrf_digest(
        self,
        *,
        token_digest: str,
        csrf_digest: str,
        now: datetime,
    ) -> AuthContext | None:
        with self._lock:
            context = self._auth_context(
                token_digest=token_digest,
                now=now,
            )
            if (
                context is None
                or context.session.csrf_digest != csrf_digest
            ):
                return None
            return context

    def revoke_session(
        self,
        *,
        token_digest: str,
        now: datetime,
    ) -> None:
        with self._lock:
            session = self._sessions.get(token_digest)
            if session is not None and session.revoked_at is None:
                self._sessions[token_digest] = replace(
                    session,
                    revoked_at=now,
                )

    def revoke_user_sessions(
        self,
        *,
        user_id: str,
        now: datetime,
    ) -> int:
        with self._lock:
            active_count = sum(
                session.user_id == user_id
                and session.revoked_at is None
                for session in self._sessions.values()
            )
            self._revoke_user_sessions(user_id, now=now)
            return active_count

    def change_password_and_create_session(
        self,
        *,
        user_id: str,
        password_hash: str,
        token_digest: str,
        now: datetime,
        expires_at: datetime,
    ) -> AuthSessionRecord:
        with self._lock:
            user = self._users[user_id]
            self._users[user_id] = replace(
                user,
                password_hash=password_hash,
                updated_at=now,
            )
            self._revoke_user_sessions(user_id, now=now)
            return self.create_session(
                user_id=user_id,
                token_digest=token_digest,
                now=now,
                expires_at=expires_at,
            )

    def record_privacy_acceptance(
        self,
        *,
        user_id: str,
        context: PrivacyContext,
        policy_version: str,
        accepted_at: datetime,
    ) -> None:
        del accepted_at
        with self._lock:
            self._privacy.add((user_id, context, policy_version))

    def has_privacy_acceptance(
        self,
        *,
        user_id: str,
        context: PrivacyContext,
        policy_version: str,
    ) -> bool:
        with self._lock:
            return (user_id, context, policy_version) in self._privacy

    def consume_rate_limit(
        self,
        *,
        action: str,
        key_digest: str,
        window_started_at: datetime,
        expires_at: datetime,
        limit: int,
    ) -> bool:
        del expires_at
        key = (action, key_digest, window_started_at)
        with self._lock:
            count = self._rates.get(key, 0)
            if count >= limit:
                return False
            self._rates[key] = count + 1
            return True

    def _active_token(
        self,
        token_digest: str,
        *,
        purpose: TokenPurpose,
        now: datetime,
    ) -> _TokenState | None:
        state = self._tokens.get(token_digest)
        if (
            state is None
            or state.purpose != purpose
            or state.consumed_at is not None
            or state.invalidated_at is not None
            or state.expires_at <= now
        ):
            return None
        return state

    def _auth_context(
        self,
        *,
        token_digest: str,
        now: datetime,
    ) -> AuthContext | None:
        session = self._sessions.get(token_digest)
        if (
            session is None
            or session.revoked_at is not None
            or session.expires_at <= now
        ):
            return None
        user = self._users.get(session.user_id)
        if user is None or user.status != "active":
            return None
        if session.last_seen_at != now:
            session = replace(session, last_seen_at=now)
            self._sessions[token_digest] = session
        return AuthContext(user=user, session=session)

    def _revoke_user_sessions(
        self,
        user_id: str,
        *,
        now: datetime,
    ) -> None:
        for digest, session in tuple(self._sessions.items()):
            if session.user_id == user_id and session.revoked_at is None:
                self._sessions[digest] = replace(
                    session,
                    revoked_at=now,
                )

    def _expire_pending(
        self,
        *,
        now: datetime,
        pending_ttl: timedelta,
    ) -> None:
        cutoff = now - pending_ttl
        for user_id, user in tuple(self._users.items()):
            if (
                user.status == "pending_verification"
                and user.created_at <= cutoff
            ):
                self._email_index.pop(user.email, None)
                self._users[user_id] = replace(
                    user,
                    email="",
                    password_hash="",
                    status="deleted",
                    updated_at=now,
                    deleted_at=now,
                )


class PostgresAuthStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def register_pending(
        self,
        *,
        email: str,
        password_hash: str,
        policy_version: str,
        now: datetime,
        pending_ttl: timedelta,
        capacity_limit: int | None = None,
    ) -> RegistrationDecision:
        cutoff = now - pending_ttl
        user_id = str(uuid4())
        with self.engine.begin() as connection:
            capacity = connection.execute(
                select(registration_capacity)
                .where(registration_capacity.c.id == 1)
                .with_for_update()
            ).mappings().one()
            effective_limit = _effective_capacity_limit(
                int(capacity["capacity_limit"]),
                capacity_limit,
            )
            connection.execute(
                update(users)
                .where(
                    users.c.status == "pending_verification",
                    users.c.created_at <= cutoff,
                )
                .values(
                    email_normalized=None,
                    password_hash=None,
                    status="deleted",
                    deleted_at=now,
                    updated_at=now,
                )
            )
            existing = connection.execute(
                select(users).where(
                    users.c.email_normalized == email,
                    users.c.status != "deleted",
                )
            ).mappings().one_or_none()
            if existing is not None:
                return RegistrationDecision(
                    status="duplicate",
                    user=_user_from_row(existing),
                )
            used = connection.execute(
                select(func.count())
                .select_from(users)
                .where(
                    or_(
                        users.c.status.in_(("active", "disabled")),
                        and_(
                            users.c.status == "pending_verification",
                            users.c.created_at > cutoff,
                        ),
                    )
                )
            ).scalar_one()
            if used >= effective_limit:
                return RegistrationDecision(status="capacity_full")
            connection.execute(
                insert(users).values(
                    id=user_id,
                    email_normalized=email,
                    password_hash=password_hash,
                    status="pending_verification",
                    role="user",
                    created_at=now,
                    updated_at=now,
                )
            )
            connection.execute(
                insert(privacy_acceptances).values(
                    id=str(uuid4()),
                    user_id=user_id,
                    context="registration",
                    policy_version=policy_version,
                    accepted_at=now,
                )
            )
            row = connection.execute(
                select(users).where(users.c.id == user_id)
            ).mappings().one()
            return RegistrationDecision(
                status="created",
                user=_user_from_row(row),
            )

    def create_verified_user(
        self,
        *,
        email: str,
        password_hash: str,
        role: UserRole,
        policy_version: str,
        now: datetime,
        pending_ttl: timedelta = timedelta(hours=24),
    ) -> RegistrationDecision:
        cutoff = now - pending_ttl
        user_id = str(uuid4())
        with self.engine.begin() as connection:
            capacity = connection.execute(
                select(registration_capacity)
                .where(registration_capacity.c.id == 1)
                .with_for_update()
            ).mappings().one()
            connection.execute(
                update(users)
                .where(
                    users.c.status == "pending_verification",
                    users.c.created_at <= cutoff,
                )
                .values(
                    email_normalized=None,
                    password_hash=None,
                    status="deleted",
                    deleted_at=now,
                    updated_at=now,
                )
            )
            existing = connection.execute(
                select(users).where(
                    users.c.email_normalized == email,
                    users.c.status != "deleted",
                )
            ).mappings().one_or_none()
            if existing is not None:
                return RegistrationDecision(
                    status="duplicate",
                    user=_user_from_row(existing),
                )
            used = connection.execute(
                select(func.count())
                .select_from(users)
                .where(
                    or_(
                        users.c.status.in_(("active", "disabled")),
                        and_(
                            users.c.status == "pending_verification",
                            users.c.created_at > cutoff,
                        ),
                    )
                )
            ).scalar_one()
            if used >= capacity["capacity_limit"]:
                return RegistrationDecision(status="capacity_full")
            connection.execute(
                insert(users).values(
                    id=user_id,
                    email_normalized=email,
                    password_hash=password_hash,
                    status="active",
                    role=role,
                    verified_at=now,
                    created_at=now,
                    updated_at=now,
                )
            )
            connection.execute(
                insert(privacy_acceptances).values(
                    id=str(uuid4()),
                    user_id=user_id,
                    context="registration",
                    policy_version=policy_version,
                    accepted_at=now,
                )
            )
            row = connection.execute(
                select(users).where(users.c.id == user_id)
            ).mappings().one()
        return RegistrationDecision(
            status="created",
            user=_user_from_row(row),
        )

    def get_user_by_email(self, email: str) -> UserRecord | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(users).where(
                    users.c.email_normalized == email,
                    users.c.status != "deleted",
                )
            ).mappings().one_or_none()
        return _user_from_row(row) if row is not None else None

    def get_user_by_id(self, user_id: str) -> UserRecord | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(users).where(
                    users.c.id == _uuid(user_id),
                    users.c.status != "deleted",
                )
            ).mappings().one_or_none()
        return _user_from_row(row) if row is not None else None

    def list_users(self) -> tuple[UserRecord, ...]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(users)
                .where(
                    users.c.status.not_in(("deleted", "local")),
                )
                .order_by(users.c.created_at, users.c.id)
            ).mappings().all()
        return tuple(_user_from_row(row) for row in rows)

    def disable_user(
        self,
        *,
        user_id: str,
        now: datetime,
    ) -> UserRecord | None:
        normalized_user_id = _uuid(user_id)
        with self.engine.begin() as connection:
            row = connection.execute(
                select(users)
                .where(
                    users.c.id == normalized_user_id,
                    users.c.status != "deleted",
                )
                .with_for_update()
            ).mappings().one_or_none()
            if row is None:
                return None
            connection.execute(
                update(users)
                .where(users.c.id == normalized_user_id)
                .values(
                    status="disabled",
                    disabled_at=now,
                    updated_at=now,
                )
            )
            connection.execute(
                update(auth_sessions)
                .where(
                    auth_sessions.c.user_id == normalized_user_id,
                    auth_sessions.c.revoked_at.is_(None),
                )
                .values(revoked_at=now)
            )
            disabled = connection.execute(
                select(users).where(users.c.id == normalized_user_id)
            ).mappings().one()
        return _user_from_row(disabled)

    def replace_token(
        self,
        *,
        user_id: str,
        purpose: TokenPurpose,
        token_digest: str,
        now: datetime,
        expires_at: datetime,
    ) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                update(auth_tokens)
                .where(
                    auth_tokens.c.user_id == _uuid(user_id),
                    auth_tokens.c.purpose == purpose,
                    auth_tokens.c.consumed_at.is_(None),
                    auth_tokens.c.invalidated_at.is_(None),
                )
                .values(invalidated_at=now)
            )
            connection.execute(
                insert(auth_tokens).values(
                    id=str(uuid4()),
                    user_id=_uuid(user_id),
                    purpose=purpose,
                    token_digest=token_digest,
                    created_at=now,
                    expires_at=expires_at,
                )
            )

    def consume_email_verification(
        self,
        *,
        token_digest: str,
        now: datetime,
    ) -> UserRecord | None:
        with self.engine.begin() as connection:
            token = self._lock_active_token(
                connection,
                token_digest=token_digest,
                purpose="email_verification",
                now=now,
            )
            if token is None:
                return None
            user = connection.execute(
                select(users)
                .where(
                    users.c.id == token["user_id"],
                    users.c.status == "pending_verification",
                )
                .with_for_update()
            ).mappings().one_or_none()
            if user is None:
                return None
            connection.execute(
                update(auth_tokens)
                .where(auth_tokens.c.id == token["id"])
                .values(consumed_at=now)
            )
            connection.execute(
                update(users)
                .where(users.c.id == token["user_id"])
                .values(
                    status="active",
                    verified_at=now,
                    updated_at=now,
                )
            )
            row = connection.execute(
                select(users).where(users.c.id == token["user_id"])
            ).mappings().one()
            return _user_from_row(row)

    def consume_password_reset(
        self,
        *,
        token_digest: str,
        new_password_hash: str,
        now: datetime,
    ) -> UserRecord | None:
        with self.engine.begin() as connection:
            token = self._lock_active_token(
                connection,
                token_digest=token_digest,
                purpose="password_reset",
                now=now,
            )
            if token is None:
                return None
            user = connection.execute(
                select(users)
                .where(
                    users.c.id == token["user_id"],
                    users.c.status == "active",
                )
                .with_for_update()
            ).mappings().one_or_none()
            if user is None:
                return None
            connection.execute(
                update(auth_tokens)
                .where(auth_tokens.c.id == token["id"])
                .values(consumed_at=now)
            )
            connection.execute(
                update(users)
                .where(users.c.id == token["user_id"])
                .values(
                    password_hash=new_password_hash,
                    updated_at=now,
                )
            )
            connection.execute(
                update(auth_sessions)
                .where(
                    auth_sessions.c.user_id == token["user_id"],
                    auth_sessions.c.revoked_at.is_(None),
                )
                .values(revoked_at=now)
            )
            row = connection.execute(
                select(users).where(users.c.id == token["user_id"])
            ).mappings().one()
            return _user_from_row(row)

    def update_password_hash(
        self,
        *,
        user_id: str,
        password_hash: str,
        now: datetime,
    ) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                update(users)
                .where(users.c.id == _uuid(user_id))
                .values(password_hash=password_hash, updated_at=now)
            )

    def create_session(
        self,
        *,
        user_id: str,
        token_digest: str,
        now: datetime,
        expires_at: datetime,
    ) -> AuthSessionRecord:
        session_id = str(uuid4())
        with self.engine.begin() as connection:
            connection.execute(
                insert(auth_sessions).values(
                    id=session_id,
                    user_id=_uuid(user_id),
                    token_digest=token_digest,
                    created_at=now,
                    last_seen_at=now,
                    expires_at=expires_at,
                )
            )
            row = connection.execute(
                select(auth_sessions).where(
                    auth_sessions.c.id == session_id
                )
            ).mappings().one()
        return _session_from_row(row)

    def get_auth_context(
        self,
        *,
        token_digest: str,
        now: datetime,
    ) -> AuthContext | None:
        with self.engine.begin() as connection:
            context = self._get_auth_context(
                connection,
                token_digest=token_digest,
                now=now,
            )
            if context is not None:
                connection.execute(
                    update(auth_sessions)
                    .where(auth_sessions.c.id == context.session.id)
                    .values(last_seen_at=now)
                )
                context = AuthContext(
                    user=context.user,
                    session=replace(
                        context.session,
                        last_seen_at=now,
                    ),
                )
            return context

    def set_csrf_digest(
        self,
        *,
        token_digest: str,
        csrf_digest: str,
        now: datetime,
    ) -> bool:
        with self.engine.begin() as connection:
            result = connection.execute(
                update(auth_sessions)
                .where(
                    auth_sessions.c.token_digest == token_digest,
                    auth_sessions.c.revoked_at.is_(None),
                    auth_sessions.c.expires_at > now,
                )
                .values(csrf_digest=csrf_digest, last_seen_at=now)
            )
            return result.rowcount == 1

    def validate_csrf_digest(
        self,
        *,
        token_digest: str,
        csrf_digest: str,
        now: datetime,
    ) -> AuthContext | None:
        with self.engine.connect() as connection:
            return self._get_auth_context(
                connection,
                token_digest=token_digest,
                csrf_digest=csrf_digest,
                now=now,
            )

    def revoke_session(
        self,
        *,
        token_digest: str,
        now: datetime,
    ) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                update(auth_sessions)
                .where(
                    auth_sessions.c.token_digest == token_digest,
                    auth_sessions.c.revoked_at.is_(None),
                )
                .values(revoked_at=now)
            )

    def revoke_user_sessions(
        self,
        *,
        user_id: str,
        now: datetime,
    ) -> int:
        with self.engine.begin() as connection:
            result = connection.execute(
                update(auth_sessions)
                .where(
                    auth_sessions.c.user_id == _uuid(user_id),
                    auth_sessions.c.revoked_at.is_(None),
                )
                .values(revoked_at=now)
            )
        return int(result.rowcount or 0)

    def change_password_and_create_session(
        self,
        *,
        user_id: str,
        password_hash: str,
        token_digest: str,
        now: datetime,
        expires_at: datetime,
    ) -> AuthSessionRecord:
        normalized_user_id = _uuid(user_id)
        session_id = str(uuid4())
        with self.engine.begin() as connection:
            connection.execute(
                update(users)
                .where(
                    users.c.id == normalized_user_id,
                    users.c.status == "active",
                )
                .values(password_hash=password_hash, updated_at=now)
            )
            connection.execute(
                update(auth_sessions)
                .where(
                    auth_sessions.c.user_id == normalized_user_id,
                    auth_sessions.c.revoked_at.is_(None),
                )
                .values(revoked_at=now)
            )
            connection.execute(
                insert(auth_sessions).values(
                    id=session_id,
                    user_id=normalized_user_id,
                    token_digest=token_digest,
                    created_at=now,
                    last_seen_at=now,
                    expires_at=expires_at,
                )
            )
            row = connection.execute(
                select(auth_sessions).where(
                    auth_sessions.c.id == session_id
                )
            ).mappings().one()
        return _session_from_row(row)

    def record_privacy_acceptance(
        self,
        *,
        user_id: str,
        context: PrivacyContext,
        policy_version: str,
        accepted_at: datetime,
    ) -> None:
        statement = (
            postgresql_insert(privacy_acceptances)
            .values(
                id=str(uuid4()),
                user_id=_uuid(user_id),
                context=context,
                policy_version=policy_version,
                accepted_at=accepted_at,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    privacy_acceptances.c.user_id,
                    privacy_acceptances.c.context,
                    privacy_acceptances.c.policy_version,
                ]
            )
        )
        with self.engine.begin() as connection:
            connection.execute(statement)

    def has_privacy_acceptance(
        self,
        *,
        user_id: str,
        context: PrivacyContext,
        policy_version: str,
    ) -> bool:
        with self.engine.connect() as connection:
            value = connection.execute(
                select(privacy_acceptances.c.id).where(
                    privacy_acceptances.c.user_id == _uuid(user_id),
                    privacy_acceptances.c.context == context,
                    privacy_acceptances.c.policy_version
                    == policy_version,
                )
            ).scalar_one_or_none()
        return value is not None

    def consume_rate_limit(
        self,
        *,
        action: str,
        key_digest: str,
        window_started_at: datetime,
        expires_at: datetime,
        limit: int,
    ) -> bool:
        statement = (
            postgresql_insert(security_rate_limits)
            .values(
                id=str(uuid4()),
                action=action,
                key_digest=key_digest,
                window_started_at=window_started_at,
                request_count=1,
                expires_at=expires_at,
            )
            .on_conflict_do_update(
                index_elements=[
                    security_rate_limits.c.action,
                    security_rate_limits.c.key_digest,
                    security_rate_limits.c.window_started_at,
                ],
                set_={
                    "request_count": (
                        security_rate_limits.c.request_count + 1
                    ),
                    "expires_at": expires_at,
                },
                where=security_rate_limits.c.request_count < limit,
            )
            .returning(security_rate_limits.c.request_count)
        )
        with self.engine.begin() as connection:
            count = connection.execute(statement).scalar_one_or_none()
        return count is not None and count <= limit

    def _lock_active_token(
        self,
        connection: object,
        *,
        token_digest: str,
        purpose: TokenPurpose,
        now: datetime,
    ) -> RowMapping | None:
        return connection.execute(
            select(auth_tokens)
            .where(
                auth_tokens.c.token_digest == token_digest,
                auth_tokens.c.purpose == purpose,
                auth_tokens.c.consumed_at.is_(None),
                auth_tokens.c.invalidated_at.is_(None),
                auth_tokens.c.expires_at > now,
            )
            .with_for_update()
        ).mappings().one_or_none()

    def _get_auth_context(
        self,
        connection: object,
        *,
        token_digest: str,
        now: datetime,
        csrf_digest: str | None = None,
    ) -> AuthContext | None:
        conditions = [
            auth_sessions.c.token_digest == token_digest,
            auth_sessions.c.revoked_at.is_(None),
            auth_sessions.c.expires_at > now,
        ]
        if csrf_digest is not None:
            conditions.append(auth_sessions.c.csrf_digest == csrf_digest)
        session_row = connection.execute(
            select(auth_sessions).where(*conditions)
        ).mappings().one_or_none()
        if session_row is None:
            return None
        user_row = connection.execute(
            select(users).where(
                users.c.id == session_row["user_id"],
                users.c.status == "active",
            )
        ).mappings().one_or_none()
        if user_row is None:
            return None
        return AuthContext(
            user=_user_from_row(user_row),
            session=_session_from_row(session_row),
        )


def _uuid(value: str) -> str:
    return str(UUID(value))


def _effective_capacity_limit(
    global_limit: int,
    override: int | None,
) -> int:
    if global_limit < 1:
        raise ValueError("注册容量必须大于 0")
    if override is None:
        return global_limit
    if override < 1:
        raise ValueError("注册容量覆盖值必须大于 0")
    return min(global_limit, override)


def _user_from_row(row: RowMapping) -> UserRecord:
    return UserRecord(
        id=str(row["id"]),
        email=row["email_normalized"],
        password_hash=row["password_hash"],
        status=row["status"],
        role=row["role"],
        verified_at=row["verified_at"],
        disabled_at=row["disabled_at"],
        deleted_at=row["deleted_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _session_from_row(row: RowMapping) -> AuthSessionRecord:
    return AuthSessionRecord(
        id=str(row["id"]),
        user_id=str(row["user_id"]),
        token_digest=row["token_digest"],
        csrf_digest=row["csrf_digest"],
        created_at=row["created_at"],
        last_seen_at=row["last_seen_at"],
        expires_at=row["expires_at"],
        revoked_at=row["revoked_at"],
    )
