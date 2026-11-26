"""The migration that encrypts stored MFA secrets (issue #115, ADR 0046).

Each test runs inside the test transaction, which rolls back, so the
schema it migrates back and forward ends at the latest migration again.
"""

from collections.abc import Iterator

import pytest
from allauth.mfa.models import Authenticator
from django.core.exceptions import ImproperlyConfigured
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import override_settings

from apps.accounts.tests.factories import UserFactory, enroll_totp
from apps.encryption.keys import (
    MISSING_KEYS_MESSAGE,
    decrypt_text,
    encrypt_text,
    is_token,
)

pytestmark = pytest.mark.django_db

PLAIN = [("accounts", "0002_agreement_acceptance")]
ENCRYPTED = [("accounts", "0003_encrypt_mfa_secrets")]
MIGRATED = ["11111111", "22222222"]


@pytest.fixture
def executor() -> Iterator[MigrationExecutor]:
    migrator = MigrationExecutor(connection)
    yield migrator
    migrator.loader.build_graph()
    migrator.migrate(migrator.loader.graph.leaf_nodes())


def _migrate(executor: MigrationExecutor, target: list[tuple[str, str]]) -> None:
    executor.loader.build_graph()
    executor.migrate(target)


def _immediate() -> None:
    with connection.cursor() as cursor:
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")


def _data(pk: int) -> dict[str, object]:
    # The plain JSON, read through the base model, which never decrypts.
    data: dict[str, object] = Authenticator.objects.get(pk=pk).data
    return data


def _migrated_codes_account() -> Authenticator:
    # Recovery codes carried over from an older store, as allauth keeps them.
    return Authenticator.objects.create(
        user=UserFactory.create(),
        type=Authenticator.Type.RECOVERY_CODES,
        data={
            "migrated_codes": [encrypt_text(code) for code in MIGRATED],
            "used_mask": 0,
        },
    )


def test_back_decrypts_and_forward_encrypts_again(
    executor: MigrationExecutor,
) -> None:
    account = UserFactory.create()
    enrollment = enroll_totp(account)
    totp = Authenticator.objects.get(user=account, type="totp")
    codes = Authenticator.objects.get(user=account, type="recovery_codes")
    seed = decrypt_text(str(codes.data["seed"]))
    migrated = _migrated_codes_account()
    _immediate()

    _migrate(executor, PLAIN)

    assert _data(totp.pk)["secret"] == enrollment.secret
    assert _data(codes.pk)["seed"] == seed
    assert _data(migrated.pk)["migrated_codes"] == MIGRATED

    _migrate(executor, ENCRYPTED)

    assert decrypt_text(str(_data(totp.pk)["secret"])) == enrollment.secret
    assert decrypt_text(str(_data(codes.pk)["seed"])) == seed
    stored = _data(migrated.pk)["migrated_codes"]
    assert isinstance(stored, list)
    assert [decrypt_text(code) for code in stored] == MIGRATED
    assert _data(codes.pk)["used_mask"] == 0


def test_forward_leaves_a_value_already_encrypted(
    executor: MigrationExecutor,
) -> None:
    account = UserFactory.create()
    enroll_totp(account)
    _immediate()
    _migrate(executor, PLAIN)
    totp = Authenticator.objects.get(user=account, type="totp")
    token = encrypt_text("JBSWY3DPEHPK3PXP")
    totp.data = {"secret": token}
    totp.save(update_fields=["data"])

    _migrate(executor, ENCRYPTED)

    assert _data(totp.pk) == {"secret": token}
    seed = _data(Authenticator.objects.get(user=account, type="recovery_codes").pk)
    assert is_token(str(seed["seed"]))


def test_no_key_is_needed_without_authenticators(
    executor: MigrationExecutor,
) -> None:
    _migrate(executor, PLAIN)

    with override_settings(FIELD_ENCRYPTION_KEYS=[]):
        _migrate(executor, ENCRYPTED)
        _migrate(executor, PLAIN)


def test_authenticators_without_a_key_stop_the_migration_with_fixed_text(
    executor: MigrationExecutor,
) -> None:
    enroll_totp(UserFactory.create())
    _immediate()
    _migrate(executor, PLAIN)

    with (
        override_settings(FIELD_ENCRYPTION_KEYS=[]),
        pytest.raises(ImproperlyConfigured) as caught,
    ):
        _migrate(executor, ENCRYPTED)

    assert str(caught.value) == MISSING_KEYS_MESSAGE
