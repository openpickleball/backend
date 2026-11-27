"""Nobody changes an Account that Account deletion cleared (#344, ADR 0048).

A cleared Account is kept inactive, with a placeholder email and an
unusable password. In the Account admin it is read-only for everyone,
superusers included: the change form and the set-password view refuse
it with 403 and change nothing, even when the page was loaded before the
deletion committed. No admin action other than the delete (which already
refuses a cleared Account) is offered, and neither staff nor sign-up can
use a placeholder address to make an Account look cleared. Made-up data
only.
"""

import html
from typing import TYPE_CHECKING, Any

import pytest
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.cache import cache
from django.test import Client, RequestFactory
from django.urls import reverse

from apps.accounts import admin as account_admin
from apps.accounts.admin import (
    PLACEHOLDER_ACTIVE_MESSAGE,
    PLACEHOLDER_EMAIL_KEPT_MESSAGE,
    UserAdmin,
)
from apps.accounts.deletion import (
    DELETED_EMAIL_DOMAIN,
    PLACEHOLDER_EMAIL_MESSAGE,
    delete_account,
    is_deleted_email,
)
from apps.accounts.tests.factories import UserFactory
from apps.accounts.tests.headless import (
    APP,
    BROWSER,
    PASSWORD,
    SIGNUP_DETAILS,
    browser_client,
    csrf_header,
    post,
    signup,
)
from apps.matches.models import Team
from apps.players.tests.factories import PlayerFactory
from apps.ratings.tests.builders import saved_match

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

pytestmark = pytest.mark.django_db

NEW_PASSWORD = "made-up-Passw0rd-e5f6"


def _manager() -> "_User":
    """Staff that may view, add, change, and delete Accounts, not a superuser."""
    account: _User = UserFactory.create(is_staff=True)
    account.user_permissions.set(
        Permission.objects.filter(
            codename__in=["view_user", "add_user", "change_user", "delete_user"]
        )
    )
    return account


def _superuser() -> "_User":
    account: _User = UserFactory.create(is_staff=True, is_superuser=True)
    return account


STAFF = [_manager, _superuser]


def _played_account() -> "_User":
    """An Account whose Player is in the Ledger, so deletion keeps both."""
    player = PlayerFactory.create()
    others = [PlayerFactory.create() for _ in range(3)]
    saved_match(players={Team.A: [player, others[0]], Team.B: others[1:]})
    account: _User = player.account
    return account


def _clear(account: "_User") -> "_User":
    result = delete_account(account, actor=account)
    assert result.account_kept
    stored: _User = get_user_model()._default_manager.get(pk=account.pk)
    assert not stored.is_active
    assert is_deleted_email(stored.email)
    return stored


def _change_data(email: str) -> dict[str, Any]:
    """The change form's fields, with the Account set active."""
    return {"email": email, "is_active": "on"}


def _text(response: Any) -> str:
    """The page's text, with HTML escapes undone."""
    return html.unescape(response.content.decode())


def _snapshot(account: "_User") -> tuple[Any, ...]:
    stored = get_user_model()._default_manager.get(pk=account.pk)
    return (
        stored.email,
        stored.is_active,
        stored.is_staff,
        stored.is_superuser,
        stored.password,
        stored.has_usable_password(),
    )


@pytest.mark.parametrize("make", STAFF)
def test_nobody_can_reactivate_a_cleared_account(client: Client, make: Any) -> None:
    client.force_login(make())
    cleared = _clear(_played_account())
    before = _snapshot(cleared)

    response = client.post(
        reverse("admin:accounts_user_change", args=[cleared.pk]),
        _change_data("made-up-back@example.com"),
    )

    assert response.status_code == 403
    assert _snapshot(cleared) == before


@pytest.mark.parametrize("make", STAFF)
def test_nobody_can_set_a_password_on_a_cleared_account(
    client: Client, make: Any
) -> None:
    client.force_login(make())
    cleared = _clear(_played_account())
    before = _snapshot(cleared)

    response = client.post(
        reverse("admin:auth_user_password_change", args=[cleared.pk]),
        {
            "usable_password": "true",
            "password1": NEW_PASSWORD,
            "password2": NEW_PASSWORD,
        },
    )

    assert response.status_code == 403
    assert _snapshot(cleared) == before


@pytest.mark.parametrize("make", STAFF)
def test_a_cleared_account_shows_read_only(client: Client, make: Any) -> None:
    client.force_login(make())
    cleared = _clear(_played_account())

    response = client.get(reverse("admin:accounts_user_change", args=[cleared.pk]))

    assert response.status_code == 200
    assert b'name="is_active"' not in response.content
    assert b'name="email"' not in response.content
    assert b'name="_save"' not in response.content


@pytest.mark.parametrize("make", STAFF)
def test_a_form_loaded_before_the_deletion_cannot_undo_it(
    client: Client, make: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The change form was loaded, its object read, and its permission
    checked, before the deletion committed: the save checks the stored
    row again under a lock."""
    client.force_login(make())
    account = _played_account()
    stale = get_user_model()._default_manager.get(pk=account.pk)
    old_email = stale.email
    cleared = _clear(account)
    before = _snapshot(cleared)
    monkeypatch.setattr(UserAdmin, "get_object", lambda *args, **kwargs: stale)
    # The view's own permission checks ran before the deletion committed.
    monkeypatch.setattr(account_admin, "_stored_is_cleared", lambda _: False)

    response = client.post(
        reverse("admin:accounts_user_change", args=[cleared.pk]),
        _change_data(old_email),
    )

    assert response.status_code == 403
    assert _snapshot(cleared) == before


@pytest.mark.parametrize("make", STAFF)
def test_a_form_loaded_before_a_hard_delete_cannot_insert_the_account_again(
    client: Client, make: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An Account nothing protects is hard-deleted: a stale save would
    insert it again, with its old email and password hash."""
    client.force_login(make())
    account = UserFactory.create()
    stale = get_user_model()._default_manager.get(pk=account.pk)
    assert delete_account(account, actor=account).account_kept is False
    monkeypatch.setattr(UserAdmin, "get_object", lambda *args, **kwargs: stale)

    changed = client.post(
        reverse("admin:accounts_user_change", args=[stale.pk]),
        _change_data(stale.email),
    )
    password = client.post(
        reverse("admin:auth_user_password_change", args=[stale.pk]),
        {
            "usable_password": "true",
            "password1": NEW_PASSWORD,
            "password2": NEW_PASSWORD,
        },
    )

    assert changed.status_code == 403
    assert password.status_code == 403
    assert not get_user_model()._default_manager.filter(pk=stale.pk).exists()


@pytest.mark.parametrize("make", STAFF)
def test_a_password_form_loaded_before_the_deletion_cannot_set_one(
    client: Client, make: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    client.force_login(make())
    account = _played_account()
    stale = get_user_model()._default_manager.get(pk=account.pk)
    cleared = _clear(account)
    before = _snapshot(cleared)
    monkeypatch.setattr(UserAdmin, "get_object", lambda *args, **kwargs: stale)
    # The view's own permission checks ran before the deletion committed.
    monkeypatch.setattr(account_admin, "_stored_is_cleared", lambda _: False)

    response = client.post(
        reverse("admin:auth_user_password_change", args=[cleared.pk]),
        {
            "usable_password": "true",
            "password1": NEW_PASSWORD,
            "password2": NEW_PASSWORD,
        },
    )

    assert response.status_code == 403
    assert _snapshot(cleared) == before


def test_an_inactive_account_with_a_real_email_can_still_be_changed(
    client: Client,
) -> None:
    """Only a cleared Account is refused: staff may still reactivate an
    Account that was only deactivated."""
    client.force_login(_manager())
    account = UserFactory.create(is_active=False)

    response = client.post(
        reverse("admin:accounts_user_change", args=[account.pk]),
        _change_data(account.email),
    )

    assert response.status_code == 302
    account.refresh_from_db()
    assert account.is_active


PLACEHOLDER = f"deleted-made-up@{DELETED_EMAIL_DOMAIN}"


@pytest.mark.parametrize("make", STAFF)
@pytest.mark.parametrize("email", [PLACEHOLDER, PLACEHOLDER.upper()])
def test_staff_cannot_make_an_account_look_cleared(
    client: Client, make: Any, email: str
) -> None:
    """Only Account deletion writes a placeholder address: an Account that
    looked cleared could never be changed or deleted again, so its
    personal data would stay."""
    client.force_login(make())
    account = UserFactory.create()
    before = _snapshot(account)

    response = client.post(
        reverse("admin:accounts_user_change", args=[account.pk]), {"email": email}
    )

    assert response.status_code == 200
    assert PLACEHOLDER_EMAIL_MESSAGE in _text(response)
    assert _snapshot(account) == before


@pytest.mark.parametrize("make", STAFF)
def test_staff_cannot_add_an_account_with_a_placeholder_address(
    client: Client, make: Any
) -> None:
    client.force_login(make())
    count = get_user_model()._default_manager.count()

    response = client.post(
        reverse("admin:accounts_user_add"),
        {
            "email": PLACEHOLDER,
            "usable_password": "true",
            "password1": NEW_PASSWORD,
            "password2": NEW_PASSWORD,
        },
    )

    assert response.status_code == 200
    assert PLACEHOLDER_EMAIL_MESSAGE in _text(response)
    assert get_user_model()._default_manager.count() == count


@pytest.mark.parametrize("make", STAFF)
def test_an_account_reactivated_before_the_fix_is_only_saved_inactive(
    client: Client, make: Any
) -> None:
    """A cleared Account staff reactivated before #344 is active with its
    placeholder address. It keeps the address, and a save must leave it
    inactive."""
    client.force_login(make())
    cleared = _clear(_played_account())
    get_user_model()._default_manager.filter(pk=cleared.pk).update(is_active=True)
    url = reverse("admin:accounts_user_change", args=[cleared.pk])

    new_email = client.post(url, {"email": "made-up-back@example.com"})
    still_active = client.post(url, _change_data(cleared.email))
    deactivated = client.post(url, {"email": cleared.email})

    assert new_email.status_code == 200
    assert PLACEHOLDER_EMAIL_KEPT_MESSAGE in _text(new_email)
    assert still_active.status_code == 200
    assert PLACEHOLDER_ACTIVE_MESSAGE in _text(still_active)
    assert deactivated.status_code == 302
    stored = get_user_model()._default_manager.get(pk=cleared.pk)
    assert stored.email == cleared.email
    assert not stored.is_active


@pytest.mark.parametrize("make", STAFF)
def test_the_only_account_action_is_the_delete(make: Any) -> None:
    """No bulk action or list edit can reactivate a cleared Account: the
    delete, the only action, refuses one (#317)."""
    request = RequestFactory().get("/")
    request.user = make()
    model_admin = admin.site._registry[get_user_model()]

    assert set(model_admin.get_actions(request)) == {"delete_selected"}
    assert not model_admin.list_editable


@pytest.mark.parametrize("base", [APP, BROWSER])
@pytest.mark.parametrize("email", [PLACEHOLDER, f"  {PLACEHOLDER.upper()} "])
def test_signup_refuses_a_placeholder_address(base: str, email: str) -> None:
    """Sign-up (and an added address, through the same adapter check)
    refuses a placeholder address, on its normalized value: only a
    deletion writes one."""
    cache.clear()
    if base == APP:
        response = signup(Client(), APP, email)
    else:
        client = browser_client()
        response = post(
            client,
            f"{BROWSER}/auth/signup",
            {"email": email, "password": PASSWORD, **SIGNUP_DETAILS},
            **csrf_header(client),
        )

    assert response.status_code == 400
    assert [e["message"] for e in response.json()["errors"]] == [
        PLACEHOLDER_EMAIL_MESSAGE
    ]
    assert not get_user_model()._default_manager.exists()
