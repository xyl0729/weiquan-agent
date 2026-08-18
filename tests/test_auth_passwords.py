from __future__ import annotations

import pytest
from argon2 import PasswordHasher
from argon2.low_level import Type

from app.auth.passwords import PasswordManager, PasswordPolicyError


def test_password_policy_accepts_only_8_to_128_characters() -> None:
    manager = PasswordManager()

    with pytest.raises(PasswordPolicyError):
        manager.hash("a" * 7)
    with pytest.raises(PasswordPolicyError):
        manager.hash("a" * 129)

    assert manager.verify("a" * 8, manager.hash("a" * 8)).valid
    assert manager.verify("a" * 128, manager.hash("a" * 128)).valid


def test_passwords_use_argon2id_and_never_embed_plaintext() -> None:
    manager = PasswordManager()
    password = "correct horse battery staple"

    encoded = manager.hash(password)

    assert encoded.startswith("$argon2id$")
    assert password not in encoded
    assert manager.verify(password, encoded).valid
    assert not manager.verify("wrong-password", encoded).valid


def test_successful_verification_can_upgrade_old_argon2_parameters() -> None:
    old_hasher = PasswordHasher(
        time_cost=1,
        memory_cost=8192,
        parallelism=1,
        hash_len=16,
        salt_len=16,
        type=Type.ID,
    )
    manager = PasswordManager()
    password = "upgrade-this-password"
    old_hash = old_hasher.hash(password)

    result = manager.verify(password, old_hash)

    assert result.valid
    assert result.upgraded_hash is not None
    assert result.upgraded_hash.startswith("$argon2id$")
    assert manager.verify(password, result.upgraded_hash).valid
