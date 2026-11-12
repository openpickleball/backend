"""Factories for Accounts. Made-up data only, never real player data."""

from typing import TYPE_CHECKING, Any

from django.conf import settings
from factory.declarations import Sequence
from factory.django import DjangoModelFactory
from faker import Faker

if TYPE_CHECKING:
    # The django-stubs plugin resolves this alias to AUTH_USER_MODEL, so the
    # concrete User model is never imported (CLAUDE.md ordering rules).
    from django.contrib.auth.models import _User

# A made-up password, used when a test does not pass one.
DEFAULT_PASSWORD = "made-up-test-password"

_faker = Faker()


def _email(n: int) -> str:
    # Faker's safe_email is on a reserved example domain. The sequence
    # number keeps it unique across a run, which Faker alone does not.
    return f"player{n}.{_faker.safe_email()}"


class UserFactory(DjangoModelFactory["_User"]):
    """An Account created through the manager, so its email is normalized
    and checked like any other (see CLAUDE.md, "Account emails").
    """

    class Meta:
        model = settings.AUTH_USER_MODEL

    # factory_boy's declarations carry no type hints.
    email = Sequence(_email)  # type: ignore[no-untyped-call]
    password = DEFAULT_PASSWORD

    @classmethod
    def _build(cls, model_class: type[Any], *args: Any, **kwargs: Any) -> "_User":
        """An unsaved Account, normalized and hashed as the manager would."""
        email = model_class._default_manager.normalize_email(kwargs.pop("email"))
        password = kwargs.pop("password")
        account: _User = model_class(email=email, **kwargs)
        account.set_password(password)
        return account

    @classmethod
    def _create(cls, model_class: type[Any], *args: Any, **kwargs: Any) -> "_User":
        """A saved Account, created through the model's manager."""
        email = kwargs.pop("email")
        password = kwargs.pop("password")
        account: _User = model_class._default_manager.create_user(
            email, password, **kwargs
        )
        return account
