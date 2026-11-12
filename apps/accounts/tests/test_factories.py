"""UserFactory makes Accounts through the manager, with made-up emails."""

import pytest
from django.contrib.auth import get_user_model

from apps.accounts.tests.factories import DEFAULT_PASSWORD, UserFactory
from apps.accounts.validators import (
    normalize_email_address,
    validate_normalized_email,
)

pytestmark = pytest.mark.django_db

EXAMPLE_DOMAINS = {"example.com", "example.net", "example.org"}


def test_email_is_normalized_ascii_on_an_example_domain() -> None:
    account = UserFactory.create()

    assert account.email.isascii()
    assert account.email == normalize_email_address(account.email)
    assert account.email.rsplit("@", 1)[1] in EXAMPLE_DOMAINS
    validate_normalized_email(account.email)


def test_emails_are_unique_across_creates() -> None:
    accounts = UserFactory.create_batch(50)

    assert len({account.email for account in accounts}) == 50


def test_creates_through_the_manager() -> None:
    account = UserFactory.create(email="  Made.Up@Example.COM ")

    assert account.email == "made.up@example.com"


def test_default_password_is_usable() -> None:
    account = UserFactory.create()

    assert account.check_password(DEFAULT_PASSWORD)


def test_password_override_is_usable() -> None:
    account = UserFactory.create(password="another-made-up-password")

    assert account.check_password("another-made-up-password")
    assert not account.check_password(DEFAULT_PASSWORD)


def test_defaults_to_a_regular_account() -> None:
    account = UserFactory.create()

    assert account.is_active
    assert not account.is_staff
    assert not account.is_superuser


def test_accepts_field_overrides() -> None:
    account = UserFactory.create(is_staff=True)

    assert account.is_staff


def test_stored_account_is_found_by_natural_key() -> None:
    account = UserFactory.create()

    found = get_user_model().objects.get_by_natural_key(account.email.upper())

    assert found.pk == account.pk


def test_build_normalizes_email_without_saving() -> None:
    account = UserFactory.build(email="  Made.Up@Example.COM ")

    assert account.email == "made.up@example.com"
    assert account.pk is None
    assert not get_user_model().objects.filter(email=account.email).exists()


def test_build_sets_a_usable_password() -> None:
    account = UserFactory.build()

    assert account.check_password(DEFAULT_PASSWORD)
    assert account.password != DEFAULT_PASSWORD


class _RecordingManager:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.created = object()

    def create_user(
        self, email: str, password: str | None = None, **extra: object
    ) -> object:
        self.calls.append((email, password, extra))
        return self.created


def test_create_uses_the_factory_model_manager() -> None:
    # A subclass factory with another Meta.model must create that model,
    # not the default user model.
    manager = _RecordingManager()
    model = type("OtherAccount", (), {"objects": manager, "_default_manager": manager})

    created = UserFactory._create(
        model, email="made.up@example.com", password="pw", is_staff=True
    )

    assert created is manager.created
    assert manager.calls == [("made.up@example.com", "pw", {"is_staff": True})]
