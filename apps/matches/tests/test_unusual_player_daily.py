"""The per-player daily matches rule (#207, ADR 0052).

A new match with an Event is flagged ``unusual_submission`` with rule
``player_daily_matches`` when, once it is recorded, any of its players has
more than ``UNUSUAL_SUBMISSION_PLAYER_DAILY_MATCHES`` matches on its
``played_on``: every match of the player that day but voided ones,
self-reported ones included, a correction counting in its voided
original's place. One flag per match, however many
players are past the number. The flag is a signal only. Made-up data only.
"""

from datetime import date, timedelta
from typing import Any

import pytest
from pytest_django.fixtures import Settings

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditAction, AuditEvent
from apps.clubs.models import Organizer
from apps.clubs.reports import organizer_review
from apps.clubs.tests.factories import OrganizerMemberFactory, verified_organizer
from apps.emails.models import QueuedEmail
from apps.events.models import Event
from apps.events.tests.factories import EventFactory
from apps.matches.models import (
    FlagReason,
    FlagRule,
    Match,
    MatchFlag,
    MatchFormat,
    MatchStatus,
)
from apps.matches.services import record_match
from apps.matches.tests.conftest import SignalLog
from apps.matches.workflow import edit_match, void_match
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory
from apps.ratings.models import RatingReplayRequest, RatingState

pytestmark = pytest.mark.django_db

DAY = date(2026, 5, 9)
CAP = 2
_scores = iter(range(10_000))


@pytest.fixture(autouse=True)
def thresholds(settings: Settings) -> None:
    """Only this rule can fire past its cap; the others stay quiet."""
    settings.UNUSUAL_SUBMISSION_FIRST_MATCHES = 1
    settings.UNUSUAL_SUBMISSION_BATCH_SIZE = 1_000
    settings.UNUSUAL_SUBMISSION_DAILY_MATCHES = 1_000
    settings.UNUSUAL_SUBMISSION_PLAYER_DAILY_MATCHES = CAP


@pytest.fixture
def organizer() -> Organizer:
    return verified_organizer()


@pytest.fixture
def event(organizer: Organizer) -> Event:
    made: Event = EventFactory.create(organizer=organizer)
    return made


@pytest.fixture
def member(organizer: Organizer) -> User:
    account: User = UserFactory.create()
    OrganizerMemberFactory.create(organizer=organizer, account=account)
    return account


@pytest.fixture
def player() -> Player:
    made: Player = PlayerFactory.create()
    return made


def _games() -> tuple[tuple[int, int], ...]:
    """Scores that differ each call, so no two matches are duplicates."""
    loser = next(_scores) % 10
    return ((11, loser), (11, 9 - loser if loser < 9 else 0))


def _record(
    event: Event | None,
    submitter: Any,
    team_a: list[Player],
    *,
    team_b: list[Player] | None = None,
    played_on: date = DAY,
) -> Match:
    return record_match(
        format=MatchFormat.SINGLES,
        team_a=team_a,
        team_b=team_b or [PlayerFactory.create()],
        games=_games(),
        played_on=played_on,
        event=event,
        submitted_by_user=submitter,
    ).match


def _rules(match: Match) -> list[str]:
    return sorted(
        match.flags.filter(
            reason=FlagReason.UNUSUAL_SUBMISSION, rule=FlagRule.PLAYER_DAILY_MATCHES
        ).values_list("rule", flat=True)
    )


def _match_events() -> int:
    """Audit Events but rating changes, which depend on each player's
    earlier ratings."""
    return AuditEvent.objects.exclude(action=AuditAction.RATING_CHANGE).count()


def _counted(match: Match) -> bool:
    return Match.objects.counted().filter(pk=match.pk).exists()


def test_the_match_past_the_cap_and_later_ones_are_flagged(
    event: Event, member: User, player: Player
) -> None:
    matches = [_record(event, member, [player]) for _ in range(CAP + 2)]

    assert [_rules(match) for match in matches] == [
        [],
        [],
        [FlagRule.PLAYER_DAILY_MATCHES],
        [FlagRule.PLAYER_DAILY_MATCHES],
    ]
    assert all(match.status == MatchStatus.CONFIRMED for match in matches)
    assert all(_counted(match) for match in matches)


def test_a_player_on_the_other_team_counts_too(
    event: Event, member: User, player: Player
) -> None:
    for _ in range(CAP):
        _record(event, member, [PlayerFactory.create()], team_b=[player])

    match = _record(event, member, [PlayerFactory.create()], team_b=[player])

    assert _rules(match) == [FlagRule.PLAYER_DAILY_MATCHES]


def test_one_flag_when_several_players_are_past(
    event: Event, member: User, player: Player
) -> None:
    other = PlayerFactory.create()
    for _ in range(CAP):
        _record(event, member, [player])
        _record(event, member, [other])

    match = _record(event, member, [player], team_b=[other])

    assert _rules(match) == [FlagRule.PLAYER_DAILY_MATCHES]
    assert match.flags.filter(rule=FlagRule.PLAYER_DAILY_MATCHES).count() == 1


def test_other_days_do_not_count(event: Event, member: User, player: Player) -> None:
    for offset in range(1, CAP + 2):
        _record(event, member, [player], played_on=DAY - timedelta(days=offset))
    _record(event, member, [player])

    assert _rules(_record(event, member, [player])) == []


def test_voided_matches_do_not_count(
    event: Event, member: User, player: Player
) -> None:
    first = _record(event, member, [player])
    void_match(first, actor=member)
    _record(event, member, [player])

    assert _rules(_record(event, member, [player])) == []


def test_a_correction_counts_once_and_is_never_flagged(
    event: Event, member: User, player: Player
) -> None:
    original = _record(event, member, [player])
    opponent = original.participants.exclude(player=player).get().player
    correction = edit_match(
        original,
        actor=member,
        format=MatchFormat.SINGLES,
        team_a=[player],
        team_b=[opponent],
        games=((11, 1), (11, 2)),
        played_on=DAY,
    )
    # The voided original and its correction count once together.
    assert _rules(_record(event, member, [player])) == []
    assert _rules(_record(event, member, [player])) == [FlagRule.PLAYER_DAILY_MATCHES]

    second = edit_match(
        correction,
        actor=member,
        format=MatchFormat.SINGLES,
        team_a=[player],
        team_b=[opponent],
        games=((11, 3), (11, 2)),
        played_on=DAY,
    )

    # Past the cap, but a correction is never flagged.
    assert _rules(second) == []


def test_self_reported_matches_count(
    event: Event, member: User, player: Player
) -> None:
    for _ in range(CAP):
        pending = _record(None, player.account, [player])
        assert pending.status == MatchStatus.PENDING

    assert _rules(_record(event, member, [player])) == [FlagRule.PLAYER_DAILY_MATCHES]


def test_a_self_submission_past_the_cap_is_not_flagged(
    event: Event, member: User, player: Player
) -> None:
    for _ in range(CAP):
        _record(event, member, [player])

    pending = _record(None, player.account, [player])

    assert not MatchFlag.objects.filter(match=pending).exists()


def test_the_flag_is_a_signal_only(
    event: Event,
    member: User,
    player: Player,
    signals: SignalLog,
    django_capture_on_commit_callbacks: Any,
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        for _ in range(CAP):
            _record(event, member, [player])
        fresh, opponent, other_opponent = (PlayerFactory.create() for _ in range(3))
        mail_before = QueuedEmail.objects.count()
        audit_before = _match_events()
        unflagged = _record(event, member, [fresh], team_b=[opponent])
    unflagged_mail = QueuedEmail.objects.count() - mail_before
    unflagged_audit = _match_events() - audit_before
    signals.confirmed.clear()
    mail_before = QueuedEmail.objects.count()
    audit_before = _match_events()

    with django_capture_on_commit_callbacks(execute=True):
        match = _record(event, member, [player], team_b=[other_opponent])

    assert _rules(unflagged) == []
    assert _rules(match) == [FlagRule.PLAYER_DAILY_MATCHES]
    assert signals.confirmed == [match.pk]
    assert signals.changed == []
    # The flagged match wrote only what an unflagged one does.
    assert QueuedEmail.objects.count() - mail_before == unflagged_mail
    assert _match_events() - audit_before == unflagged_audit
    assert _counted(match)
    assert RatingState.objects.filter(player=player).exists()
    assert not RatingReplayRequest.objects.exists()


def test_the_match_api_shows_no_flag(
    event: Event, member: User, player: Player, client: Any
) -> None:
    for _ in range(CAP):
        _record(event, member, [player])
    match = _record(event, member, [player])
    assert _rules(match) == [FlagRule.PLAYER_DAILY_MATCHES]
    client.force_login(player.account)

    response = client.get(f"/api/v1/matches/{match.public_code}")

    assert response.status_code == 200
    body = response.content.decode().lower()
    assert "flag" not in body
    assert "unusual" not in body
    assert "daily" not in body


def test_the_organizer_review_counts_the_flag(
    organizer: Organizer, event: Event, member: User
) -> None:
    player = PlayerFactory.create()
    for _ in range(CAP):
        _record(event, member, [player])
    before = organizer_review(organizer).signal_flags

    _record(event, member, [player])

    assert organizer_review(organizer).signal_flags == before + 1
    assert MatchFlag.objects.filter(rule=FlagRule.PLAYER_DAILY_MATCHES).count() == 1
