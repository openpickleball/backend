"""Dispute and hold notices (#272).

When a match becomes disputed, or a possible duplicate flag holds a
match, the Accounts that can act on it get one email each, queued after
the change commits: the other team's players for a dispute, the players
of a self-reported match or the Event's verified Organizer's members for
a hold, and those members for a dispute on an Event match. Made-up data
only.
"""

import logging
from collections.abc import Callable
from datetime import UTC, date, timedelta
from typing import Any

import pytest
from django.conf import settings
from django.contrib.auth.models import Permission
from django.core import mail
from django.db import transaction
from django.utils.dateformat import format as date_format

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.clubs.models import MemberRole
from apps.clubs.services import unverify_organizer
from apps.clubs.tests.factories import ClubFactory, OrganizerMemberFactory
from apps.events.models import Event
from apps.matches import notices
from apps.matches.models import (
    FlagReason,
    Game,
    Match,
    MatchFlag,
    MatchFormat,
    MatchParticipant,
    MatchStatus,
    Team,
)
from apps.matches.services import record_match
from apps.matches.tests.conftest import WORKFLOW_GAMES, WORKFLOW_PLAYED_ON
from apps.matches.tests.factories import MatchFactory, raw_update
from apps.matches.workflow import (
    confirm_match,
    dispute_match,
    dispute_started_at,
    withdraw_dispute,
)
from apps.players.models import Player

pytestmark = pytest.mark.django_db

REASON = "Made-up reason for a test."
OTHER_GAMES = ((11, 2), (11, 3))
Capture = Callable[..., Any]


def _sent() -> dict[str, list[str]]:
    """Each recipient's subjects, from the emails sent so far."""
    sent: dict[str, list[str]] = {}
    for message in mail.outbox:
        [to] = message.to
        sent.setdefault(to, []).append(str(message.subject))
    return sent


def _body(address: str, code: str) -> str:
    [body] = [
        str(message.body)
        for message in mail.outbox
        if message.to == [address] and code in str(message.body)
    ]
    return body


def _day(value: date) -> str:
    return date_format(value, "F j, Y")


def _member(event: Event, account: User | None = None) -> User:
    member: User = account or UserFactory.create()
    OrganizerMemberFactory.create(
        organizer=event.organizer, account=member, role=MemberRole.MATCH_MANAGER
    )
    return member


def _self_reported(
    players: list[Player], games: tuple[tuple[int, int], ...] = WORKFLOW_GAMES
) -> Match:
    return record_match(
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=games,
        played_on=WORKFLOW_PLAYED_ON,
        submitted_by_user=players[0].account,
    ).match


def _event_match(
    players: list[Player],
    event: Event,
    submitter: User,
    games: tuple[tuple[int, int], ...] = WORKFLOW_GAMES,
) -> Match:
    return record_match(
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=games,
        played_on=WORKFLOW_PLAYED_ON,
        submitted_by_user=submitter,
        event=event,
    ).match


def _deactivate(account: User) -> None:
    account.is_active = False
    account.save(update_fields=["is_active"])


# Disputes


def test_dispute_emails_the_other_team_only(
    players: list[Player], confirmed: Match, django_capture_on_commit_callbacks: Capture
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        dispute_match(confirmed, actor=players[0].account)

    subject = "A match you played in was disputed on OPEN"
    assert _sent() == {
        players[2].account.email: [subject],
        players[3].account.email: [subject],
    }
    body = _body(players[2].account.email, str(confirmed.public_code))
    started = dispute_started_at(confirmed)
    assert started is not None
    ends_on = (started + timedelta(days=settings.MATCH_DISPUTE_EXPIRY_DAYS)).date()
    assert _day(ends_on) in body
    assert "7-11, 11-9, 4-11" in body
    assert "Scores show your team first." in body


def test_dispute_skips_an_inactive_account(
    players: list[Player], pending: Match, django_capture_on_commit_callbacks: Capture
) -> None:
    _deactivate(players[3].account)

    with django_capture_on_commit_callbacks(execute=True):
        dispute_match(pending, actor=players[2].account)

    assert set(_sent()) == {players[0].account.email, players[1].account.email}


def test_a_second_or_repeat_dispute_sends_nothing(
    players: list[Player], confirmed: Match, django_capture_on_commit_callbacks: Capture
) -> None:
    dispute_match(confirmed, actor=players[0].account)

    with django_capture_on_commit_callbacks(execute=True):
        dispute_match(confirmed, actor=players[2].account)
        dispute_match(confirmed, actor=players[0].account)

    assert mail.outbox == []


def test_a_rolled_back_dispute_sends_nothing(
    players: list[Player], confirmed: Match, django_capture_on_commit_callbacks: Capture
) -> None:
    class Rollback(Exception):
        pass

    def dispute_then_fail() -> None:
        with transaction.atomic():
            dispute_match(confirmed, actor=players[0].account)
            raise Rollback

    with (
        django_capture_on_commit_callbacks(execute=True),
        pytest.raises(Rollback),
    ):
        dispute_then_fail()

    assert mail.outbox == []
    assert Match.objects.get(pk=confirmed.pk).status == MatchStatus.CONFIRMED


def test_a_dispute_on_an_event_match_also_emails_free_organizer_members(
    players: list[Player], event: Event, django_capture_on_commit_callbacks: Capture
) -> None:
    free = _member(event)
    plays_other_team = _member(event, players[2].account)
    plays_own_team = _member(event, players[1].account)
    inactive = _member(event)
    _deactivate(inactive)
    match = _event_match(players, event, free)

    with django_capture_on_commit_callbacks(execute=True):
        dispute_match(match, actor=players[0].account)

    player_subject = "A match you played in was disputed on OPEN"
    organizer_subject = "A match from your organizer's event was disputed on OPEN"
    assert _sent() == {
        plays_other_team.email: [player_subject],
        players[3].account.email: [player_subject],
        free.email: [organizer_subject],
    }
    assert plays_own_team.email not in _sent()
    code = str(match.public_code)
    assert "Scores show team A first." in _body(free.email, code)
    assert "11-7, 9-11, 11-4" in _body(free.email, code)
    assert "The event's organizer can settle it." in _body(
        players[3].account.email, code
    )


def test_an_unverified_organizers_members_get_nothing(
    players: list[Player], event: Event, django_capture_on_commit_callbacks: Capture
) -> None:
    member = _member(event)
    match = _event_match(players, event, member)
    verifier = UserFactory.create()
    verifier.user_permissions.add(
        Permission.objects.get(
            content_type__app_label="clubs", codename="verify_organizer"
        )
    )
    unverify_organizer(event.organizer, actor=verifier, reason=REASON)

    with django_capture_on_commit_callbacks(execute=True):
        dispute_match(match, actor=players[0].account)

    assert set(_sent()) == {players[2].account.email, players[3].account.email}


def test_a_dispute_settled_before_the_callback_sends_nothing(
    players: list[Player], confirmed: Match, django_capture_on_commit_callbacks: Capture
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        dispute_match(confirmed, actor=players[0].account)
        withdraw_dispute(confirmed, actor=players[0].account)

    assert mail.outbox == []


def test_a_notice_failure_never_fails_the_dispute(
    players: list[Player],
    confirmed: Match,
    django_capture_on_commit_callbacks: Capture,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("made-up secret text")

    monkeypatch.setattr(notices, "render_dispute_notice", boom)

    with (
        caplog.at_level(logging.WARNING, logger="apps.matches.notices"),
        django_capture_on_commit_callbacks(execute=True),
    ):
        dispute_match(confirmed, actor=players[0].account)

    assert Match.objects.get(pk=confirmed.pk).status == MatchStatus.DISPUTED
    assert mail.outbox == []
    records = [r for r in caplog.records if r.name == "apps.matches.notices"]
    # One line for each of the two recipients on the other team.
    assert [r.getMessage() for r in records] == [
        f"Could not queue notices for match {confirmed.pk} (RuntimeError)."
    ] * 2
    assert all(r.exc_info is None for r in records)
    assert "made-up secret text" not in caplog.text


# Holds


def _hold_ends_on(match: Match) -> date:
    created = max(
        MatchFlag.objects.filter(match=match).values_list("created_at", flat=True)
    )
    return (
        created.astimezone(UTC) + timedelta(days=settings.MATCH_DUPLICATE_HOLD_DAYS)
    ).date()


def test_a_near_duplicate_emails_the_players_of_both_matches(
    players: list[Player], confirmed: Match, django_capture_on_commit_callbacks: Capture
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        newer = _self_reported(players, OTHER_GAMES)

    subject = "A match you played in is on hold on OPEN"
    assert _sent() == {player.account.email: [subject, subject] for player in players}
    for player in players:
        for held in (newer, confirmed):
            body = _body(player.account.email, str(held.public_code))
            assert _day(_hold_ends_on(newer)) in body
    assert "2-11, 3-11" in _body(players[2].account.email, str(newer.public_code))
    assert str(confirmed.public_code) not in _body(
        players[0].account.email, str(newer.public_code)
    )


def test_an_event_match_hold_emails_free_organizer_members(
    players: list[Player], event: Event, django_capture_on_commit_callbacks: Capture
) -> None:
    free = _member(event)
    _member(event, players[0].account)
    earlier = _event_match(players, event, free)

    with django_capture_on_commit_callbacks(execute=True):
        newer = _self_reported(players, OTHER_GAMES)

    sent = _sent()
    assert sent[free.email] == [
        "A match from your organizer's event is on hold on OPEN"
    ]
    assert str(earlier.public_code) in _body(free.email, str(earlier.public_code))
    for player in players:
        assert sent[player.account.email] == [
            "A match you played in is on hold on OPEN"
        ]
        assert _body(player.account.email, str(newer.public_code))
    assert len(mail.outbox) == 5


def test_a_club_only_match_hold_emails_nobody_for_it(
    players: list[Player], django_capture_on_commit_callbacks: Capture
) -> None:
    legacy = MatchFactory.create(
        club=ClubFactory.create(),
        format=MatchFormat.DOUBLES,
        best_of=1,
        played_on=WORKFLOW_PLAYED_ON,
        status=MatchStatus.CONFIRMED,
        participants=False,
        games=False,
    )
    for player, team in zip(players, (Team.A, Team.A, Team.B, Team.B), strict=True):
        MatchParticipant.objects.create(match=legacy, player=player, team=team)
    Game.objects.create(match=legacy, game_number=1, team_a_score=11, team_b_score=1)

    with django_capture_on_commit_callbacks(execute=True):
        newer = _self_reported(players, OTHER_GAMES)

    assert MatchFlag.objects.filter(
        match=newer, duplicate_of=legacy, reason=FlagReason.POSSIBLE_DUPLICATE
    ).exists()
    assert len(mail.outbox) == 4
    for message in mail.outbox:
        assert str(legacy.public_code) not in str(message.body)


def test_a_hold_ended_before_the_callback_sends_nothing(
    players: list[Player],
    confirmed: Match,
    django_capture_on_commit_callbacks: Capture,
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        _self_reported(players, OTHER_GAMES)
        raw_update(confirmed, status=MatchStatus.VOIDED)

    assert mail.outbox == []


def test_a_hold_skips_an_inactive_account(
    players: list[Player], confirmed: Match, django_capture_on_commit_callbacks: Capture
) -> None:
    _deactivate(players[1].account)

    with django_capture_on_commit_callbacks(execute=True):
        _self_reported(players, OTHER_GAMES)

    assert players[1].account.email not in _sent()
    assert len(mail.outbox) == 6


def test_a_hold_notice_failure_never_fails_the_submission(
    players: list[Player],
    confirmed: Match,
    django_capture_on_commit_callbacks: Capture,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("made-up secret text")

    monkeypatch.setattr(notices, "render_hold_notice", boom)

    with (
        caplog.at_level(logging.WARNING, logger="apps.matches.notices"),
        django_capture_on_commit_callbacks(execute=True),
    ):
        newer = _self_reported(players, OTHER_GAMES)

    assert Match.objects.filter(pk=newer.pk).exists()
    assert mail.outbox == []
    assert "made-up secret text" not in caplog.text
    assert f"match {newer.pk} (RuntimeError)" in caplog.text


def test_no_flag_no_hold_notice(
    players: list[Player], django_capture_on_commit_callbacks: Capture
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        match = _self_reported(players)
        confirm_match(match, actor=players[2].account)

    assert mail.outbox == []
