"""The unusual submission counts include only new trusted submissions (#206).

An ``edit_match()`` correction is a new Match that keeps the old match's
submitter and Event, with a fresh ``created_at``, but it is not a new
submission: it never uses up an Organizer's or an application's first
matches, and never raises its submitter's daily number. A player's own
self-submitted matches are not trusted submissions either, so they never
count toward that Account's daily number. Voided matches still count.
Made-up data only.
"""

from datetime import UTC, date, datetime, timedelta
from itertools import count
from typing import Any

import pytest
from django.utils import timezone
from pytest_django.fixtures import Settings

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.clubs.models import Organizer
from apps.clubs.tests.factories import OrganizerMemberFactory, verified_organizer
from apps.events.models import Event
from apps.events.tests.factories import EventFactory
from apps.matches.models import FlagReason, FlagRule, Match, MatchFormat
from apps.matches.services import record_match
from apps.matches.workflow import edit_match
from apps.partners.tests.factories import approved_application
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db

GAMES = ((11, 7), (11, 4))
_days = count()


@pytest.fixture(autouse=True)
def high_thresholds(settings: Settings) -> None:
    settings.UNUSUAL_SUBMISSION_FIRST_MATCHES = 1_000
    settings.UNUSUAL_SUBMISSION_BATCH_SIZE = 1_000
    settings.UNUSUAL_SUBMISSION_DAILY_MATCHES = 1_000
    settings.UNUSUAL_SUBMISSION_PLAYER_DAILY_MATCHES = 1_000


@pytest.fixture
def organizer() -> Organizer:
    return verified_organizer()


@pytest.fixture
def event(organizer: Organizer) -> Event:
    made: Event = EventFactory.create(organizer=organizer)
    return made


def _member(organizer: Organizer) -> User:
    account: User = UserFactory.create()
    OrganizerMemberFactory.create(organizer=organizer, account=account)
    return account


def _played_on() -> date:
    return date(2026, 1, 1) + timedelta(days=next(_days) % 300)


def _record(event: Event | None = None, **overrides: Any) -> Match:
    players = [PlayerFactory.create(), PlayerFactory.create()]
    values: dict[str, Any] = {
        "format": MatchFormat.SINGLES,
        "team_a": players[:1],
        "team_b": players[1:],
        "games": GAMES,
        "played_on": _played_on(),
        "event": event,
    }
    values.update(overrides)
    return record_match(**values).match


def _correct(match: Match, actor: Any, scores: tuple[int, int]) -> Match:
    players: list[Player] = [
        participant.player for participant in match.participants.order_by("team")
    ]
    return edit_match(
        match,
        actor=actor,
        format=MatchFormat.SINGLES,
        team_a=players[:1],
        team_b=players[1:],
        games=((11, scores[0]), (11, scores[1])),
        played_on=match.played_on,
    )


def _rules(match: Match) -> list[str]:
    return sorted(
        match.flags.filter(reason=FlagReason.UNUSUAL_SUBMISSION).values_list(
            "rule", flat=True
        )
    )


@pytest.fixture
def pinned_now(monkeypatch: pytest.MonkeyPatch) -> list[datetime]:
    moment = [datetime(2026, 11, 20, 12, 0, tzinfo=UTC)]
    monkeypatch.setattr(timezone, "now", lambda: moment[0])
    return moment


def test_corrections_do_not_use_up_an_organizers_first_matches(
    settings: Settings, organizer: Organizer, event: Event
) -> None:
    settings.UNUSUAL_SUBMISSION_FIRST_MATCHES = 2
    member = _member(organizer)
    match = _record(event, submitted_by_user=member)
    for loser in (1, 2, 3):
        match = _correct(match, member, (loser, loser))

    second = _record(event, submitted_by_user=member)
    third = _record(event, submitted_by_user=member)

    assert _rules(second) == [FlagRule.FIRST_MATCHES]
    assert _rules(third) == []


def test_corrections_do_not_use_up_an_applications_first_matches(
    settings: Settings, organizer: Organizer, event: Event
) -> None:
    settings.UNUSUAL_SUBMISSION_FIRST_MATCHES = 2
    member = _member(organizer)
    for _ in range(3):
        _record(event, submitted_by_user=member)
    application = approved_application(organizer=organizer)
    match = _record(event, submitted_by_application=application)
    for loser in (1, 2, 3):
        match = _correct(match, application, (loser, loser))

    second = _record(event, submitted_by_application=application)
    third = _record(event, submitted_by_application=application)

    assert _rules(second) == [FlagRule.FIRST_MATCHES]
    assert _rules(third) == []


def test_a_correction_does_not_raise_the_original_submitters_daily_number(
    settings: Settings, organizer: Organizer, event: Event, pinned_now: list[datetime]
) -> None:
    settings.UNUSUAL_SUBMISSION_DAILY_MATCHES = 3
    submitter = _member(organizer)
    corrector = _member(organizer)
    first = _record(event, submitted_by_user=submitter)
    second = _record(event, submitted_by_user=submitter)
    # Another member's corrections keep the original submitter.
    corrected = _correct(first, corrector, (1, 2))
    _correct(second, corrector, (3, 4))
    assert corrected.submitted_by_user_id == submitter.pk

    third = _record(event, submitted_by_user=submitter)

    assert FlagRule.DAILY_VOLUME not in _rules(third)


def test_self_submissions_do_not_count_toward_the_daily_number(
    settings: Settings, organizer: Organizer, event: Event, pinned_now: list[datetime]
) -> None:
    settings.UNUSUAL_SUBMISSION_DAILY_MATCHES = 1
    submitter = _member(organizer)
    own = PlayerFactory.create(account=submitter)
    for _ in range(2):
        _record(team_a=[own], submitted_by_user=submitter)

    first_event_match = _record(event, submitted_by_user=submitter)
    second_event_match = _record(event, submitted_by_user=submitter)

    assert FlagRule.DAILY_VOLUME not in _rules(first_event_match)
    assert FlagRule.DAILY_VOLUME in _rules(second_event_match)
