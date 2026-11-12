"""Factories for Accounts. Made-up data only, never real player data."""

from typing import Any

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AbstractUser
from factory.declarations import Sequence
from factory.django import DjangoModelFactory
from faker import Faker

# A made-up password, used when a test does not pass one.
DEFAULT_PASSWORD = "made-up-test-password"

_faker = Faker()


def _email(n: int) -> str:
    # Faker's safe_email is on a reserved example domain. The sequence
    # number keeps it unique across a run, which Faker alone does not.
    return f"player{n}.{_faker.safe_email()}"


# Typed as AbstractUser: other code never imports the concrete User model
# (CLAUDE.md ordering rules); Meta.model resolves it from settings.
class UserFactory(DjangoModelFactory[AbstractUser]):
    """An Account created through the manager, so its email is normalized
    and checked like any other (see CLAUDE.md, "Account emails").
    """

    class Meta:
        model = settings.AUTH_USER_MODEL

    # factory_boy's declarations carry no type hints.
    email = Sequence(_email)  # type: ignore[no-untyped-call]
    password = DEFAULT_PASSWORD

    @classmethod
    def _create(cls, model_class: type[Any], *args: Any, **kwargs: Any) -> AbstractUser:
        email = kwargs.pop("email")
        password = kwargs.pop("password")
        return get_user_model().objects.create_user(email, password, **kwargs)
