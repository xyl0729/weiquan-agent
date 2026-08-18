from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest

from app.auth.errors import CaptchaFailedError, PrivacyRequiredError
from app.trial.identity import (
    InMemoryTrialIdentityStore,
    TrialIdentityManager,
    TrialTokenCodec,
)
from app.trial.models import TrialIdentityLimitError


NOW = datetime(2026, 8, 10, 8, 0, tzinfo=UTC)


class Captcha:
    def __init__(self) -> None:
        self.calls = 0

    def verify(self, *, token: str, remote_ip: str) -> bool:
        assert remote_ip
        self.calls += 1
        return token == "captcha-ok"


def _manager(
    store: InMemoryTrialIdentityStore,
    captcha: Captcha,
    *,
    now: datetime = NOW,
) -> TrialIdentityManager:
    return TrialIdentityManager(
        store=store,
        captcha=captcha,
        policy_version="2026-08-10",
        token_codec=TrialTokenCodec(b"t" * 32),
        ip_hmac_secret=b"i" * 32,
        now=lambda: now,
    )


def _start(
    manager: TrialIdentityManager,
    *,
    client_ip: str = "203.0.113.8",
):
    return manager.start(
        existing_token=None,
        captcha_token="captcha-ok",
        privacy_version="2026-08-10",
        privacy_accepted=True,
        client_ip=client_ip,
    )


def test_identity_is_only_created_after_captcha_and_privacy() -> None:
    store = InMemoryTrialIdentityStore()
    captcha = Captcha()
    manager = _manager(store, captcha)

    with pytest.raises(CaptchaFailedError):
        manager.start(
            existing_token=None,
            captcha_token="bad",
            privacy_version="2026-08-10",
            privacy_accepted=True,
            client_ip="203.0.113.8",
        )
    with pytest.raises(PrivacyRequiredError):
        manager.start(
            existing_token=None,
            captcha_token="captcha-ok",
            privacy_version="old",
            privacy_accepted=True,
            client_ip="203.0.113.8",
        )

    assert store.identity_count == 0


def test_cookie_is_random_opaque_and_only_its_digest_is_stored() -> None:
    store = InMemoryTrialIdentityStore()
    captcha = Captcha()
    manager = _manager(store, captcha)

    started = manager.start(
        existing_token=None,
        captcha_token="captcha-ok",
        privacy_version="2026-08-10",
        privacy_accepted=True,
        client_ip="203.0.113.8",
    )
    restored = manager.start(
        existing_token=started.cookie_value,
        captcha_token="",
        privacy_version="",
        privacy_accepted=False,
        client_ip="198.51.100.4",
    )

    assert len(started.cookie_value) >= 43
    assert started.identity.token_digest != started.cookie_value
    assert started.identity.expires_at == NOW + timedelta(days=365)
    assert restored.identity == started.identity
    assert restored.cookie_value is None
    assert restored.created is False
    assert captcha.calls == 1
    assert store.raw_values() == {
        started.identity.token_digest,
        started.identity.ip_digest,
    }
    assert started.cookie_value not in store.raw_values()


def test_tampered_cookie_does_not_authenticate() -> None:
    store = InMemoryTrialIdentityStore()
    captcha = Captcha()
    manager = _manager(store, captcha)
    started = manager.start(
        existing_token=None,
        captcha_token="captcha-ok",
        privacy_version="2026-08-10",
        privacy_accepted=True,
        client_ip="203.0.113.8",
    )

    assert manager.authenticate(started.cookie_value + "x") is None
    assert manager.authenticate(started.cookie_value) == started.identity


def test_unused_ip_grants_release_after_fifteen_minutes() -> None:
    store = InMemoryTrialIdentityStore()
    captcha = Captcha()
    manager = _manager(store, captcha)

    for _ in range(3):
        _start(manager)

    with pytest.raises(TrialIdentityLimitError):
        _start(
            _manager(
                store,
                captcha,
                now=NOW + timedelta(minutes=14, seconds=59),
            )
        )

    after_pending_window = _manager(
        store,
        captcha,
        now=NOW + timedelta(minutes=15),
    )
    result = _start(after_pending_window)

    assert result.created is True
    assert store.active_ip_grant_count(
        result.identity.ip_digest,
        now=NOW + timedelta(minutes=15),
    ) == 1


def test_activated_ip_grant_lasts_thirty_days_without_sliding() -> None:
    store = InMemoryTrialIdentityStore()
    captcha = Captcha()
    started = _start(_manager(store, captcha))
    activated_at = NOW + timedelta(minutes=5)

    _manager(store, captcha, now=activated_at).activate_for_consult(
        started.identity
    )
    _manager(
        store,
        captcha,
        now=activated_at + timedelta(days=1),
    ).activate_for_consult(started.identity)

    assert store.active_ip_grant_count(
        started.identity.ip_digest,
        now=activated_at + timedelta(days=30) - timedelta(seconds=1),
    ) == 1
    assert store.active_ip_grant_count(
        started.identity.ip_digest,
        now=activated_at + timedelta(days=30),
    ) == 0


def test_expired_pending_cookie_still_obeys_three_identity_limit() -> None:
    store = InMemoryTrialIdentityStore()
    captcha = Captcha()
    original = _start(_manager(store, captcha))
    active_manager = _manager(
        store,
        captcha,
        now=NOW + timedelta(minutes=16),
    )
    active_identities = [_start(active_manager).identity for _ in range(3)]
    for identity in active_identities:
        active_manager.activate_for_consult(identity)

    retry_manager = _manager(
        store,
        captcha,
        now=NOW + timedelta(minutes=17),
    )
    assert (
        retry_manager.authenticate(original.cookie_value)
        == original.identity
    )
    with pytest.raises(TrialIdentityLimitError):
        retry_manager.activate_for_consult(original.identity)


def test_concurrent_activation_never_exceeds_three_identities() -> None:
    store = InMemoryTrialIdentityStore()
    captcha = Captcha()
    identities = [
        _start(_manager(store, captcha)).identity
        for _ in range(3)
    ]
    second_batch_manager = _manager(
        store,
        captcha,
        now=NOW + timedelta(minutes=16),
    )
    identities.extend(
        _start(second_batch_manager).identity
        for _ in range(3)
    )
    activation_manager = _manager(
        store,
        captcha,
        now=NOW + timedelta(minutes=32),
    )
    barrier = Barrier(len(identities))

    def activate(identity: object) -> str:
        barrier.wait(timeout=5)
        try:
            activation_manager.activate_for_consult(identity)  # type: ignore[arg-type]
        except TrialIdentityLimitError:
            return "limited"
        return "activated"

    with ThreadPoolExecutor(max_workers=len(identities)) as executor:
        outcomes = list(executor.map(activate, identities))

    assert outcomes.count("activated") == 3
    assert outcomes.count("limited") == 3
    assert store.active_ip_grant_count(
        identities[0].ip_digest,
        now=NOW + timedelta(minutes=32),
    ) == 3
