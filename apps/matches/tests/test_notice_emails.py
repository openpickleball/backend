"""The dispute and hold notice emails (#272).

Each notice tells one Account about one match: its OPEN Code, date,
scores from the recipient's side (team A first for an Organizer member,
who plays on no side), the date the dispute or hold ends on its own, and
a link to it in the web app. It names no player. Made-up data only.
"""

from datetime import date
from pathlib import Path

import pytest
from django.conf import settings
from django.core.mail import EmailMultiAlternatives

from apps.accounts.tests.factories import UserFactory
from apps.emails.services import queue_email
from apps.emails.tests.hygiene import assert_clean_message, assert_clean_template
from apps.events.models import Event
from apps.events.tests.factories import EventFactory
from apps.matches.emails import (
    DISPUTE_NOTICE,
    HOLD_NOTICE,
    render_dispute_notice,
    render_hold_notice,
)
from apps.matches.models import (
    Game,
    Match,
    MatchFormat,
    MatchParticipant,
    MatchStatus,
    Team,
)
from apps.matches.tests.factories import MatchFactory
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db

FRONTEND_URL: str = settings.FRONTEND_URL
ENDS_ON = date(2026, 7, 1)
TEMPLATES_DIR = Path(settings.BASE_DIR) / "templates" / "matches" / "email"


def _match(
    players: list[Player],
    scores: list[tuple[int, int]],
    status: MatchStatus = MatchStatus.DISPUTED,
    event: Event | None = None,
) -> Match:
    """A doubles match of these four players (A, A, B, B) in ``status``."""
    match = MatchFactory.create(
        event=event,
        format=MatchFormat.DOUBLES,
        best_of=1 if len(scores) == 1 else 3,
        played_on=date(2026, 5, 3),
        status=status,
        participants=False,
        games=False,
    )
    for player, team in zip(players, (Team.A, Team.A, Team.B, Team.B), strict=True):
        MatchParticipant.objects.create(match=match, player=player, team=team)
    for number, (a, b) in enumerate(scores, start=1):
        Game.objects.create(
            match=match, game_number=number, team_a_score=a, team_b_score=b
        )
    return match


def _players() -> list[Player]:
    return [
        PlayerFactory.create(
            display_name=f"Quillon Made-up {letter}.",
            legal_name=f"Quillon Made-up {letter}one",
        )
        for letter in "WXYZ"
    ]


def _html(message: EmailMultiAlternatives) -> str:
    [(html, mimetype)] = message.alternatives
    assert mimetype == "text/html"
    return str(html)


def _parts(message: EmailMultiAlternatives) -> list[str]:
    return [str(message.subject), str(message.body), _html(message)]


def _link(match: Match) -> str:
    return f"{FRONTEND_URL}/matches/{match.public_code}"


def test_dispute_notice_for_a_player() -> None:
    players = _players()
    match = _match(players, [(11, 7), (9, 11), (11, 5)])
    me = players[2]

    message = render_dispute_notice(me.account, match, team=Team.B, ends_on=ENDS_ON)

    assert message.to == [me.account.email]
    assert message.subject == "A match you played in was disputed on OPEN"
    for body in (message.body, _html(message)):
        assert str(match.public_code) in body
        assert "May 3, 2026" in body
        assert "7-11, 11-9, 5-11" in body
        assert "July 1, 2026" in body
        assert "Scores show your team first." in body
        assert "Either team can correct the scores" in body
        assert (
            "You received this email because a match you played in was "
            "disputed on OPEN." in body
        )
    assert _link(match) in message.body
    assert f'href="{_link(match)}"' in _html(message)


def test_dispute_notice_for_an_event_match_says_the_organizer_settles() -> None:
    players = _players()
    event_match = _match(players, [(11, 7)], event=EventFactory.create())

    message = render_dispute_notice(
        players[0].account, event_match, team=Team.A, ends_on=ENDS_ON
    )

    for body in (message.body, _html(message)):
        assert "The event's organizer can settle it." in body
        assert "Either team can correct" not in body


def test_dispute_notice_for_an_organizer_member() -> None:
    players = _players()
    match = _match(players, [(7, 11), (11, 9), (5, 11)])
    member = UserFactory.create()

    message = render_dispute_notice(member, match, team=None, ends_on=ENDS_ON)

    assert message.to == [member.email]
    assert message.subject == (
        "A match from your organizer's event was disputed on OPEN"
    )
    for body in (message.body, _html(message)):
        assert "7-11, 11-9, 5-11" in body
        assert "Scores show team A first." in body
        assert "you can confirm it, void it, or correct it" in body
        assert (
            "You received this email because you are a member of the "
            "organizer that ran this match's event." in body
        )


def test_hold_notice_for_a_player() -> None:
    players = _players()
    match = _match(players, [(11, 7)], status=MatchStatus.PENDING)
    me = players[3]

    message = render_hold_notice(me.account, match, team=Team.B, ends_on=ENDS_ON)

    assert message.subject == "A match you played in is on hold on OPEN"
    for body in (message.body, _html(message)):
        assert str(match.public_code) in body
        assert "7-11" in body
        assert "July 1, 2026" in body
        assert "same teams and date as another match" in body
        assert "One player from each team needs to vouch." in body
        assert "the earlier of the two matches is confirmed" in body
        assert (
            "You received this email because a match you played in is on "
            "hold on OPEN." in body
        )
    assert _link(match) in message.body


def test_hold_notice_for_an_organizer_member() -> None:
    players = _players()
    match = _match(players, [(7, 11)], status=MatchStatus.CONFIRMED)
    member = UserFactory.create()

    message = render_hold_notice(member, match, team=None, ends_on=ENDS_ON)

    assert message.subject == "A match from your organizer's event is on hold on OPEN"
    for body in (message.body, _html(message)):
        assert "7-11" in body
        assert "Scores show team A first." in body
        assert "a member of your organizer can vouch for it" in body


def test_no_player_is_named() -> None:
    players = _players()
    match = _match(players, [(11, 7)])
    hold = _match(_players(), [(11, 7)], status=MatchStatus.PENDING)
    messages = [
        render_dispute_notice(players[0].account, match, team=Team.A, ends_on=ENDS_ON),
        render_dispute_notice(UserFactory.create(), match, team=None, ends_on=ENDS_ON),
        render_hold_notice(
            hold.participants.first().player.account,  # type: ignore[union-attr]
            hold,
            team=Team.A,
            ends_on=ENDS_ON,
        ),
    ]

    everyone = list(Player.objects.all())
    for message in messages:
        for part in _parts(message):
            for player in everyone:
                assert player.display_name not in part
                assert player.legal_name not in part
                assert player.account.email not in part


def test_messages_are_clean_and_queueable() -> None:
    players = _players()
    match = _match(players, [(11, 7)])
    dispute = render_dispute_notice(
        players[0].account, match, team=Team.A, ends_on=ENDS_ON
    )
    hold = render_hold_notice(players[0].account, match, team=None, ends_on=ENDS_ON)

    for message, prefix in ((dispute, DISPUTE_NOTICE), (hold, HOLD_NOTICE)):
        assert_clean_message(message)
        assert queue_email(message, template=prefix).html


def test_template_files_are_clean() -> None:
    files = sorted(TEMPLATES_DIR.glob("*_notice_*"))

    assert len(files) == 6
    for path in files:
        assert_clean_template(path)


def test_refuses_an_inactive_account() -> None:
    players = _players()
    match = _match(players, [(11, 7)])
    account = players[0].account
    account.is_active = False

    with pytest.raises(ValueError, match="active account") as dispute:
        render_dispute_notice(account, match, team=Team.A, ends_on=ENDS_ON)
    with pytest.raises(ValueError, match="active account") as hold:
        render_hold_notice(account, match, team=Team.A, ends_on=ENDS_ON)

    assert str(dispute.value) == "A dispute notice needs an active account."
    assert str(hold.value) == "A hold notice needs an active account."


def test_refuses_a_match_with_no_games() -> None:
    players = _players()
    match = _match(players, [])

    with pytest.raises(ValueError, match="with games") as dispute:
        render_dispute_notice(players[0].account, match, team=Team.A, ends_on=ENDS_ON)
    with pytest.raises(ValueError, match="with games") as hold:
        render_hold_notice(players[0].account, match, team=Team.A, ends_on=ENDS_ON)

    assert str(dispute.value) == "A dispute notice needs a match with games."
    assert str(hold.value) == "A hold notice needs a match with games."
    assert str(match.public_code) not in str(dispute.value)


@pytest.mark.parametrize(
    "status", [MatchStatus.PENDING, MatchStatus.CONFIRMED, MatchStatus.VOIDED]
)
def test_dispute_notice_refuses_a_match_that_is_not_disputed(
    status: MatchStatus,
) -> None:
    players = _players()
    match = _match(players, [(11, 7)], status=status)

    with pytest.raises(ValueError, match="disputed matches") as caught:
        render_dispute_notice(players[0].account, match, team=Team.A, ends_on=ENDS_ON)

    assert str(caught.value) == "A dispute notice is only for disputed matches."


def test_hold_notice_refuses_a_voided_match() -> None:
    players = _players()
    match = _match(players, [(11, 7)], status=MatchStatus.VOIDED)

    with pytest.raises(ValueError, match="voided match") as caught:
        render_hold_notice(players[0].account, match, team=Team.A, ends_on=ENDS_ON)

    assert str(caught.value) == "A hold notice is not for a voided match."
