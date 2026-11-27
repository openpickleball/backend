"""``manage.py reencrypt_fields`` rewrites every encrypted value with the
newest key (issue #115, ADR 0046), so an old key can then be removed.
"""

from io import StringIO

import pytest
from allauth.mfa.models import Authenticator
from cryptography.fernet import Fernet, InvalidToken
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import override_settings

from apps.accounts.tests.factories import UserFactory, enroll_totp, totp_secret
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db

LEGAL_NAME = "Vexmoor Made-up Legal"


def _key() -> str:
    # Generated at test time: no key is ever committed.
    return Fernet.generate_key().decode()


def _raw_tokens() -> list[str]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT legal_name, birth_year, birth_month, gender_division "
            "FROM players_player"
        )
        player_values = [value for row in cursor.fetchall() for value in row]
        cursor.execute("SELECT data->>'secret' FROM mfa_authenticator")
        secrets = [row[0] for row in cursor.fetchall() if row[0] is not None]
    return [*player_values, *secrets]


def _decrypts_with(key: str, token: str) -> bool:
    try:
        Fernet(key).decrypt(token.encode())
    except InvalidToken:
        return False
    return True


def test_rotation_moves_every_value_to_the_newest_key() -> None:
    old, new = _key(), _key()
    with override_settings(FIELD_ENCRYPTION_KEYS=[old]):
        player = PlayerFactory.create(legal_name=LEGAL_NAME, birth_month=4)
        account = UserFactory.create()
        enrollment = enroll_totp(account)
        expected = Player.objects.values_list(
            "legal_name", "birth_year", "birth_month", "gender_division"
        ).get(pk=player.pk)
        seed = Authenticator.objects.get(user=account, type="recovery_codes")

    out = StringIO()
    with override_settings(FIELD_ENCRYPTION_KEYS=[new, old]):
        call_command("reencrypt_fields", stdout=out)

    assert out.getvalue().splitlines() == [
        "players.Player: 1 rows re-encrypted.",
        "exports.DataExport: 0 rows re-encrypted.",
        "mfa.Authenticator: 2 rows re-encrypted.",
    ]
    for token in _raw_tokens():
        assert _decrypts_with(new, token)
        assert not _decrypts_with(old, token)
    with override_settings(FIELD_ENCRYPTION_KEYS=[new]):
        assert (
            Player.objects.values_list(
                "legal_name", "birth_year", "birth_month", "gender_division"
            ).get(pk=player.pk)
            == expected
        )
        assert totp_secret(account) == enrollment.secret
        stored = Authenticator.objects.get(pk=seed.pk).data["seed"]
        assert _decrypts_with(new, stored)


def test_output_holds_counts_only() -> None:
    PlayerFactory.create(legal_name=LEGAL_NAME)
    out = StringIO()

    call_command("reencrypt_fields", stdout=out)

    assert LEGAL_NAME not in out.getvalue()
    assert out.getvalue().splitlines() == [
        "players.Player: 1 rows re-encrypted.",
        "exports.DataExport: 0 rows re-encrypted.",
        "mfa.Authenticator: 0 rows re-encrypted.",
    ]


def test_a_player_value_no_key_decrypts_fails_naming_the_row_only() -> None:
    with override_settings(FIELD_ENCRYPTION_KEYS=[_key()]):
        player = PlayerFactory.create(legal_name=LEGAL_NAME)

    with (
        override_settings(FIELD_ENCRYPTION_KEYS=[_key()]),
        pytest.raises(CommandError) as caught,
    ):
        call_command("reencrypt_fields", stdout=StringIO())

    message = str(caught.value)
    assert f"players.Player row {player.pk}" in message
    assert LEGAL_NAME not in message
    assert "gAAAA" not in message


def test_an_authenticator_no_key_decrypts_fails_naming_the_row_only() -> None:
    with override_settings(FIELD_ENCRYPTION_KEYS=[_key()]):
        enroll_totp(UserFactory.create())
    totp = Authenticator.objects.get(type="totp")

    with (
        override_settings(FIELD_ENCRYPTION_KEYS=[_key()]),
        pytest.raises(CommandError) as caught,
    ):
        call_command("reencrypt_fields", stdout=StringIO())

    message = str(caught.value)
    assert "mfa.Authenticator row" in message
    assert str(totp.data["secret"]) not in message
    assert "gAAAA" not in message
