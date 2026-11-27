"""A moderator removes the personal data of a player under 13 (#190, ADR 0059).

``remove_underage_player`` runs Account deletion's own steps
(``delete_account``) for the Player's Account, with the moderator as
actor and one ``player.underage_remove`` Audit Event. Only an active
Account with ``players.remove_underage_player`` may run it, and every
refusal changes nothing. Made-up data only.
"""

import json
from typing import TYPE_CHECKING, Any

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser, Group, Permission
from django.core.cache import cache

from apps.accounts.deletion import (
    DELETED_EMAIL_DOMAIN,
    DELETED_PLAYER_NAME,
    AccountAlreadyDeleted,
    delete_account,
)
from apps.accounts.tests.admin import verified_account
from apps.accounts.tests.factories import UserFactory
from apps.accounts.tests.headless import APP, HeadlessSession
from apps.audit.models import AuditAction, AuditEvent
from apps.clubs.tests.factories import OrganizerMemberFactory
from apps.events.models import EventKind
from apps.events.tests.factories import EventFactory
from apps.matches.models import Match, MatchParticipant, Team
from apps.matches.permissions import MODERATOR_GROUP, MODERATOR_PERMISSIONS
from apps.players.models import Player, PlayerPrivacy, Visibility
from apps.players.tests.factories import PlayerFactory
from apps.players.underage import (
    REMOVE_UNDERAGE_PERMISSION,
    UnderageRemovalRefused,
    remove_underage_player,
)
from apps.ratings import services as rating_services
from apps.ratings.models import RatingState
from apps.ratings.services import (
    LiveUpdateOutcome,
    recompute_ratings,
    update_ratings_for_match,
)
from apps.ratings.tests.builders import saved_match

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

pytestmark = pytest.mark.django_db

LEGAL_NAME = "Quillon Made-up Legalname"
DISPLAY_NAME = "Quill Madeup"
CITY = "Madeupton"
REGION = "Madeup Region"
CLEARED_FIELDS = [
    "birth_month",
    "birth_year",
    "city",
    "display_name",
    "gender_division",
    "legal_name",
    "region",
]


def _permission() -> Permission:
    return Permission.objects.get(
        content_type__app_label="players", codename="remove_underage_player"
    )


def _moderator(**fields: Any) -> "_User":
    account: _User = UserFactory.create(**fields)
    account.user_permissions.add(_permission())
    return get_user_model().objects.get(pk=account.pk)


def _player(account: "_User | None" = None) -> Player:
    if account is None:
        account = UserFactory.create()
        EmailAddress.objects.create(
            user=account, email=account.email, verified=True, primary=True
        )
    player: Player = PlayerFactory.create(
        account=account,
        display_name=DISPLAY_NAME,
        legal_name=LEGAL_NAME,
        city=CITY,
        region=REGION,
    )
    return player


def _played(player: Player) -> Match:
    others = [PlayerFactory.create() for _ in range(3)]
    match, _ = saved_match(
        players={Team.A: [player, others[0]], Team.B: others[1:]},
        event=EventFactory.create(kind=EventKind.TOURNAMENT),
    )
    return match


def _state(player: Player) -> dict[str, Any]:
    """Every stored value a refusal must leave as it was."""
    stored = Player.objects.get(pk=player.pk)
    account = get_user_model().objects.get(pk=player.account_id)
    privacy = PlayerPrivacy.objects.get(player=stored)
    return {
        "player": [
            getattr(stored, name)
            for name in (
                "display_name",
                "legal_name",
                "birth_year",
                "birth_month",
                "gender_division",
                "city",
                "region",
            )
        ],
        "privacy": [getattr(privacy, name) for name in PlayerPrivacy.SETTINGS],
        "account": [account.email, account.is_active, account.password],
        "addresses": EmailAddress.objects.filter(user_id=account.pk).count(),
        "events": AuditEvent.objects.count(),
    }


def _removal_events() -> list[AuditEvent]:
    return list(AuditEvent.objects.filter(action=AuditAction.PLAYER_UNDERAGE_REMOVE))


def test_the_permission_exists_and_moderators_do_not_hold_it() -> None:
    assert REMOVE_UNDERAGE_PERMISSION == "players.remove_underage_player"
    assert _permission()
    assert REMOVE_UNDERAGE_PERMISSION not in MODERATOR_PERMISSIONS
    group = Group.objects.get(name=MODERATOR_GROUP)
    assert not group.permissions.filter(codename="remove_underage_player").exists()


def test_a_player_in_the_ledger_is_kept_and_shown_as_after_account_deletion() -> None:
    player = _player()
    match = _played(player)
    moderator = _moderator()
    pk, code = player.account_id, player.public_code

    result = remove_underage_player(player, moderator=moderator)

    assert (result.account_kept, result.player_kept) == (True, True)
    kept = Player.objects.get(pk=player.pk)
    assert kept.public_code == code
    assert kept.display_name == DELETED_PLAYER_NAME
    assert kept.legal_name == ""
    assert (kept.birth_year, kept.birth_month) == (None, None)
    assert kept.gender_division == ""
    assert (kept.city, kept.region) == ("", "")
    privacy = PlayerPrivacy.objects.get(player=kept)
    assert {getattr(privacy, name) for name in PlayerPrivacy.SETTINGS} == {
        Visibility.PRIVATE
    }
    assert privacy.rating_visibility == Visibility.PRIVATE
    assert MatchParticipant.objects.filter(match=match, player=kept).exists()
    account = get_user_model().objects.get(pk=pk)
    assert account.email.endswith(f"@{DELETED_EMAIL_DOMAIN}")
    assert not account.has_usable_password()
    assert account.is_active is False
    assert not EmailAddress.objects.filter(user_id=pk).exists()


def test_an_unprotected_player_and_account_are_deleted() -> None:
    player = _player()
    pk = player.account_id

    result = remove_underage_player(player, moderator=_moderator())

    assert (result.account_kept, result.player_kept) == (False, False)
    assert not Player.objects.filter(pk=player.pk).exists()
    assert not get_user_model().objects.filter(pk=pk).exists()
    [event] = _removal_events()
    assert event.after == {
        "account_kept": False,
        "player_kept": False,
        "fields": CLEARED_FIELDS,
    }


def test_one_removal_event_names_the_moderator_and_no_value() -> None:
    player = _player()
    _played(player)
    moderator = _moderator()
    values = [player.account.email, DISPLAY_NAME, LEGAL_NAME, CITY, REGION]
    before = set(AuditEvent.objects.values_list("pk", flat=True))

    remove_underage_player(player, moderator=moderator)

    [event] = _removal_events()
    assert event.actor_user_id == moderator.pk
    assert event.target_content_type.model_class() is get_user_model()
    assert event.target_object_id == str(player.account_id)
    assert event.after == {
        "account_kept": True,
        "player_kept": True,
        "fields": CLEARED_FIELDS,
    }
    assert event.reason == ""
    assert not AuditEvent.objects.filter(action=AuditAction.ACCOUNT_DELETE).exists()
    for new in AuditEvent.objects.exclude(pk__in=before):
        stored = json.dumps([new.before, new.after, new.reason])
        for value in values:
            assert value not in stored


def test_an_organizer_membership_removal_is_recorded_with_the_moderator() -> None:
    player = _player()
    _played(player)
    OrganizerMemberFactory.create(account=player.account)
    moderator = _moderator()

    remove_underage_player(player, moderator=moderator)

    event = AuditEvent.objects.get(action=AuditAction.ORGANIZER_MEMBER_REMOVE)
    assert event.actor_user_id == moderator.pk


def test_a_superuser_may_remove() -> None:
    player = _player()
    _played(player)
    superuser = UserFactory.create(is_staff=True, is_superuser=True)

    remove_underage_player(player, moderator=superuser)

    assert len(_removal_events()) == 1


def test_the_removed_account_cannot_sign_in_again() -> None:
    cache.clear()
    account = verified_account()
    email = account.email
    player = _player(account)
    _played(player)
    session = HeadlessSession(APP)
    assert session.login(email).status_code == 200
    assert session.get("auth/session").status_code == 200

    remove_underage_player(player, moderator=_moderator())

    # 410: allauth's app client no longer knows the session token.
    assert session.get("auth/session").status_code == 410
    assert HeadlessSession(APP).login(email).status_code != 200


def test_the_removed_player_has_no_birth_year_or_month_for_partner_sharing() -> None:
    # No partner sharing helper exists yet: a Player whose birth year and
    # month were removed is excluded from all partner sharing (ADR 0048).
    player = _player()
    _played(player)

    remove_underage_player(player, moderator=_moderator())

    assert Player.objects.filter(pk=player.pk, birth_year__isnull=True).exists()
    assert Player.objects.filter(pk=player.pk, birth_month__isnull=True).exists()


def test_a_full_recompute_gives_every_player_the_same_ratings() -> None:
    player = _player()
    matches = [_played(player) for _ in range(2)]
    for match in matches:
        assert update_ratings_for_match(match.pk) == LiveUpdateOutcome.UPDATED
    recompute_ratings()

    def ratings() -> dict[tuple[str, str], list[object]]:
        return {
            (str(state.player.public_code), state.rating_format): [
                getattr(state, name) for name in rating_services.VALUE_FIELDS
            ]
            for state in RatingState.objects.select_related("player")
        }

    before = ratings()
    assert any(code == player.public_code for code, _ in before)

    remove_underage_player(player, moderator=_moderator())
    recompute_ratings()

    assert ratings() == before


# Refusals: each changes nothing and records no Audit Event.


def test_an_account_without_the_permission_is_refused() -> None:
    player = _player()
    state = _state(player)

    with pytest.raises(UnderageRemovalRefused):
        remove_underage_player(player, moderator=UserFactory.create(is_staff=True))

    assert _state(player) == state


@pytest.mark.parametrize("superuser", [False, True], ids=["permission", "superuser"])
def test_an_inactive_account_is_refused(superuser: bool) -> None:
    player = _player()
    if superuser:
        moderator = UserFactory.create(
            is_staff=True, is_superuser=True, is_active=False
        )
    else:
        moderator = _moderator(is_active=False)
    state = _state(player)

    with pytest.raises(UnderageRemovalRefused):
        remove_underage_player(player, moderator=moderator)

    assert _state(player) == state


def test_a_stale_moderator_whose_stored_account_is_inactive_is_refused() -> None:
    player = _player()
    moderator = _moderator()
    assert moderator.has_perm(REMOVE_UNDERAGE_PERMISSION)
    get_user_model().objects.filter(pk=moderator.pk).update(is_active=False)
    state = _state(player)

    with pytest.raises(UnderageRemovalRefused):
        remove_underage_player(player, moderator=moderator)

    assert _state(player) == state


def test_a_cached_permission_the_stored_account_lost_is_refused() -> None:
    player = _player()
    moderator = _moderator()
    assert moderator.has_perm(REMOVE_UNDERAGE_PERMISSION)  # fills the cache
    moderator.user_permissions.remove(_permission())
    state = _state(player)

    with pytest.raises(UnderageRemovalRefused):
        remove_underage_player(player, moderator=moderator)

    assert _state(player) == state


def test_the_moderators_own_player_is_refused() -> None:
    moderator = _moderator()
    player = _player(moderator)
    state = _state(player)

    with pytest.raises(UnderageRemovalRefused):
        remove_underage_player(player, moderator=moderator)

    assert _state(player) == state


@pytest.mark.parametrize("power", ["staff", "superuser", "group", "permission"])
def test_a_target_account_with_power_is_refused(power: str) -> None:
    account = UserFactory.create(
        is_staff=power in ("staff", "superuser"), is_superuser=power == "superuser"
    )
    if power == "group":
        account.groups.add(Group.objects.create(name="Made-up group"))
    if power == "permission":
        account.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="players", codename="view_player"
            )
        )
    player = _player(account)
    state = _state(player)

    with pytest.raises(UnderageRemovalRefused):
        remove_underage_player(player, moderator=_moderator())

    assert _state(player) == state


def test_a_player_whose_account_was_already_deleted_is_refused() -> None:
    player = _player()
    _played(player)
    delete_account(player.account, actor=player.account)
    player.refresh_from_db()
    events = AuditEvent.objects.count()

    with pytest.raises(AccountAlreadyDeleted):
        remove_underage_player(player, moderator=_moderator())

    assert AuditEvent.objects.count() == events
    assert not _removal_events()


def test_the_refusal_message_names_nobody() -> None:
    player = _player()

    with pytest.raises(UnderageRemovalRefused) as raised:
        remove_underage_player(player, moderator=UserFactory.create())

    text = str(raised.value)
    for value in (player.account.email, DISPLAY_NAME, LEGAL_NAME, player.public_code):
        assert value not in text


def test_a_signed_out_moderator_is_refused() -> None:
    player = _player()
    state = _state(player)

    with pytest.raises(UnderageRemovalRefused):
        remove_underage_player(player, moderator=AnonymousUser())

    assert _state(player) == state
