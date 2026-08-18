from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import uuid4

from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.engine import Engine

from app.auth.errors import CaptchaFailedError, PrivacyRequiredError
from app.integrations.captcha import (
    CaptchaVerificationError,
    CaptchaVerifier,
)
from app.security.network import hmac_ip_digest, normalize_ip_address
from app.trial.models import (
    TrialIdentity,
    TrialIdentityLimitError,
    TrialStartResult,
)


IDENTITY_TTL = timedelta(days=365)
IP_GRANT_TTL = timedelta(days=30)
PENDING_IP_GRANT_TTL = timedelta(minutes=15)
MAX_IDENTITIES_PER_IP = 3


class TrialIdentityStore(Protocol):
    def get_by_token_digest(
        self,
        token_digest: str,
        *,
        now: datetime,
    ) -> TrialIdentity | None: ...

    def create(
        self,
        *,
        token_digest: str,
        ip_digest: str,
        policy_version: str,
        now: datetime,
        expires_at: datetime,
        max_ip_grants: int,
        ip_grant_expires_at: datetime,
    ) -> TrialIdentity: ...

    def activate_ip_grant(
        self,
        *,
        identity_id: str,
        ip_digest: str,
        now: datetime,
        max_ip_grants: int,
        pending_ip_grant_ttl: timedelta,
        ip_grant_expires_at: datetime,
    ) -> None: ...


class TrialTokenCodec:
    def __init__(self, secret: bytes) -> None:
        if len(secret) < 32:
            raise ValueError("试用身份密钥至少需要 32 字节")
        self._secret = bytes(secret)

    def issue(self) -> str:
        return secrets.token_urlsafe(32)

    def digest(self, token: str) -> str:
        normalized = token.strip()
        if not normalized:
            return ""
        return hmac.new(
            self._secret,
            b"weiquan-trial-token-v1:" + normalized.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()


class InMemoryTrialIdentityStore:
    def __init__(self) -> None:
        self._identities: dict[str, TrialIdentity] = {}
        self._ip_grants: list[tuple[str, str, datetime, datetime]] = []
        self._lock = threading.RLock()

    @property
    def identity_count(self) -> int:
        with self._lock:
            return len(self._identities)

    def get_by_token_digest(
        self,
        token_digest: str,
        *,
        now: datetime,
    ) -> TrialIdentity | None:
        current = _utc(now)
        with self._lock:
            identity = self._identities.get(token_digest)
            if identity is None or identity.expires_at <= current:
                return None
            return identity

    def create(
        self,
        *,
        token_digest: str,
        ip_digest: str,
        policy_version: str,
        now: datetime,
        expires_at: datetime,
        max_ip_grants: int,
        ip_grant_expires_at: datetime,
    ) -> TrialIdentity:
        current = _utc(now)
        with self._lock:
            self._ip_grants = [
                grant
                for grant in self._ip_grants
                if grant[3] > current
            ]
            grant_count = sum(
                grant_ip == ip_digest
                for _, grant_ip, _, _ in self._ip_grants
            )
            if grant_count >= max_ip_grants:
                raise TrialIdentityLimitError()
            existing = self._identities.get(token_digest)
            if existing is not None:
                return existing
            identity = TrialIdentity(
                id=str(uuid4()),
                token_digest=token_digest,
                ip_digest=ip_digest,
                policy_version=policy_version,
                created_at=current,
                expires_at=_utc(expires_at),
            )
            self._identities[token_digest] = identity
            self._ip_grants.append(
                (
                    identity.id,
                    ip_digest,
                    current,
                    _utc(ip_grant_expires_at),
                )
            )
            return identity

    def active_ip_grant_count(
        self,
        ip_digest: str,
        *,
        now: datetime,
    ) -> int:
        current = _utc(now)
        with self._lock:
            return sum(
                grant_ip == ip_digest and expires_at > current
                for _, grant_ip, _, expires_at in self._ip_grants
            )

    def activate_ip_grant(
        self,
        *,
        identity_id: str,
        ip_digest: str,
        now: datetime,
        max_ip_grants: int,
        pending_ip_grant_ttl: timedelta,
        ip_grant_expires_at: datetime,
    ) -> None:
        current = _utc(now)
        target_expiry = _utc(ip_grant_expires_at)
        with self._lock:
            self._ip_grants = [
                grant
                for grant in self._ip_grants
                if grant[3] > current
            ]
            for index, grant in enumerate(self._ip_grants):
                grant_id, grant_ip, created_at, expires_at = grant
                if grant_id != identity_id:
                    continue
                if grant_ip != ip_digest:
                    raise ValueError(
                        "试用身份与 IP 授权记录不一致"
                    )
                if expires_at > created_at + pending_ip_grant_ttl:
                    return
                self._ip_grants[index] = (
                    grant_id,
                    grant_ip,
                    created_at,
                    target_expiry,
                )
                return

            grant_count = sum(
                grant_ip == ip_digest
                for _, grant_ip, _, _ in self._ip_grants
            )
            if grant_count >= max_ip_grants:
                raise TrialIdentityLimitError()
            self._ip_grants.append(
                (
                    identity_id,
                    ip_digest,
                    current,
                    target_expiry,
                )
            )

    def raw_values(self) -> set[str]:
        with self._lock:
            values = {
                identity.token_digest
                for identity in self._identities.values()
            }
            values.update(
                identity.ip_digest
                for identity in self._identities.values()
            )
            return values


class PostgresTrialIdentityStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def get_by_token_digest(
        self,
        token_digest: str,
        *,
        now: datetime,
    ) -> TrialIdentity | None:
        from app.db.tables import trial_identities

        with self._engine.connect() as connection:
            row = connection.execute(
                select(trial_identities).where(
                    trial_identities.c.token_digest == token_digest,
                    trial_identities.c.expires_at > _utc(now),
                )
            ).mappings().first()
        return _identity_from_row(row) if row is not None else None

    def create(
        self,
        *,
        token_digest: str,
        ip_digest: str,
        policy_version: str,
        now: datetime,
        expires_at: datetime,
        max_ip_grants: int,
        ip_grant_expires_at: datetime,
    ) -> TrialIdentity:
        from app.db.tables import trial_identities, trial_ip_grants

        current = _utc(now)
        identity = TrialIdentity(
            id=str(uuid4()),
            token_digest=token_digest,
            ip_digest=ip_digest,
            policy_version=policy_version,
            created_at=current,
            expires_at=_utc(expires_at),
        )
        with self._engine.begin() as connection:
            connection.execute(
                text(
                    "SELECT pg_advisory_xact_lock("
                    "hashtextextended(:ip_digest, 0))"
                ),
                {"ip_digest": ip_digest},
            )
            connection.execute(
                delete(trial_ip_grants).where(
                    trial_ip_grants.c.expires_at <= current
                )
            )
            count = connection.execute(
                select(func.count())
                .select_from(trial_ip_grants)
                .where(
                    trial_ip_grants.c.ip_digest == ip_digest,
                    trial_ip_grants.c.expires_at > current,
                )
            ).scalar_one()
            if int(count) >= max_ip_grants:
                raise TrialIdentityLimitError()
            connection.execute(
                insert(trial_identities).values(
                    id=identity.id,
                    token_digest=identity.token_digest,
                    ip_digest=identity.ip_digest,
                    policy_version=identity.policy_version,
                    created_at=identity.created_at,
                    expires_at=identity.expires_at,
                )
            )
            connection.execute(
                insert(trial_ip_grants).values(
                    id=str(uuid4()),
                    identity_id=identity.id,
                    ip_digest=ip_digest,
                    created_at=current,
                    expires_at=_utc(ip_grant_expires_at),
                )
            )
        return identity

    def activate_ip_grant(
        self,
        *,
        identity_id: str,
        ip_digest: str,
        now: datetime,
        max_ip_grants: int,
        pending_ip_grant_ttl: timedelta,
        ip_grant_expires_at: datetime,
    ) -> None:
        from app.db.tables import trial_ip_grants

        current = _utc(now)
        target_expiry = _utc(ip_grant_expires_at)
        with self._engine.begin() as connection:
            connection.execute(
                text(
                    "SELECT pg_advisory_xact_lock("
                    "hashtextextended(:ip_digest, 0))"
                ),
                {"ip_digest": ip_digest},
            )
            connection.execute(
                delete(trial_ip_grants).where(
                    trial_ip_grants.c.ip_digest == ip_digest,
                    trial_ip_grants.c.expires_at <= current,
                )
            )
            existing = connection.execute(
                select(trial_ip_grants)
                .where(
                    trial_ip_grants.c.identity_id == identity_id
                )
                .with_for_update()
            ).mappings().first()
            if existing is not None:
                if str(existing["ip_digest"]) != ip_digest:
                    raise ValueError(
                        "试用身份与 IP 授权记录不一致"
                    )
                if (
                    existing["expires_at"]
                    > existing["created_at"] + pending_ip_grant_ttl
                ):
                    return
                connection.execute(
                    update(trial_ip_grants)
                    .where(
                        trial_ip_grants.c.identity_id == identity_id
                    )
                    .values(expires_at=target_expiry)
                )
                return

            count = connection.execute(
                select(func.count())
                .select_from(trial_ip_grants)
                .where(
                    trial_ip_grants.c.ip_digest == ip_digest,
                    trial_ip_grants.c.expires_at > current,
                )
            ).scalar_one()
            if int(count) >= max_ip_grants:
                raise TrialIdentityLimitError()
            connection.execute(
                insert(trial_ip_grants).values(
                    id=str(uuid4()),
                    identity_id=identity_id,
                    ip_digest=ip_digest,
                    created_at=current,
                    expires_at=target_expiry,
                )
            )


class TrialIdentityManager:
    def __init__(
        self,
        *,
        store: TrialIdentityStore,
        captcha: CaptchaVerifier,
        policy_version: str,
        token_codec: TrialTokenCodec,
        ip_hmac_secret: bytes,
        identity_ttl: timedelta = IDENTITY_TTL,
        ip_grant_ttl: timedelta = IP_GRANT_TTL,
        pending_ip_grant_ttl: timedelta = PENDING_IP_GRANT_TTL,
        max_identities_per_ip: int = MAX_IDENTITIES_PER_IP,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        normalized_policy = policy_version.strip()
        if not normalized_policy:
            raise ValueError("隐私政策版本不能为空")
        self.store = store
        self.captcha = captcha
        self.policy_version = normalized_policy
        self.token_codec = token_codec
        self.ip_hmac_secret = bytes(ip_hmac_secret)
        if identity_ttl <= timedelta(0):
            raise ValueError("试用身份有效期必须为正数")
        if ip_grant_ttl <= timedelta(0):
            raise ValueError("IP 领取窗口必须为正数")
        if (
            pending_ip_grant_ttl <= timedelta(0)
            or pending_ip_grant_ttl > ip_grant_ttl
        ):
            raise ValueError(
                "待使用 IP 领取窗口必须为正数且不超过激活窗口"
            )
        if max_identities_per_ip < 1:
            raise ValueError("IP 领取上限必须大于零")
        self.identity_ttl = identity_ttl
        self.ip_grant_ttl = ip_grant_ttl
        self.pending_ip_grant_ttl = pending_ip_grant_ttl
        self.max_identities_per_ip = max_identities_per_ip
        self._now = now or (lambda: datetime.now(UTC))

    def authenticate(self, token: str | None) -> TrialIdentity | None:
        if token is None:
            return None
        digest = self.token_codec.digest(token)
        if not digest:
            return None
        return self.store.get_by_token_digest(
            digest,
            now=_utc(self._now()),
        )

    def start(
        self,
        *,
        existing_token: str | None,
        captcha_token: str,
        privacy_version: str,
        privacy_accepted: bool,
        client_ip: str,
    ) -> TrialStartResult:
        existing = self.authenticate(existing_token)
        if existing is not None:
            return TrialStartResult(
                identity=existing,
                cookie_value=None,
                created=False,
            )

        normalized_ip = normalize_ip_address(client_ip)
        try:
            captcha_valid = self.captcha.verify(
                token=captcha_token,
                remote_ip=normalized_ip,
            )
        except CaptchaVerificationError as exc:
            raise CaptchaFailedError() from exc
        if not captcha_valid:
            raise CaptchaFailedError()
        if (
            not privacy_accepted
            or privacy_version.strip() != self.policy_version
        ):
            raise PrivacyRequiredError()

        current = _utc(self._now())
        cookie_value = self.token_codec.issue()
        identity = self.store.create(
            token_digest=self.token_codec.digest(cookie_value),
            ip_digest=hmac_ip_digest(
                normalized_ip,
                self.ip_hmac_secret,
            ),
            policy_version=self.policy_version,
            now=current,
            expires_at=current + self.identity_ttl,
            max_ip_grants=self.max_identities_per_ip,
            ip_grant_expires_at=(
                current + self.pending_ip_grant_ttl
            ),
        )
        return TrialStartResult(
            identity=identity,
            cookie_value=cookie_value,
            created=True,
        )

    def activate_for_consult(
        self,
        identity: TrialIdentity,
    ) -> None:
        current = _utc(self._now())
        self.store.activate_ip_grant(
            identity_id=identity.id,
            ip_digest=identity.ip_digest,
            now=current,
            max_ip_grants=self.max_identities_per_ip,
            pending_ip_grant_ttl=self.pending_ip_grant_ttl,
            ip_grant_expires_at=current + self.ip_grant_ttl,
        )


def _identity_from_row(row: object) -> TrialIdentity:
    values = dict(row)  # type: ignore[arg-type]
    return TrialIdentity(
        id=str(values["id"]),
        token_digest=str(values["token_digest"]),
        ip_digest=str(values["ip_digest"]),
        policy_version=str(values["policy_version"]),
        created_at=values["created_at"],
        expires_at=values["expires_at"],
    )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("时间必须包含时区")
    return value.astimezone(UTC)
