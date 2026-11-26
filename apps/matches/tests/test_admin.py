"""The moderator's match admin (#40, #273). Made-up data only.

A read-only Match admin that lists every match by default, with a
"Disputed or held" filter (``?held=yes``) for disputed matches and ones
an open flag holds. Moderators review no single match there: the only
actions are the bulk voids (``test_admin_bulk_void.py``). No page or
message shows a player's name or any Account's email.
"""

from datetime import date

import pytest
from django.contrib.auth.models import Group
from django.contrib.messages import get_messages
from django.test import Client
from django.urls import reverse

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.matches.models import Match, MatchFlag, MatchFormat, MatchStatus
from apps.matches.permissions import MODERATOR_GROUP
from apps.matches.services import record_match
from apps.matches.tests.factories import raw_update
from apps.matches.workflow import confirm_match, dispute_match, vouch_for_match
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db

CHANGELIST = "admin:matches_match_changelist"
HELD = "?held=yes"
REASON = "Made-up moderator reason."
PLAYED_ON = date(2026, 5, 2)
GAMES = ((11, 7), (9, 11), (11, 4))
OTHER_GAMES = ((11, 5), (11, 6))


def _moderator() -> User:
    account: User = UserFactory.create(is_staff=True)
    account.groups.add(Group.objects.get(name=MODERATOR_GROUP))
    return account


@pytest.fixture
def moderator() -> User:
    return _moderator()


@pytest.fixture
def moderator_client(client: Client, moderator: User) -> Client:
    client.force_login(moderator)
    return client


@pytest.fixture
def players() -> list[Player]:
    return [PlayerFactory.create(display_name=f"Zqadult{n} Made-up") for n in range(4)]


def _record(players: list[Player], games: tuple[tuple[int, int], ...]) -> Match:
    return record_match(
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=games,
        played_on=PLAYED_ON,
        submitted_by_user=players[0].account,
    ).match


def _confirmed(
    players: list[Player], games: tuple[tuple[int, int], ...] = GAMES
) -> Match:
    match = _record(players, games)
    confirm_match(match, actor=players[0].account)
    return confirm_match(match, actor=players[2].account)


def _disputed(players: list[Player]) -> Match:
    match = _confirmed(players)
    return dispute_match(match, actor=players[1].account)


def _pair(players: list[Player]) -> tuple[Match, Match]:
    earlier = _confirmed(players)
    later = _confirmed(players, OTHER_GAMES)
    assert MatchFlag.objects.filter(match=later, duplicate_of=earlier).exists()
    return earlier, later


def _page(client: Client, query: str = "") -> str:
    response = client.get(reverse(CHANGELIST) + query)
    assert response.status_code == 200
    return response.content.decode()


# The changelist and its filters


def test_default_list_is_every_match(
    moderator_client: Client, players: list[Player]
) -> None:
    plain = _confirmed([PlayerFactory.create() for _ in range(4)])
    earlier, later = _pair(players)
    disputed = _disputed([PlayerFactory.create() for _ in range(4)])

    page = _page(moderator_client)

    for match in (plain, earlier, later, disputed):
        assert match.public_code in page


def test_held_filter_lists_disputed_and_held_matches(
    moderator_client: Client, players: list[Player]
) -> None:
    plain = _confirmed([PlayerFactory.create() for _ in range(4)])
    earlier, later = _pair(players)
    disputed = _disputed([PlayerFactory.create() for _ in range(4)])

    page = _page(moderator_client, HELD)

    assert earlier.public_code in page
    assert later.public_code in page
    assert disputed.public_code in page
    assert plain.public_code not in page


def test_the_old_review_parameter_is_unknown(moderator_client: Client) -> None:
    response = moderator_client.get(reverse(CHANGELIST) + "?review=all")

    assert response.status_code == 302
    assert response["Location"].endswith("?e=1")


def test_a_voided_partner_leaves_the_held_list(
    moderator_client: Client, players: list[Player]
) -> None:
    earlier, later = _pair(players)
    raw_update(later, status=MatchStatus.VOIDED)

    page = _page(moderator_client, HELD)

    assert earlier.public_code not in page
    assert later.public_code not in page


def test_a_resolved_flag_leaves_the_held_list(
    moderator_client: Client, players: list[Player]
) -> None:
    earlier, later = _pair(players)
    for match in (earlier, later):
        vouch_for_match(match, actor=players[0].account)
        vouch_for_match(match, actor=players[2].account)
    assert MatchFlag.objects.get().resolved_at is not None

    page = _page(moderator_client, HELD)

    assert earlier.public_code not in page
    assert later.public_code not in page


def test_list_shows_why_a_match_is_disputed_or_held(
    moderator_client: Client, players: list[Player]
) -> None:
    _pair(players)
    _disputed([PlayerFactory.create() for _ in range(4)])

    page = _page(moderator_client, HELD)

    assert "Disputed or held" in page
    assert "Possible duplicate" in page
    assert "Disputed" in page


def test_status_filter(moderator_client: Client, players: list[Player]) -> None:
    confirmed = _confirmed(players)
    pending = _record([PlayerFactory.create() for _ in range(4)], GAMES)

    page = _page(moderator_client, f"?status__exact={MatchStatus.PENDING}")

    assert pending.public_code in page
    assert confirmed.public_code not in page


def test_search_by_open_code(moderator_client: Client, players: list[Player]) -> None:
    wanted = _confirmed(players)
    other = _confirmed([PlayerFactory.create() for _ in range(4)])

    page = _page(moderator_client, f"?q={wanted.public_code}")

    assert wanted.public_code in page
    assert other.public_code not in page


def test_no_add_and_no_delete(moderator_client: Client, players: list[Player]) -> None:
    match = _confirmed(players)
    superuser = UserFactory.create(is_staff=True, is_superuser=True)
    moderator_client.force_login(superuser)

    page = _page(moderator_client)
    assert "delete_selected" not in page
    assert moderator_client.get(reverse("admin:matches_match_add")).status_code == 403
    delete = moderator_client.get(
        reverse("admin:matches_match_delete", args=[match.pk])
    )
    assert delete.status_code == 403


def test_change_page_is_read_only_and_shows_codes(
    moderator_client: Client, players: list[Player]
) -> None:
    earlier, later = _pair(players)

    page = moderator_client.get(
        reverse("admin:matches_match_change", args=[later.pk])
    ).content.decode()

    assert 'name="_save"' not in page
    for player in players:
        assert player.public_code in page
    assert earlier.public_code in page
    assert "Possible duplicate" in page
    assert "Account" in page


def test_change_page_shows_the_stored_match_type(
    moderator_client: Client, players: list[Player]
) -> None:
    _earlier, later = _pair(players)

    page = moderator_client.get(
        reverse("admin:matches_match_change", args=[later.pk])
    ).content.decode()

    assert "Match type" in page
    assert "Self-reported" in page


def test_only_the_bulk_void_actions_are_offered(
    moderator_client: Client, players: list[Player]
) -> None:
    _confirmed(players)

    page = _page(moderator_client)

    assert "void_batch_selected" in page
    assert "void_submitter_selected" in page
    for removed in (
        "approve_selected",
        "void_selected",
        "dismiss_flags_selected",
        "edit_selected",
    ):
        assert f'value="{removed}"' not in page


# Nothing identifying on any page or message


def test_no_names_or_emails_anywhere(moderator_client: Client) -> None:
    minor = PlayerFactory.create(
        minor=True,
        display_name="Zqminor Made-up",
        legal_name="Zqlegal Made-up Fullname",
    )
    adults = [
        PlayerFactory.create(display_name=f"Zqadult{n} Made-up") for n in range(3)
    ]
    players = [minor, *adults]
    earlier, later = _pair(players)
    dispute_match(later, actor=players[1].account)
    secret = [
        minor.display_name,
        minor.legal_name,
        *(p.display_name for p in adults),
        *(p.account.email for p in players),
    ]

    pages = [
        _page(moderator_client),
        _page(moderator_client, HELD),
        moderator_client.get(
            reverse("admin:matches_match_change", args=[later.pk])
        ).content.decode(),
        moderator_client.get(
            reverse("admin:matches_match_change", args=[earlier.pk])
        ).content.decode(),
    ]
    for confirm in (False, True):
        data: dict[str, object] = {
            "action": "void_submitter_selected",
            "_selected_action": [str(later.pk)],
            "reason": REASON,
        }
        if confirm:
            data["post"] = "yes"
        response = moderator_client.post(reverse(CHANGELIST), data, follow=True)
        pages.append(response.content.decode())
        pages.append(" ".join(str(m) for m in get_messages(response.wsgi_request)))

    for text in pages:
        for value in secret:
            assert value not in text
