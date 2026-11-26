"""Encrypting and decrypting text with the FIELD_ENCRYPTION_KEYS keys."""

import pytest
from cryptography.fernet import Fernet
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from apps.encryption.keys import (
    MISSING_KEYS_MESSAGE,
    DecryptionError,
    decrypt_text,
    encrypt_text,
    is_token,
    rotate_text,
)


def _key() -> str:
    # Generated at test time: no key is ever committed.
    return Fernet.generate_key().decode()


def test_round_trip() -> None:
    token = encrypt_text("Made-up Name")
    assert token != "Made-up Name"
    assert "Made-up" not in token
    assert decrypt_text(token) == "Made-up Name"


def test_tokens_differ_for_the_same_text() -> None:
    # Fernet is not deterministic, so no lookup can compare tokens.
    assert encrypt_text("mens") != encrypt_text("mens")


def test_no_keys_refuses_to_encrypt_or_decrypt() -> None:
    token = encrypt_text("x")
    with override_settings(FIELD_ENCRYPTION_KEYS=[]):
        with pytest.raises(ImproperlyConfigured) as error:
            encrypt_text("x")
        assert str(error.value) == MISSING_KEYS_MESSAGE
        with pytest.raises(ImproperlyConfigured):
            decrypt_text(token)
    assert "FIELD_ENCRYPTION_KEYS" in MISSING_KEYS_MESSAGE


def test_a_token_from_another_key_fails_with_fixed_text() -> None:
    with override_settings(FIELD_ENCRYPTION_KEYS=[_key()]):
        token = encrypt_text("Made-up Secret")
    with pytest.raises(DecryptionError) as error:
        decrypt_text(token)
    assert "Made-up" not in str(error.value)
    assert token not in str(error.value)


def test_plain_text_is_not_a_token() -> None:
    assert not is_token("mens")
    assert not is_token("1990")
    assert not is_token("")
    assert is_token(encrypt_text("mens"))
    with pytest.raises(DecryptionError):
        decrypt_text("mens")


def test_older_keys_still_decrypt_and_rotation_uses_the_newest() -> None:
    old, new = _key(), _key()
    with override_settings(FIELD_ENCRYPTION_KEYS=[old]):
        token = encrypt_text("Made-up Value")
    with override_settings(FIELD_ENCRYPTION_KEYS=[new, old]):
        assert decrypt_text(token) == "Made-up Value"
        rotated = rotate_text(token)
    with override_settings(FIELD_ENCRYPTION_KEYS=[new]):
        assert decrypt_text(rotated) == "Made-up Value"
        with pytest.raises(DecryptionError):
            decrypt_text(token)


def test_rotate_refuses_a_value_no_key_decrypts() -> None:
    with pytest.raises(DecryptionError):
        rotate_text("not-a-token")
