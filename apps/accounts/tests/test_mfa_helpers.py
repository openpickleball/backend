"""OPEN's MFA helpers (issue #99, ADR 0043): who has a method, who needs one,
and whether a session passed the second factor."""

from importlib import import_module
from typing import TYPE_CHECKING, Any

import pytest
from allauth.account.models import EmailAddress
from allauth.mfa.models import Authenticator
from allauth.mfa.recovery_codes.internal.auth import RecoveryCodes
from django.conf import settings
from django.contrib.auth.models import AnonymousUser, Group, Permission
from django.contrib.sessions.backends.base import SessionBase
from django.core.cache import cache
from django.test import Client

from apps.accounts.mfa import (
    MFAStatus,
    accounts_with_mfa,
    has_mfa,
    lacks_required_mfa,
    last_authenticated_at,
    last_mfa_at,
    mfa_required,
    mfa_status,
    session_passed_mfa,
)
from apps.accounts.tests.factories import UserFactory, enroll_totp, totp_code
from apps.accounts.tests.headless import APP, PASSWORD, login, post
from apps.matches.permissions import MODERATOR_GROUP
from apps.partners.tests.factories import PartnerApplicationFactory

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    cache.clear()


@pytest.fixture(autouse=True)
def _one_step_of_clock_drift(settings: Any) -> None:
    """Accept the neighbouring TOTP step, so a login never fails on a
    period boundary between computing a code and posting it."""
    settings.MFA_TOTP_TOLERANCE = 1


def test_has_mfa_is_true_with_totp() -> None:
    account = UserFactory.create()
    enroll_totp(account)

    assert has_mfa(account)


def test_has_mfa_is_false_without_a_method() -> None:
    assert not has_mfa(UserFactory.create())


def test_recovery_codes_alone_are_not_mfa() -> None:
    account = UserFactory.create()
    RecoveryCodes.activate(account)

    assert not has_mfa(account)


def test_has_mfa_is_false_for_unsaved_and_anonymous_actors() -> None:
    assert not has_mfa(UserFactory.build())
    assert not has_mfa(AnonymousUser())
    assert not has_mfa(None)


def test_has_mfa_reads_the_database() -> None:
    account = UserFactory.create()
    enroll_totp(account)
    Authenticator.objects.filter(user=account).delete()

    assert not has_mfa(account)


def test_accounts_with_mfa_lists_only_accounts_with_a_method() -> None:
    enrolled = UserFactory.create()
    enroll_totp(enrolled)
    codes_only = UserFactory.create()
    RecoveryCodes.activate(codes_only)
    UserFactory.create()

    model = type(enrolled)
    assert list(model.objects.filter(pk__in=accounts_with_mfa())) == [enrolled]


def test_a_player_does_not_need_mfa() -> None:
    assert not mfa_required(UserFactory.create())


@pytest.mark.parametrize("field", ["is_staff", "is_superuser"])
def test_staff_and_superusers_need_mfa(field: str) -> None:
    assert mfa_required(UserFactory.create(**{field: True}, mfa=False))


def test_a_moderator_needs_mfa() -> None:
    account = UserFactory.create()
    account.groups.add(Group.objects.get(name=MODERATOR_GROUP))

    assert mfa_required(account)


def test_an_account_with_the_moderate_permission_itself_needs_mfa() -> None:
    account = UserFactory.create()
    account.user_permissions.add(
        Permission.objects.get(
            content_type__app_label="matches", codename="moderate_match"
        )
    )

    assert mfa_required(account)


def test_an_inactive_moderator_does_not_count_as_one() -> None:
    account = UserFactory.create(is_active=False)
    account.groups.add(Group.objects.get(name=MODERATOR_GROUP))

    assert not mfa_required(account)


def test_the_owner_of_a_production_application_needs_mfa() -> None:
    account = UserFactory.create()
    PartnerApplicationFactory.create(user=account, is_sandbox=False)

    assert mfa_required(account)


def test_the_owner_of_a_sandbox_application_does_not() -> None:
    account = UserFactory.create()
    PartnerApplicationFactory.create(user=account, is_sandbox=True)

    assert not mfa_required(account)


def test_mfa_required_reads_the_database() -> None:
    account = UserFactory.create()
    type(account).objects.filter(pk=account.pk).update(is_staff=True)

    assert mfa_required(account)


def test_mfa_required_is_false_for_unsaved_and_anonymous_actors() -> None:
    assert not mfa_required(UserFactory.build(is_staff=True))
    assert not mfa_required(AnonymousUser())


def _token_session(token: str) -> SessionBase:
    store: SessionBase = import_module(settings.SESSION_ENGINE).SessionStore(
        session_key=token
    )
    return store


def _verified_account() -> "_User":
    account = UserFactory.create(password=PASSWORD)
    EmailAddress.objects.create(
        user=account, email=account.email, verified=True, primary=True
    )
    return account


def test_session_passed_mfa_after_a_totp_login() -> None:
    account = _verified_account()
    secret = enroll_totp(account).secret
    client = Client()
    token = login(client, APP, account.email).json()["meta"]["session_token"]

    response = post(
        client,
        f"{APP}/auth/2fa/authenticate",
        {"code": totp_code(secret)},
        **{"X-Session-Token": token},
    )

    assert response.status_code == 200
    # The login cycles the session key, so the token changes.
    token = response.json()["meta"]["session_token"]
    assert session_passed_mfa(_token_session(token), account)


def test_session_did_not_pass_mfa_after_a_password_login() -> None:
    account = _verified_account()
    client = Client()
    response = login(client, APP, account.email)

    assert response.status_code == 200
    token = response.json()["meta"]["session_token"]
    assert not session_passed_mfa(_token_session(token), account)


def test_session_passed_mfa_is_bound_to_its_account() -> None:
    account = _verified_account()
    secret = enroll_totp(account).secret
    client = Client()
    token = login(client, APP, account.email).json()["meta"]["session_token"]
    token = post(
        client,
        f"{APP}/auth/2fa/authenticate",
        {"code": totp_code(secret)},
        **{"X-Session-Token": token},
    ).json()["meta"]["session_token"]

    assert session_passed_mfa(_token_session(token), account)
    assert not session_passed_mfa(_token_session(token), UserFactory.create())


@pytest.mark.parametrize("field", ["is_staff", "is_superuser"])
def test_factory_enrolls_staff_by_default(field: str) -> None:
    assert has_mfa(UserFactory.create(**{field: True}))


def test_factory_leaves_a_player_and_an_opted_out_staff_account_without_mfa() -> None:
    assert not has_mfa(UserFactory.create())
    assert not has_mfa(UserFactory.create(is_staff=True, mfa=False))
    assert has_mfa(UserFactory.create(mfa=True))


def test_lacks_required_mfa_only_when_required_and_missing() -> None:
    staff = UserFactory.create(is_staff=True, mfa=False)
    enrolled = UserFactory.create(is_staff=True)
    player = UserFactory.create()

    assert lacks_required_mfa(staff)
    assert not lacks_required_mfa(enrolled)
    assert not lacks_required_mfa(player)
    assert not lacks_required_mfa(AnonymousUser())


def test_lacks_required_mfa_is_one_query(
    django_assert_num_queries: pytest.FixtureRequest,
) -> None:
    staff = UserFactory.create(is_staff=True, mfa=False)

    with django_assert_num_queries(1):  # type: ignore[operator]
        assert lacks_required_mfa(staff)


def test_mfa_status_answers_both_questions_in_one_query(
    django_assert_num_queries: pytest.FixtureRequest,
) -> None:
    staff = UserFactory.create(is_staff=True, mfa=False)
    enrolled = UserFactory.create(mfa=True)

    with django_assert_num_queries(1):  # type: ignore[operator]
        assert mfa_status(staff) == MFAStatus(required=True, enrolled=False)
    assert mfa_status(enrolled) == MFAStatus(required=False, enrolled=True)
    assert mfa_status(UserFactory.create()) == MFAStatus(False, False)
    assert mfa_status(AnonymousUser()) == MFAStatus(False, False)


def _session_with(records: list[dict[str, Any]], account: "_User") -> SessionBase:
    from allauth.account.internal.flows.login import (
        _AUTHENTICATION_METHODS_USER_SESSION_KEY,
        AUTHENTICATION_METHODS_SESSION_KEY,
    )
    from allauth.account.internal.userkit import user_id_to_str

    store: SessionBase = import_module(settings.SESSION_ENGINE).SessionStore()
    store[AUTHENTICATION_METHODS_SESSION_KEY] = records
    store[_AUTHENTICATION_METHODS_USER_SESSION_KEY] = user_id_to_str(account)
    return store


def test_last_mfa_at_is_the_latest_mfa_step() -> None:
    account = UserFactory.create()
    session = _session_with(
        [
            {"method": "mfa", "at": 100.0, "type": "totp"},
            {"method": "password", "at": 500.0},
            {"method": "mfa", "at": 300.0, "type": "recovery_codes"},
            {"method": "mfa", "at": 200.0, "type": "totp"},
        ],
        account,
    )

    assert last_mfa_at(session, account) == 300.0


def test_last_mfa_at_is_none_without_an_mfa_step() -> None:
    account = UserFactory.create()
    session = _session_with([{"method": "password", "at": 500.0}], account)

    assert last_mfa_at(session, account) is None
    assert last_mfa_at(_session_with([], account), account) is None


def test_last_mfa_at_ignores_another_accounts_steps() -> None:
    account, other = UserFactory.create(), UserFactory.create()
    session = _session_with([{"method": "mfa", "at": 300.0}], other)

    assert last_mfa_at(session, account) is None


@pytest.mark.parametrize("at", [None, "300", True, float("nan")])
def test_last_mfa_at_ignores_a_step_without_a_time(at: object) -> None:
    account = UserFactory.create()
    session = _session_with([{"method": "mfa", "at": at}], account)

    assert last_mfa_at(session, account) is None


def test_last_authenticated_at_is_the_latest_step_of_any_kind() -> None:
    account = UserFactory.create()
    session = _session_with(
        [
            {"method": "mfa", "at": 100.0, "type": "totp"},
            {"method": "password", "at": 500.0, "reauthenticated": True},
            {"method": "password", "at": "600"},
        ],
        account,
    )

    assert last_authenticated_at(session, account) == 500.0


def test_last_authenticated_at_ignores_another_accounts_steps() -> None:
    account, other = UserFactory.create(), UserFactory.create()
    session = _session_with([{"method": "password", "at": 300.0}], other)

    assert last_authenticated_at(session, account) is None
