"""The MFA secrets migration's reverse refuses a value it cannot decrypt
(issue #115, ADR 0046).

With FIELD_ENCRYPTION_KEYS missing the key a secret was written with,
the reverse must stop with fixed text naming the model and row id, never
the value, and change nothing, instead of leaving ciphertext that
allauth's default adapter would then use as the secret.
"""

from collections.abc import Iterator

import pytest
from allauth.mfa.models import Authenticator
from cryptography.fernet import Fernet
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import override_settings

from apps.accounts.tests.factories import UserFactory, enroll_totp

pytestmark = pytest.mark.django_db

PLAIN = [("accounts", "0002_agreement_acceptance")]


@pytest.fixture
def executor() -> Iterator[MigrationExecutor]:
    migrator = MigrationExecutor(connection)
    yield migrator
    migrator.loader.build_graph()
    migrator.migrate(migrator.loader.graph.leaf_nodes())


def _data() -> list[object]:
    return list(Authenticator.objects.order_by("pk").values_list("data", flat=True))


def test_reverse_with_another_key_raises_and_changes_nothing(
    executor: MigrationExecutor,
) -> None:
    enroll_totp(UserFactory.create())
    totp = Authenticator.objects.get(type="totp")
    with connection.cursor() as cursor:
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
    before = _data()

    executor.loader.build_graph()
    with (
        override_settings(FIELD_ENCRYPTION_KEYS=[Fernet.generate_key().decode()]),
        pytest.raises(RuntimeError) as caught,
    ):
        executor.migrate(PLAIN)

    message = str(caught.value)
    assert f"mfa.Authenticator row {totp.pk}" in message
    assert str(totp.data["secret"]) not in message
    assert "gAAAA" not in message
    assert _data() == before
