"""Factories for Accounts. Made-up data only, never real player data.

``enroll_totp`` gives an Account a TOTP authenticator and recovery codes
(ADR 0043), as allauth stores them, with a freshly generated secret.
``UserFactory`` enrolls a staff or superuser Account by default, since
the admin and the API refuse one without a method; pass ``mfa=False`` to
leave it out, or ``mfa=True`` to enroll any Account.
"""

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from allauth.mfa import app_settings as mfa_settings
from allauth.mfa.recovery_codes.internal.auth import RecoveryCodes
from allauth.mfa.totp.internal.auth import (
    TOTP,
    format_hotp_value,
    generate_totp_secret,
    hotp_value,
)
from django.conf import settings
from factory.declarations import PostGeneration, Sequence
from factory.django import DjangoModelFactory
from faker import Faker

if TYPE_CHECKING:
    # The django-stubs plugin resolves this alias to AUTH_USER_MODEL, so the
    # concrete User model is never imported (CLAUDE.md ordering rules).
    from django.contrib.auth.models import _User

# A made-up password, used when a test does not pass one.
DEFAULT_PASSWORD = "made-up-test-password"

_faker = Faker()


@dataclass(frozen=True)
class Enrollment:
    """What an enrolled Account's authenticator app and printout hold."""

    secret: str
    recovery_codes: list[str]


def totp_code(secret: str, offset_steps: int = 0) -> str:
    """The TOTP code for ``secret`` now, or ``offset_steps`` periods away."""
    counter = int(time.time()) // mfa_settings.TOTP_PERIOD + offset_steps
    return str(format_hotp_value(hotp_value(secret, counter)))


def enroll_totp(account: "_User") -> Enrollment:
    """Give ``account`` a TOTP authenticator and recovery codes.

    Goes through allauth's own activation, as its flows would, with no
    request, no Audit Event, and no mail.
    """
    secret = generate_totp_secret()
    TOTP.activate(account, secret)
    codes: list[str] = RecoveryCodes.activate(account).get_unused_codes()
    return Enrollment(secret=secret, recovery_codes=codes)


def _enroll(account: "_User", create: bool, extracted: object, **kwargs: Any) -> None:
    """UserFactory's ``mfa`` flag: enroll when True, or by default for staff."""
    if not create:
        return
    wanted = (
        bool(extracted)
        if extracted is not None
        else account.is_staff or account.is_superuser
    )
    if wanted:
        enroll_totp(account)


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
        # ``mfa`` writes its own rows; the Account needs no second save.
        skip_postgeneration_save = True

    # factory_boy's declarations carry no type hints.
    email = Sequence(_email)  # type: ignore[no-untyped-call]
    password = DEFAULT_PASSWORD
    mfa = PostGeneration(_enroll)  # type: ignore[no-untyped-call]

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
