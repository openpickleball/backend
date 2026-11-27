"""Deleting an Account in the admin runs Account deletion (#317, ADR 0048).

The Account admin's delete page and ``delete_selected`` call
``delete_account`` for each Account, with the staff member as actor, so
they leave the same state as ``DELETE /api/v1/accounts/me``. As in the
API, no staff or superuser Account is deleted this way. Made-up data
only.
"""

from importlib import import_module
from typing import TYPE_CHECKING, Any

import pytest
from allauth.account.models import EmailAddress
from allauth.mfa.models import Authenticator
from allauth.usersessions.models import UserSession
from django.conf import settings
from django.contrib.admin.models import LogEntry
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.contrib.sessions.models import Session
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.admin import UserAdmin
from apps.accounts.deletion import (
    DELETED_EMAIL_DOMAIN,
    DELETED_PLAYER_NAME,
    REMOVED_ORGANIZER_NAME,
    delete_account,
)
from apps.accounts.models import Agreement, AgreementAcceptance
from apps.accounts.tests.factories import UserFactory, enroll_totp
from apps.audit.admin_access import AccessAuditAdminMixin
from apps.audit.models import AuditAction, AuditEvent
from apps.clubs.models import ClubRole, Organizer, OrganizerMember
from apps.clubs.tests.factories import (
    ClubRoleFactory,
    OrganizerFactory,
    OrganizerMemberFactory,
)
from apps.matches.models import Team
from apps.players.models import Player, PlayerPrivacy
from apps.players.tests.factories import PlayerFactory
from apps.ratings.tests.builders import saved_match

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

pytestmark = pytest.mark.django_db


def _manager() -> "_User":
    """Staff that may view and delete Accounts, not a superuser."""
    account: _User = UserFactory.create(is_staff=True)
    account.user_permissions.set(
        Permission.objects.filter(codename__in=["view_user", "delete_user"])
    )
    return account


@pytest.fixture
def manager() -> "_User":
    return _manager()


@pytest.fixture
def manager_client(client: Client, manager: "_User") -> Client:
    client.force_login(manager)
    return client


def _delete_url(account: "_User") -> str:
    return reverse("admin:accounts_user_delete", args=[account.pk])


def _bulk(client: Client, *accounts: "_User") -> Any:
    return client.post(
        reverse("admin:accounts_user_changelist"),
        {
            "action": "delete_selected",
            "_selected_action": [str(a.pk) for a in accounts],
            "post": "yes",
        },
    )


def _session_for(account: "_User") -> str:
    store = import_module(settings.SESSION_ENGINE).SessionStore()
    store["made_up"] = 1
    store.create()
    UserSession.objects.create(
        user=account, session_key=store.session_key, ip="192.0.2.1", user_agent="x"
    )
    return str(store.session_key)


def _player_account(*, played: bool) -> "_User":
    """A player's Account with the rows deletion removes: an address, MFA,
    agreements, a session, a shared and a sole Organizer membership, a Club
    Role, and an admin log entry. With ``played``, its Player is in the
    Ledger and stays."""
    account: _User = UserFactory.create()
    EmailAddress.objects.create(
        user=account, email=account.email, verified=True, primary=True
    )
    enroll_totp(account)
    for agreement in Agreement:
        AgreementAcceptance.objects.create(
            account=account, agreement=agreement, version=1, accepted_at=timezone.now()
        )
    _session_for(account)
    player = PlayerFactory.create(account=account)
    if played:
        others = [PlayerFactory.create() for _ in range(3)]
        saved_match(players={Team.A: [player, others[0]], Team.B: others[1:]})
    shared = OrganizerFactory.create()
    OrganizerMemberFactory.create(organizer=shared)
    OrganizerMemberFactory.create(organizer=shared, account=account)
    OrganizerMemberFactory.create(account=account)
    ClubRoleFactory.create(account=account)
    LogEntry.objects.create(
        user=_manager(),
        content_type=ContentType.objects.get_for_model(get_user_model()),
        object_id=str(account.pk),
        object_repr=account.email,
        action_flag=2,
    )
    return account


def _events(pk: int, action: str) -> list[AuditEvent]:
    return list(
        AuditEvent.objects.filter(
            action=action,
            target_content_type=ContentType.objects.get_for_model(get_user_model()),
            target_object_id=str(pk),
        )
    )


def _state(
    pk: int, player_pk: int, organizer_pks: list[int], since: int
) -> dict[str, Any]:
    """What a deletion leaves behind for one Account, without ids, emails,
    or actors, so two deletions can be compared."""
    account = get_user_model()._default_manager.filter(pk=pk).first()
    player = Player.objects.filter(pk=player_pk).first()
    session_keys = list(
        UserSession.objects.filter(user_id=pk).values_list("session_key", flat=True)
    )
    privacy = PlayerPrivacy.objects.filter(player_id=player_pk).first()
    return {
        "account": None
        if account is None
        else {
            "placeholder": account.email.endswith(f"@{DELETED_EMAIL_DOMAIN}"),
            "usable_password": account.has_usable_password(),
            "flags": (account.is_active, account.is_staff, account.is_superuser),
            "last_login": account.last_login,
            "groups": account.groups.count(),
            "permissions": account.user_permissions.count(),
        },
        "player": None
        if player is None
        else {
            "display_name": player.display_name,
            "legal_name": player.legal_name,
            "birth": (player.birth_year, player.birth_month),
            "gender_division": player.gender_division,
            "place": (player.city, player.region),
        },
        "privacy": None
        if privacy is None
        else {name: getattr(privacy, name) for name in PlayerPrivacy.SETTINGS},
        "addresses": EmailAddress.objects.filter(user_id=pk).count(),
        "authenticators": Authenticator.objects.filter(user_id=pk).count(),
        "agreements": AgreementAcceptance.objects.filter(account_id=pk).count(),
        "user_sessions": UserSession.objects.filter(user_id=pk).count(),
        "sessions": Session.objects.filter(session_key__in=session_keys).count(),
        "memberships": OrganizerMember.objects.filter(account_id=pk).count(),
        "roles": ClubRole.objects.filter(account_id=pk).count(),
        "log_entries": LogEntry.objects.filter(
            content_type=ContentType.objects.get_for_model(get_user_model()),
            object_id=str(pk),
        ).count(),
        "removed_organizers": Organizer.objects.filter(
            pk__in=organizer_pks, name=REMOVED_ORGANIZER_NAME
        ).count(),
        "events": sorted(
            AuditEvent.objects.filter(
                pk__gt=since,
                action__in=[
                    AuditAction.ACCOUNT_DELETE,
                    AuditAction.ORGANIZER_NAME_REMOVE,
                    AuditAction.ORGANIZER_MEMBER_REMOVE,
                    AuditAction.CLUB_ROLE_REMOVE,
                ],
            ).values_list("action", flat=True)
        ),
    }


def _last_event_id() -> int:
    last = AuditEvent.objects.order_by("-pk").values_list("pk", flat=True).first()
    return last or 0


def _organizer_pks(account: "_User") -> list[int]:
    return list(
        OrganizerMember.objects.filter(account_id=account.pk).values_list(
            "organizer_id", flat=True
        )
    )


def test_the_account_admin_keeps_the_access_audit_mixin_first() -> None:
    assert UserAdmin.__bases__[0] is AccessAuditAdminMixin


@pytest.mark.parametrize("played", [True, False])
def test_the_delete_page_leaves_the_same_state_as_self_deletion(
    manager_client: Client, played: bool
) -> None:
    by_admin = _player_account(played=played)
    by_self = _player_account(played=played)
    admin_ids = (by_admin.pk, by_admin.player.pk, _organizer_pks(by_admin))
    self_ids = (by_self.pk, by_self.player.pk, _organizer_pks(by_self))

    since = _last_event_id()
    response = manager_client.post(_delete_url(by_admin), {"post": "yes"})
    assert response.status_code == 302
    admin_state = _state(*admin_ids, since=since)
    since = _last_event_id()
    delete_account(by_self, actor=by_self)
    self_state = _state(*self_ids, since=since)

    assert admin_state == self_state
    if played:
        assert admin_state["account"]["placeholder"] is True
        assert admin_state["player"]["display_name"] == DELETED_PLAYER_NAME
    assert admin_state["addresses"] == admin_state["sessions"] == 0


def test_the_delete_page_records_the_staff_member_as_actor(
    manager_client: Client, manager: "_User"
) -> None:
    account = _player_account(played=True)

    response = manager_client.post(_delete_url(account), {"post": "yes"})

    assert response.status_code == 302
    [admin_event] = _events(account.pk, AuditAction.ADMIN_DELETE)
    [deletion] = _events(account.pk, AuditAction.ACCOUNT_DELETE)
    assert admin_event.actor_user_id == manager.pk
    assert deletion.actor_user_id == manager.pk
    assert deletion.after == {"account_kept": True, "player_kept": True}
    removals = AuditEvent.objects.filter(action=AuditAction.ORGANIZER_MEMBER_REMOVE)
    assert removals.count() == 2
    assert {e.actor_user_id for e in removals} == {manager.pk}
    # Only the manager's own login is left in the admin log, none for the
    # Account.
    assert not LogEntry.objects.filter(object_id=str(account.pk)).exists()


def test_an_account_nothing_protects_is_hard_deleted_from_the_page(
    manager_client: Client,
) -> None:
    account = UserFactory.create()
    PlayerFactory.create(account=account)
    pk = account.pk

    response = manager_client.post(_delete_url(account), {"post": "yes"})

    assert response.status_code == 302
    assert not get_user_model()._default_manager.filter(pk=pk).exists()
    assert _events(pk, AuditAction.ACCOUNT_DELETE)[0].after == {
        "account_kept": False,
        "player_kept": False,
    }


def test_the_confirmation_page_is_not_blocked_by_the_ledger(
    manager_client: Client,
) -> None:
    account = _player_account(played=True)

    response = manager_client.get(_delete_url(account))

    assert response.status_code == 200
    assert response.context["protected"] == []
    assert not response.context["perms_lacking"]
    assert response.context["deleted_objects"] == [str(account)]


def test_the_bulk_action_deletes_each_account_through_the_service(
    manager_client: Client, manager: "_User"
) -> None:
    kept = _player_account(played=True)
    gone = UserFactory.create()

    page = manager_client.post(
        reverse("admin:accounts_user_changelist"),
        {"action": "delete_selected", "_selected_action": [str(kept.pk), str(gone.pk)]},
    )
    response = _bulk(manager_client, kept, gone)

    assert page.status_code == 200
    assert page.context["protected"] == []
    assert not page.context["perms_lacking"]
    assert response.status_code == 302
    assert not get_user_model()._default_manager.filter(pk=gone.pk).exists()
    row = get_user_model()._default_manager.get(pk=kept.pk)
    assert row.email.endswith(f"@{DELETED_EMAIL_DOMAIN}")
    assert not row.is_active
    for account in (kept, gone):
        [admin_event] = _events(account.pk, AuditAction.ADMIN_DELETE)
        [deletion] = _events(account.pk, AuditAction.ACCOUNT_DELETE)
        assert admin_event.actor_user_id == deletion.actor_user_id == manager.pk
    assert not LogEntry.objects.filter(object_id__in=[str(kept.pk), str(gone.pk)])


def test_the_bulk_action_runs_each_account_in_its_own_transaction(
    rf: Any, manager: "_User", monkeypatch: pytest.MonkeyPatch
) -> None:
    from django.contrib import admin
    from django.db import connection

    import apps.accounts.admin as account_admin

    depths: list[int] = []

    def fake_delete(account: Any, *, actor: Any) -> None:
        depths.append(len(connection.savepoint_ids))

    monkeypatch.setattr(account_admin, "delete_account", fake_delete)
    accounts = [UserFactory.create() for _ in range(2)]
    request = rf.post("/")
    request.user = manager
    model_admin = admin.site._registry[get_user_model()]

    model_admin.delete_queryset(
        request,
        get_user_model()._default_manager.filter(pk__in=[a.pk for a in accounts]),
    )
    baseline = len(connection.savepoint_ids)
    model_admin.delete_model(request, UserFactory.create())

    # Each Account is as deep as a single delete: no outer transaction
    # holds the whole selection.
    assert depths == [depths[-1]] * 3
    assert depths[-1] == baseline + 2


@pytest.mark.parametrize("fields", [{"is_staff": True}, {"is_superuser": True}])
def test_a_superuser_cannot_delete_a_staff_or_superuser_account(
    client: Client, fields: dict[str, bool]
) -> None:
    client.force_login(UserFactory.create(is_staff=True, is_superuser=True))
    target = UserFactory.create(**fields)
    player = UserFactory.create()

    page = client.post(_delete_url(target), {"post": "yes"})
    bulk = _bulk(client, player, target)

    assert page.status_code == 403
    assert bulk.status_code == 403
    pks = [target.pk, player.pk]
    assert get_user_model()._default_manager.filter(pk__in=pks).count() == 2
    assert not AuditEvent.objects.filter(
        action__in=[AuditAction.ADMIN_DELETE, AuditAction.ACCOUNT_DELETE]
    ).exists()
    target.refresh_from_db()
    assert target.is_active


def test_a_superuser_still_deletes_an_account_in_a_group(client: Client) -> None:
    superuser = UserFactory.create(is_staff=True, is_superuser=True)
    client.force_login(superuser)
    target = UserFactory.create()
    target.groups.add(Group.objects.create(name="Made-up helpers"))

    response = client.post(_delete_url(target), {"post": "yes"})

    assert response.status_code == 302
    assert _events(target.pk, AuditAction.ACCOUNT_DELETE)[0].actor_user_id == (
        superuser.pk
    )


def test_an_account_a_deletion_cleared_cannot_be_deleted_again(
    manager_client: Client,
) -> None:
    account = _player_account(played=True)
    delete_account(account, actor=account)
    account.refresh_from_db()

    page = manager_client.post(_delete_url(account), {"post": "yes"})
    bulk = _bulk(manager_client, account)

    assert page.status_code == 403
    assert bulk.status_code == 403
    assert len(_events(account.pk, AuditAction.ACCOUNT_DELETE)) == 1


def test_an_account_made_staff_after_the_page_loaded_is_refused(
    rf: Any, manager: "_User"
) -> None:
    from django.contrib import admin
    from django.core.exceptions import PermissionDenied

    target = UserFactory.create()
    stale = get_user_model()._default_manager.get(pk=target.pk)
    get_user_model()._default_manager.filter(pk=target.pk).update(is_staff=True)
    request = rf.post("/")
    request.user = manager
    model_admin = admin.site._registry[get_user_model()]

    with pytest.raises(PermissionDenied):
        model_admin.delete_model(request, stale)

    target.refresh_from_db()
    assert target.is_staff
    assert target.is_active
