"""The moderator's match admin (#40). Made-up data only.

A read-only Match admin whose default list is the matches that need
review, with approve, void, dismiss flags, and edit. Every action needs a
typed reason, which goes into its Audit Event. No page or message shows a
player's name or any Account's email.
"""

from datetime import date

import pytest
from django.contrib.auth.models import Group
from django.contrib.messages import get_messages
from django.contrib.messages.storage.base import Message
from django.test import Client
from django.urls import reverse

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditAction, AuditEvent
from apps.matches.models import (
    FlagResolution,
    Match,
    MatchFlag,
    MatchFormat,
    MatchStatus,
)
from apps.matches.permissions import MODERATOR_GROUP
from apps.matches.services import record_match
from apps.matches.workflow import confirm_match, dispute_match
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db

CHANGELIST = "admin:matches_match_changelist"
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


def _run(
    client: Client,
    action: str,
    matches: list[Match],
    reason: str | None = REASON,
    *,
    follow: bool = True,
) -> tuple[int, list[Message], str]:
    data: dict[str, object] = {
        "action": action,
        "_selected_action": [str(m.pk) for m in matches],
    }
    if reason is not None:
        data["reason"] = reason
    response = client.post(reverse(CHANGELIST) + "?review=all", data, follow=follow)
    page = response.content.decode() if follow else ""
    return response.status_code, list(get_messages(response.wsgi_request)), page


def _status(match: Match) -> str:
    return Match.objects.get(pk=match.pk).status


def _edit_url(match: Match) -> str:
    return reverse("admin:matches_match_edit", args=[match.pk])


def _edit_data(players: list[Player], **overrides: str) -> dict[str, str]:
    data = {
        "reason": REASON,
        "played_on": PLAYED_ON.isoformat(),
        "team_a": f"{players[0].public_code}, {players[1].public_code}",
        "team_b": f"{players[2].public_code} {players[3].public_code}",
        "games": "11-8\n11-9",
    }
    data.update(overrides)
    return data


# The changelist and its default filter


def test_default_list_is_needs_review(
    moderator_client: Client, players: list[Player]
) -> None:
    others = [PlayerFactory.create() for _ in range(4)]
    plain = _confirmed(others)
    earlier, later = _pair(players)
    more = [PlayerFactory.create() for _ in range(4)]
    disputed = _disputed(more)

    page = moderator_client.get(reverse(CHANGELIST)).content.decode()

    assert earlier.public_code in page
    assert later.public_code in page
    assert disputed.public_code in page
    assert plain.public_code not in page


def test_all_filter_lists_every_match(
    moderator_client: Client, players: list[Player]
) -> None:
    plain = _confirmed(players)

    page = moderator_client.get(reverse(CHANGELIST) + "?review=all").content.decode()

    assert plain.public_code in page


def test_resolved_or_voided_partner_flags_leave_the_list(
    moderator_client: Client, players: list[Player], moderator: User
) -> None:
    earlier, later = _pair(players)
    _run(moderator_client, "void_selected", [later])

    page = moderator_client.get(reverse(CHANGELIST)).content.decode()

    assert earlier.public_code not in page
    assert later.public_code not in page


def test_list_shows_why_a_match_needs_review(
    moderator_client: Client, players: list[Player]
) -> None:
    _pair(players)
    _disputed([PlayerFactory.create() for _ in range(4)])

    page = moderator_client.get(reverse(CHANGELIST)).content.decode()

    assert "Possible duplicate" in page
    assert "Disputed" in page


def test_no_add_and_no_delete(moderator_client: Client, players: list[Player]) -> None:
    match = _confirmed(players)
    superuser = UserFactory.create(is_staff=True, is_superuser=True)
    moderator_client.force_login(superuser)

    page = moderator_client.get(reverse(CHANGELIST) + "?review=all").content.decode()
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


# Approve


def test_approve_resolves_a_dispute_as_confirmed(
    moderator_client: Client, players: list[Player], moderator: User
) -> None:
    match = _disputed(players)

    _status_code, messages, _page = _run(moderator_client, "approve_selected", [match])

    assert _status(match) == MatchStatus.CONFIRMED
    event = AuditEvent.objects.get(action=AuditAction.DISPUTE_OUTCOME)
    assert event.reason == REASON
    assert event.actor_user_id == moderator.pk
    assert any(match.public_code in str(m) or "1 match" in str(m) for m in messages)


def test_approve_resolves_open_flags_as_approved(
    moderator_client: Client, players: list[Player]
) -> None:
    earlier, later = _pair(players)

    _run(moderator_client, "approve_selected", [later])

    flag = MatchFlag.objects.get()
    assert flag.resolution == FlagResolution.APPROVED
    assert flag.resolved_at is not None
    assert earlier.is_counted()
    assert later.is_counted()
    event = AuditEvent.objects.get(action=AuditAction.MATCH_FLAG_RESOLVE)
    assert event.reason == REASON


def test_approve_disputed_and_flagged_does_both(
    moderator_client: Client, players: list[Player]
) -> None:
    earlier, later = _pair(players)
    dispute_match(later, actor=players[1].account)

    _run(moderator_client, "approve_selected", [later])

    assert _status(later) == MatchStatus.CONFIRMED
    assert MatchFlag.objects.get().resolution == FlagResolution.APPROVED
    assert later.is_counted()
    assert earlier.is_counted()


def test_approve_a_voided_match_changes_nothing_and_warns(
    moderator_client: Client, players: list[Player], moderator: User
) -> None:
    _earlier, later = _pair(players)
    _run(moderator_client, "void_selected", [later])
    before = AuditEvent.objects.count()

    _code, messages, _page = _run(moderator_client, "approve_selected", [later])

    assert AuditEvent.objects.count() == before
    assert MatchFlag.objects.get().resolved_at is None
    warnings = [str(m) for m in messages if m.level_tag == "warning"]
    assert any(later.public_code in text for text in warnings)


# Void


def test_void_a_disputed_match_resolves_the_dispute(
    moderator_client: Client, players: list[Player]
) -> None:
    match = _disputed(players)

    _run(moderator_client, "void_selected", [match])

    assert _status(match) == MatchStatus.VOIDED
    event = AuditEvent.objects.get(action=AuditAction.DISPUTE_OUTCOME)
    assert event.reason == REASON


def test_void_a_confirmed_match(
    moderator_client: Client, players: list[Player]
) -> None:
    match = _confirmed(players)

    _run(moderator_client, "void_selected", [match])

    assert _status(match) == MatchStatus.VOIDED
    event = AuditEvent.objects.get(action=AuditAction.MATCH_VOID)
    assert event.reason == REASON


def test_void_leaves_flags_open_and_releases_the_other(
    moderator_client: Client, players: list[Player]
) -> None:
    earlier, later = _pair(players)

    _run(moderator_client, "void_selected", [later])

    assert MatchFlag.objects.get().resolved_at is None
    assert earlier.is_counted()


def test_void_a_voided_match_warns_by_code(
    moderator_client: Client, players: list[Player]
) -> None:
    match = _confirmed(players)
    _run(moderator_client, "void_selected", [match])

    _code, messages, _page = _run(moderator_client, "void_selected", [match])

    assert AuditEvent.objects.filter(action=AuditAction.MATCH_VOID).count() == 1
    warnings = [str(m) for m in messages if m.level_tag == "warning"]
    assert any(match.public_code in text for text in warnings)


# Dismiss flags


def test_dismiss_flags_leaves_status_alone(
    moderator_client: Client, players: list[Player]
) -> None:
    earlier, later = _pair(players)
    dispute_match(later, actor=players[1].account)

    _run(moderator_client, "dismiss_flags_selected", [later])

    assert _status(later) == MatchStatus.DISPUTED
    flag = MatchFlag.objects.get()
    assert flag.resolution == FlagResolution.DISMISSED
    assert earlier.is_counted()
    assert not later.is_counted()
    event = AuditEvent.objects.get(action=AuditAction.MATCH_FLAG_RESOLVE)
    assert event.reason == REASON
    assert not AuditEvent.objects.filter(action=AuditAction.DISPUTE_OUTCOME).exists()


def test_dismiss_with_no_open_flag_warns(
    moderator_client: Client, players: list[Player]
) -> None:
    match = _confirmed(players)

    _code, messages, _page = _run(moderator_client, "dismiss_flags_selected", [match])

    assert not AuditEvent.objects.filter(action=AuditAction.MATCH_FLAG_RESOLVE).exists()
    warnings = [str(m) for m in messages if m.level_tag == "warning"]
    assert any(match.public_code in text for text in warnings)


# The reason rule


@pytest.mark.parametrize(
    "action", ["approve_selected", "void_selected", "dismiss_flags_selected"]
)
@pytest.mark.parametrize("reason", [None, "", "  \t "])
def test_blank_reason_changes_nothing(
    moderator_client: Client, players: list[Player], action: str, reason: str | None
) -> None:
    _earlier, later = _pair(players)
    dispute_match(later, actor=players[1].account)
    before = AuditEvent.objects.count()

    _code, messages, _page = _run(moderator_client, action, [later], reason)

    assert _status(later) == MatchStatus.DISPUTED
    assert MatchFlag.objects.get().resolved_at is None
    assert AuditEvent.objects.count() == before
    assert len(messages) == 1
    assert messages[0].level_tag == "warning"
    assert "reason" in str(messages[0]).lower()


def test_reason_is_stored_stripped(
    moderator_client: Client, players: list[Player]
) -> None:
    match = _confirmed(players)

    _run(moderator_client, "void_selected", [match], "  Made-up spaced reason.  ")

    event = AuditEvent.objects.get(action=AuditAction.MATCH_VOID)
    assert event.reason == "Made-up spaced reason."


# Edit


def test_edit_action_opens_the_edit_page_for_one_match(
    moderator_client: Client, players: list[Player]
) -> None:
    match = _confirmed(players)

    code, _messages, _page = _run(
        moderator_client, "edit_selected", [match], follow=False
    )

    assert code == 302
    response = moderator_client.post(
        reverse(CHANGELIST) + "?review=all",
        {"action": "edit_selected", "_selected_action": [str(match.pk)]},
    )
    assert response.status_code == 302
    assert response["Location"] == _edit_url(match)


def test_edit_action_refuses_more_than_one(
    moderator_client: Client, players: list[Player]
) -> None:
    earlier, later = _pair(players)

    _code, messages, _page = _run(moderator_client, "edit_selected", [earlier, later])

    assert [m.level_tag for m in messages] == ["warning"]
    assert _status(earlier) == MatchStatus.CONFIRMED


def test_edit_page_shows_the_current_values(
    moderator_client: Client, players: list[Player]
) -> None:
    match = _confirmed(players)

    page = moderator_client.get(_edit_url(match)).content.decode()

    assert 'name="reason"' in page
    assert 'name="games"' in page
    assert "csrfmiddlewaretoken" in page
    assert PLAYED_ON.isoformat() in page
    for player in players:
        assert player.public_code in page
    assert "11-7" in page
    assert "9-11" in page


def test_edit_corrects_the_match(
    moderator_client: Client, players: list[Player], moderator: User
) -> None:
    match = _confirmed(players)

    response = moderator_client.post(_edit_url(match), _edit_data(players))

    assert _status(match) == MatchStatus.VOIDED
    corrected = Match.objects.get(corrects=match)
    assert corrected.status == MatchStatus.CONFIRMED
    assert [
        (g.team_a_score, g.team_b_score)
        for g in corrected.games.order_by("game_number")
    ] == [(11, 8), (11, 9)]
    assert corrected.format == match.format
    assert response.status_code == 302
    assert response["Location"] == reverse(
        "admin:matches_match_change", args=[corrected.pk]
    )
    event = AuditEvent.objects.get(action=AuditAction.MATCH_EDIT)
    assert event.reason == REASON
    assert event.actor_user_id == moderator.pk


def test_edit_a_disputed_match(moderator_client: Client, players: list[Player]) -> None:
    match = _disputed(players)

    moderator_client.post(_edit_url(match), _edit_data(players))

    assert _status(match) == MatchStatus.VOIDED
    assert Match.objects.get(corrects=match).status == MatchStatus.CONFIRMED


def test_edit_can_swap_a_player(
    moderator_client: Client, players: list[Player]
) -> None:
    match = _confirmed(players)
    newcomer = PlayerFactory.create()

    moderator_client.post(
        _edit_url(match),
        _edit_data(players, team_b=f"{players[2].public_code}, {newcomer.public_code}"),
    )

    corrected = Match.objects.get(corrects=match)
    assert set(corrected.participants.values_list("player_id", flat=True)) == {
        players[0].pk,
        players[1].pk,
        players[2].pk,
        newcomer.pk,
    }


def test_edit_validation_error_shows_on_the_form(
    moderator_client: Client, players: list[Player]
) -> None:
    match = _confirmed(players)

    response = moderator_client.post(
        _edit_url(match), _edit_data(players, games="11-10")
    )

    assert response.status_code == 200
    assert "Game 1: the winner must win by 2 points." in response.content.decode()
    assert _status(match) == MatchStatus.CONFIRMED
    assert not Match.objects.filter(corrects=match).exists()
    assert not AuditEvent.objects.filter(action=AuditAction.MATCH_EDIT).exists()


@pytest.mark.parametrize("games", ["eleven to seven", "11-7-3", "11 7\n", "-1-11"])
def test_edit_unreadable_games_get_fixed_text(
    moderator_client: Client, players: list[Player], games: str
) -> None:
    match = _confirmed(players)

    response = moderator_client.post(_edit_url(match), _edit_data(players, games=games))

    page = response.content.decode()
    assert response.status_code == 200
    assert "Enter each game on its own line as two scores, like 11-7." in page
    assert _status(match) == MatchStatus.CONFIRMED


@pytest.mark.parametrize("code", ["P-ZZZZ-ZZZZ", "not a code", "M-0000-0000-0000"])
def test_edit_unknown_codes_get_fixed_text(
    moderator_client: Client, players: list[Player], code: str
) -> None:
    match = _confirmed(players)

    response = moderator_client.post(
        _edit_url(match),
        _edit_data(players, team_a=f"{players[0].public_code}, {code}"),
    )

    page = response.content.decode()
    assert response.status_code == 200
    assert "Enter the OPEN Codes of existing players." in page
    assert _status(match) == MatchStatus.CONFIRMED


@pytest.mark.parametrize("reason", ["", "   "])
def test_edit_needs_a_reason(
    moderator_client: Client, players: list[Player], reason: str
) -> None:
    match = _confirmed(players)

    response = moderator_client.post(
        _edit_url(match), _edit_data(players, reason=reason)
    )

    assert response.status_code == 200
    assert _status(match) == MatchStatus.CONFIRMED
    assert not AuditEvent.objects.filter(action=AuditAction.MATCH_EDIT).exists()


def test_edit_duplicate_error_shows_on_the_form(
    moderator_client: Client, players: list[Player]
) -> None:
    earlier, later = _pair(players)

    response = moderator_client.post(
        _edit_url(later), _edit_data(players, games="11-7\n9-11\n11-4")
    )

    page = response.content.decode()
    assert response.status_code == 200
    assert f"This match was already submitted as {earlier.public_code}." in page
    assert _status(later) == MatchStatus.CONFIRMED
    assert not Match.objects.filter(corrects=later).exists()


def test_edit_a_voided_match_shows_the_state_error(
    moderator_client: Client, players: list[Player]
) -> None:
    match = _confirmed(players)
    _run(moderator_client, "void_selected", [match])

    response = moderator_client.post(_edit_url(match), _edit_data(players))

    assert response.status_code == 200
    assert (
        "This match cannot make this change in its current status."
        in response.content.decode()
    )
    assert not Match.objects.filter(corrects=match).exists()


def test_edit_page_for_a_missing_match_is_404(moderator_client: Client) -> None:
    for object_id in ("999999", "abc"):
        url = reverse("admin:matches_match_edit", args=[object_id])
        assert moderator_client.get(url).status_code == 404


def test_edit_keeps_played_at_only_on_the_same_day(
    moderator_client: Client, players: list[Player]
) -> None:
    match = _confirmed(players)

    moderator_client.post(_edit_url(match), _edit_data(players, played_on="2026-05-03"))

    corrected = Match.objects.get(corrects=match)
    assert corrected.played_on == date(2026, 5, 3)
    assert corrected.played_at is None


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
    secret = [
        minor.display_name,
        minor.legal_name,
        *(p.display_name for p in adults),
        *(p.account.email for p in players),
    ]

    pages = [
        moderator_client.get(reverse(CHANGELIST)).content.decode(),
        moderator_client.get(reverse(CHANGELIST) + "?review=all").content.decode(),
        moderator_client.get(
            reverse("admin:matches_match_change", args=[later.pk])
        ).content.decode(),
        moderator_client.get(
            reverse("admin:matches_match_change", args=[earlier.pk])
        ).content.decode(),
        moderator_client.get(_edit_url(later)).content.decode(),
        moderator_client.post(
            _edit_url(later), _edit_data(players, games="11-10")
        ).content.decode(),
    ]
    for action in ("dismiss_flags_selected", "approve_selected", "void_selected"):
        _code, messages, page = _run(moderator_client, action, [later])
        pages.append(page)
        pages.append(" ".join(str(m) for m in messages))
    _code, messages, page = _run(moderator_client, "void_selected", [later])
    pages.append(page)
    pages.append(" ".join(str(m) for m in messages))

    for text in pages:
        for value in secret:
            assert value not in text
