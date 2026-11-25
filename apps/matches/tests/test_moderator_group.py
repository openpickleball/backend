"""The Moderator group and the permission split in the admin (#40).

A data migration creates the group with exactly the permissions a
moderator needs. Staff in the group can run every moderator action; staff
without it see none, a forged action changes nothing, and the edit page
is refused. Made-up data only.
"""

from datetime import date

import pytest
from django.contrib.auth.models import Group, Permission
from django.test import Client
from django.urls import reverse

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditAction, AuditEvent
from apps.clubs.tests.factories import OrganizerFactory
from apps.matches.models import (
    FlagResolution,
    Match,
    MatchFlag,
    MatchFormat,
    MatchStatus,
)
from apps.matches.permissions import (
    MODERATOR_GROUP,
    MODERATOR_PERMISSIONS,
    is_moderator,
)
from apps.matches.services import record_match
from apps.matches.workflow import confirm_match, dispute_match
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db

REASON = "Made-up moderator reason."
PLAYED_ON = date(2026, 4, 11)
MATCH_ACTIONS = ("approve_selected", "void_selected", "dismiss_flags_selected")
EXPECTED = {
    "matches.moderate_match",
    "matches.view_match",
    "matches.view_matchflag",
    "clubs.verify_organizer",
    "clubs.view_organizer",
    "clubs.view_organizerreport",
}


def _codes(group: Group) -> set[str]:
    return {f"{p.content_type.app_label}.{p.codename}" for p in group.permissions.all()}


@pytest.fixture
def players() -> list[Player]:
    return [PlayerFactory.create() for _ in range(4)]


def _confirmed(players: list[Player], games: tuple[tuple[int, int], ...]) -> Match:
    match = record_match(
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=games,
        played_on=PLAYED_ON,
        submitted_by_user=players[0].account,
    ).match
    confirm_match(match, actor=players[0].account)
    return confirm_match(match, actor=players[2].account)


def _in_group() -> User:
    account: User = UserFactory.create(is_staff=True)
    account.groups.add(Group.objects.get(name=MODERATOR_GROUP))
    return account


def _staff_without_group() -> User:
    """Staff who may view matches and organizers, and nothing more."""
    account: User = UserFactory.create(is_staff=True)
    account.user_permissions.add(
        *Permission.objects.filter(
            content_type__app_label__in=["matches", "clubs"],
            codename__in=["view_match", "view_matchflag", "view_organizer"],
        )
    )
    return account


def _post_action(client: Client, url: str, action: str, pks: list[int]) -> None:
    client.post(
        url,
        {
            "action": action,
            "_selected_action": [str(pk) for pk in pks],
            "reason": REASON,
        },
    )


def test_the_group_has_exactly_the_moderator_permissions() -> None:
    group = Group.objects.get(name=MODERATOR_GROUP)

    assert _codes(group) == EXPECTED
    assert set(MODERATOR_PERMISSIONS) == EXPECTED
    assert MODERATOR_GROUP == "Moderator"


def test_a_group_member_is_a_moderator() -> None:
    assert is_moderator(_in_group())
    assert not is_moderator(_staff_without_group())


def test_group_member_runs_every_moderator_action(
    client: Client, players: list[Player]
) -> None:
    moderator = _in_group()
    client.force_login(moderator)
    url = reverse("admin:matches_match_changelist") + "?review=all"
    first = _confirmed(players, ((11, 7), (11, 9)))
    second = _confirmed(players, ((11, 3), (11, 2)))
    third = _confirmed(players, ((11, 1), (11, 0)))
    dispute_match(first, actor=players[1].account)

    page = client.get(url).content.decode()
    for action in (*MATCH_ACTIONS, "edit_selected"):
        assert action in page

    _post_action(client, url, "approve_selected", [first.pk])
    assert Match.objects.get(pk=first.pk).status == MatchStatus.CONFIRMED

    _post_action(client, url, "dismiss_flags_selected", [second.pk])
    assert MatchFlag.objects.filter(resolution=FlagResolution.DISMISSED).exists()

    _post_action(client, url, "void_selected", [third.pk])
    assert Match.objects.get(pk=third.pk).status == MatchStatus.VOIDED

    edit = reverse("admin:matches_match_edit", args=[second.pk])
    assert client.get(edit).status_code == 200
    client.post(
        edit,
        {
            "reason": REASON,
            "played_on": PLAYED_ON.isoformat(),
            "team_a": " ".join(p.public_code for p in players[:2]),
            "team_b": " ".join(p.public_code for p in players[2:]),
            "games": "11-4\n11-5",
        },
    )
    assert Match.objects.filter(corrects=second).exists()

    organizer = OrganizerFactory.create()
    _post_action(
        client,
        reverse("admin:clubs_organizer_changelist"),
        "verify_selected",
        [organizer.pk],
    )
    organizer.refresh_from_db()
    assert organizer.verified_by_id == moderator.pk
    assert AuditEvent.objects.get(action=AuditAction.ORGANIZER_VERIFY).reason == REASON


def test_staff_without_the_group_sees_no_actions(
    client: Client, players: list[Player]
) -> None:
    client.force_login(_staff_without_group())
    _confirmed(players, ((11, 7), (11, 9)))
    OrganizerFactory.create()

    matches_page = client.get(reverse("admin:matches_match_changelist") + "?review=all")
    organizers_page = client.get(reverse("admin:clubs_organizer_changelist"))

    assert matches_page.status_code == 200
    for action in (*MATCH_ACTIONS, "edit_selected"):
        assert action not in matches_page.content.decode()
    assert organizers_page.status_code == 200
    assert "verify_selected" not in organizers_page.content.decode()


def test_staff_without_the_group_forged_posts_change_nothing(
    client: Client, players: list[Player]
) -> None:
    client.force_login(_staff_without_group())
    url = reverse("admin:matches_match_changelist") + "?review=all"
    first = _confirmed(players, ((11, 7), (11, 9)))
    second = _confirmed(players, ((11, 3), (11, 2)))
    dispute_match(first, actor=players[1].account)
    organizer = OrganizerFactory.create()
    before = AuditEvent.objects.count()

    for action in (*MATCH_ACTIONS, "edit_selected"):
        _post_action(client, url, action, [first.pk, second.pk])
    _post_action(
        client,
        reverse("admin:clubs_organizer_changelist"),
        "verify_selected",
        [organizer.pk],
    )

    assert Match.objects.get(pk=first.pk).status == MatchStatus.DISPUTED
    assert Match.objects.get(pk=second.pk).status == MatchStatus.CONFIRMED
    assert MatchFlag.objects.filter(resolved_at__isnull=False).count() == 0
    organizer.refresh_from_db()
    assert organizer.is_verified is False
    assert AuditEvent.objects.count() == before


def test_staff_without_the_group_gets_403_on_the_edit_page(
    client: Client, players: list[Player]
) -> None:
    client.force_login(_staff_without_group())
    match = _confirmed(players, ((11, 7), (11, 9)))
    edit = reverse("admin:matches_match_edit", args=[match.pk])

    assert client.get(edit).status_code == 403
    response = client.post(
        edit,
        {
            "reason": REASON,
            "played_on": PLAYED_ON.isoformat(),
            "team_a": " ".join(p.public_code for p in players[:2]),
            "team_b": " ".join(p.public_code for p in players[2:]),
            "games": "11-4\n11-5",
        },
    )
    assert response.status_code == 403
    assert Match.objects.get(pk=match.pk).status == MatchStatus.CONFIRMED
    assert not Match.objects.filter(corrects=match).exists()


def test_superuser_who_is_not_active_is_not_a_moderator() -> None:
    account: User = UserFactory.create(
        is_staff=True, is_superuser=True, is_active=False
    )

    assert not is_moderator(account)
