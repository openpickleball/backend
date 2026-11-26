"""The encrypted model fields, tested on the field objects alone."""

from typing import Any

import pytest
from django.db import connection

from apps.encryption.fields import (
    EncryptedCharField,
    EncryptedPositiveSmallIntegerField,
)
from apps.encryption.keys import decrypt_text, is_token


def _char() -> EncryptedCharField[Any, Any]:
    field = EncryptedCharField(max_length=8)
    field.set_attributes_from_name("division")
    return field


def _small() -> EncryptedPositiveSmallIntegerField[Any, Any]:
    field = EncryptedPositiveSmallIntegerField()
    field.set_attributes_from_name("year")
    return field


def test_columns_are_text_with_no_check() -> None:
    for field in (_char(), _small()):
        assert field.db_type(connection) == "text"
        assert field.db_check(connection) is None


def test_saves_a_token_and_reads_back_the_python_value() -> None:
    char, small = _char(), _small()
    char_token = char.get_db_prep_save("womens", connection)
    small_token = small.get_db_prep_save(1990, connection)
    assert is_token(char_token)
    assert is_token(small_token)
    assert "womens" not in char_token
    assert "1990" not in small_token
    assert decrypt_text(small_token) == "1990"
    assert char.from_db_value(char_token, None, connection) == "womens"
    assert small.from_db_value(small_token, None, connection) == 1990


def test_none_stays_none() -> None:
    for field in (_char(), _small()):
        assert field.get_db_prep_save(None, connection) is None
        assert field.from_db_value(None, None, connection) is None


@pytest.mark.parametrize(
    "name", ["exact", "iexact", "in", "contains", "gt", "lt", "startswith", "regex"]
)
def test_only_isnull_lookups_are_allowed(name: str) -> None:
    for field in (_char(), _small()):
        assert field.get_lookup(name) is None
        assert field.get_lookup("isnull") is not None


def test_no_transforms() -> None:
    for field in (_char(), _small()):
        assert field.get_transform("lower") is None
        assert field.get_transform("year") is None


def test_deconstructs_to_its_own_path() -> None:
    _, path, _, kwargs = _char().deconstruct()
    assert path == "apps.encryption.fields.EncryptedCharField"
    assert kwargs == {"max_length": 8}
    _, path, _, _ = _small().deconstruct()
    assert path == "apps.encryption.fields.EncryptedPositiveSmallIntegerField"
