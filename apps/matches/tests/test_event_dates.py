"""A match with an Event must be played within the Event's dates (#207).

``validate_match`` takes the Event's start and end dates as plain inputs
and refuses a ``played_on`` outside them (``outside_event_dates``, fixed
text that names neither date). ``draft_match`` reads the stored dates by
id, after the caller's permission check, so ``record_match`` and
``edit_match`` (every correction) both apply it, and the API and the CSV
upload report it like any other validation issue. Made-up data only.
"""

import csv
import io
from datetime import date, timedelta
from typing import Any

import pytest
from django.test import Client
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditEvent
from apps.clubs.tests.factories import OrganizerMemberFactory, verified_organizer
from apps.events.models import Event
from apps.events.tests.factories import EventFactory
from apps.matches import services, tasks
from apps.matches.csv_format import COLUMNS
from apps.matches.models import (
    Game,
    GameTarget,
    Match,
    MatchFlag,
    MatchFormat,
    MatchParticipant,
    MatchStatus,
)
from apps.matches.permissions import MatchActionDenied, MatchPermissionDenied
from apps.matches.results_files import create_results_file, process_results_file
from apps.matches.services import (
    MatchIssue,
    MatchValidationError,
    record_match,
    validate_match,
)
from apps.matches.workflow import dispute_match, edit_match
from apps.partners.tests.factories import approved_application
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory

START = date(2026, 3, 10)
END = date(2026, 3, 12)
GAMES = ((11, 7), (11, 4))
MESSAGE = "The date played must fall within the event's dates."
OUTSIDE = MatchIssue("played_on", "outside_event_dates", MESSAGE)
REASON = "Made-up reason for a test."


def _validate(**overrides: Any) -> object:
    values: dict[str, Any] = {
        "format": MatchFormat.SINGLES,
        "team_a": ("player-a1",),
        "team_b": ("player-b1",),
        "games": GAMES,
        "played_on": START,
        "event_dates": (START, END),
    }
    values.update(overrides)
    return validate_match(**values)


def _issues(**overrides: Any) -> list[MatchIssue]:
    with pytest.raises(MatchValidationError) as caught:
        _validate(**overrides)
    return list(caught.value.issues)


# validate_match: plain inputs, no database.


@pytest.mark.parametrize("played_on", [START - timedelta(days=1), END + timedelta(1)])
def test_a_date_outside_the_event_is_one_fixed_issue(played_on: date) -> None:
    assert _issues(played_on=played_on) == [OUTSIDE]


@pytest.mark.parametrize("played_on", [START, date(2026, 3, 11), END])
def test_the_first_and_last_days_are_inside(played_on: date) -> None:
    _validate(played_on=played_on)


def test_the_message_names_neither_date() -> None:
    (issue,) = _issues(played_on=END + timedelta(days=1))

    assert issue.message == MESSAGE
    for day in (START, END, END + timedelta(days=1)):
        for form in (
            day.isoformat(),
            day.strftime("%m/%d/%Y"),
            day.strftime("%d/%m/%Y"),
            day.strftime("%B"),
            str(day.day),
        ):
            assert form not in issue.message


def test_no_event_dates_change_nothing() -> None:
    _validate(played_on=END + timedelta(days=30), event_dates=None)
    validate_match(
        format=MatchFormat.SINGLES,
        team_a=("player-a1",),
        team_b=("player-b1",),
        games=GAMES,
        played_on=END + timedelta(days=30),
    )


def test_an_invalid_date_gets_only_its_own_issue() -> None:
    issues = _issues(played_on="2026-03-11")

    assert [(i.field, i.code) for i in issues] == [("played_on", "invalid")]


def test_a_future_date_gets_only_its_own_issue() -> None:
    future = (timezone.now() + timedelta(days=30)).date()

    issues = _issues(played_on=future, event_dates=(START, future - timedelta(1)))

    assert [(i.field, i.code) for i in issues] == [("played_on", "future_date")]


def test_validate_match_reads_no_database() -> None:
    # No django_db mark: any query would raise.
    assert _issues(played_on=END + timedelta(days=1)) == [OUTSIDE]


# record_match and edit_match.


def _member(event: Event) -> User:
    account: User = UserFactory.create()
    OrganizerMemberFactory.create(organizer=event.organizer, account=account)
    return account


@pytest.fixture
def event() -> Event:
    made: Event = EventFactory.create(
        organizer=verified_organizer(), start_date=START, end_date=END
    )
    return made


@pytest.fixture
def players() -> list[Player]:
    return [PlayerFactory.create() for _ in range(2)]


def _record(event: Event, players: list[Player], **overrides: Any) -> Match:
    values: dict[str, Any] = {
        "format": MatchFormat.SINGLES,
        "team_a": players[:1],
        "team_b": players[1:],
        "games": GAMES,
        "played_on": START,
        "event": event,
        "submitted_by_user": _member(event),
    }
    values.update(overrides)
    return record_match(**values).match


def _rows() -> tuple[int, ...]:
    return (
        Match.objects.count(),
        MatchParticipant.objects.count(),
        Game.objects.count(),
        MatchFlag.objects.count(),
        AuditEvent.objects.count(),
    )


@pytest.mark.django_db
@pytest.mark.parametrize("played_on", [START - timedelta(days=1), END + timedelta(1)])
def test_record_match_refuses_a_date_outside_the_event(
    event: Event, players: list[Player], played_on: date
) -> None:
    member = _member(event)
    before = _rows()

    with pytest.raises(MatchValidationError) as caught:
        _record(event, players, played_on=played_on, submitted_by_user=member)

    assert list(caught.value.issues) == [OUTSIDE]
    assert _rows() == before


@pytest.mark.django_db
@pytest.mark.parametrize("played_on", [START, END])
def test_record_match_accepts_the_first_and_last_days(
    event: Event, players: list[Player], played_on: date
) -> None:
    match = _record(event, players, played_on=played_on)

    assert match.status == MatchStatus.CONFIRMED
    assert match.played_on == played_on


@pytest.mark.django_db
def test_the_stored_dates_are_read_not_the_instances(
    event: Event, players: list[Player]
) -> None:
    stale = Event.objects.get(pk=event.pk)
    stale.end_date = END + timedelta(days=10)

    with pytest.raises(MatchValidationError) as caught:
        _record(stale, players, played_on=END + timedelta(days=1))

    assert list(caught.value.issues) == [OUTSIDE]


@pytest.mark.django_db
def test_a_refused_submitter_gets_the_permission_error(
    event: Event, players: list[Player]
) -> None:
    with pytest.raises(MatchPermissionDenied):
        _record(
            event,
            players,
            played_on=END + timedelta(days=1),
            submitted_by_user=UserFactory.create(),
        )


@pytest.mark.django_db
def test_no_event_date_is_read_before_the_permission_check(
    event: Event, players: list[Player], monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise AssertionError("read the Event's dates before the permission check")

    monkeypatch.setattr(services, "_event_dates", fail)
    outsider = UserFactory.create()

    with pytest.raises(MatchPermissionDenied):
        _record(
            event, players, played_on=END + timedelta(1), submitted_by_user=outsider
        )


@pytest.mark.django_db
def test_an_organizer_correction_outside_the_dates_is_refused(
    event: Event, players: list[Player]
) -> None:
    member = _member(event)
    match = _record(event, players, submitted_by_user=member)
    before = _rows()

    with pytest.raises(MatchValidationError) as caught:
        edit_match(
            match,
            actor=member,
            format=MatchFormat.SINGLES,
            team_a=players[:1],
            team_b=players[1:],
            games=((11, 3), (11, 2)),
            played_on=END + timedelta(days=1),
        )

    assert list(caught.value.issues) == [OUTSIDE]
    assert _rows() == before
    assert Match.objects.get(pk=match.pk).status == MatchStatus.CONFIRMED


@pytest.mark.django_db
def test_a_settling_organizer_correction_outside_the_dates_is_refused(
    event: Event, players: list[Player]
) -> None:
    match = _record(event, players)
    dispute_match(match, actor=players[0].account)
    settler = _member(event)
    before = _rows()

    with pytest.raises(MatchValidationError) as caught:
        edit_match(
            match,
            actor=settler,
            reason=REASON,
            format=MatchFormat.SINGLES,
            team_a=players[:1],
            team_b=players[1:],
            games=((11, 3), (11, 2)),
            played_on=START - timedelta(days=1),
        )

    assert list(caught.value.issues) == [OUTSIDE]
    assert _rows() == before
    assert Match.objects.get(pk=match.pk).status == MatchStatus.DISPUTED


@pytest.mark.django_db
def test_an_application_correction_outside_the_dates_is_refused(
    event: Event, players: list[Player]
) -> None:
    application = approved_application(organizer=event.organizer)
    match = _record(
        event, players, submitted_by_user=None, submitted_by_application=application
    )

    with pytest.raises(MatchValidationError) as caught:
        edit_match(
            match,
            actor=application,
            format=MatchFormat.SINGLES,
            team_a=players[:1],
            team_b=players[1:],
            games=((11, 3), (11, 2)),
            played_on=END + timedelta(days=1),
        )

    assert list(caught.value.issues) == [OUTSIDE]


@pytest.mark.django_db
def test_an_old_match_is_not_corrected_after_its_event_dates_moved(
    event: Event, players: list[Player]
) -> None:
    member = _member(event)
    match = _record(event, players, submitted_by_user=member)
    Event.objects.filter(pk=event.pk).update(
        start_date=END + timedelta(days=5), end_date=END + timedelta(days=6)
    )

    with pytest.raises(MatchValidationError) as caught:
        edit_match(
            match,
            actor=member,
            format=MatchFormat.SINGLES,
            team_a=players[:1],
            team_b=players[1:],
            games=((11, 3), (11, 2)),
            played_on=START,
        )

    assert list(caught.value.issues) == [OUTSIDE]


@pytest.mark.django_db
def test_a_refused_editor_gets_the_permission_error(
    event: Event, players: list[Player], monkeypatch: pytest.MonkeyPatch
) -> None:
    match = _record(event, players)

    def fail(*args: object, **kwargs: object) -> None:
        raise AssertionError("read the Event's dates before the permission check")

    monkeypatch.setattr(services, "_event_dates", fail)

    with pytest.raises(MatchActionDenied):
        edit_match(
            match,
            actor=UserFactory.create(),
            format=MatchFormat.SINGLES,
            team_a=players[:1],
            team_b=players[1:],
            games=((11, 3), (11, 2)),
            played_on=END + timedelta(days=1),
        )


# The API: the Organizer's dispute correction.


@pytest.mark.django_db
def test_the_api_returns_the_issue_on_played_on(
    event: Event, players: list[Player]
) -> None:
    match = _record(event, players)
    dispute_match(match, actor=players[0].account)
    client = Client()
    client.force_login(_member(event))

    response = client.post(
        f"/api/v1/matches/{match.public_code}/dispute-correction",
        {
            "format": "singles",
            "played_on": (END + timedelta(days=1)).isoformat(),
            "team_a": [players[0].public_code],
            "team_b": [players[1].public_code],
            "games": [{"team_a": 11, "team_b": 3}, {"team_a": 11, "team_b": 5}],
            "reason": REASON,
        },
        content_type="application/json",
    )

    assert response.status_code == 422
    assert response.json()["errors"] == [
        {"field": "body.played_on", "code": "outside_event_dates", "message": MESSAGE}
    ]
    assert Match.objects.get(pk=match.pk).status == MatchStatus.DISPUTED


# The CSV upload.


def _csv_row(a: Player, b: Player, played_on: date) -> list[str]:
    return (
        ["S", "SIDEOUT", "", played_on.isoformat()]
        + [a.display_name, a.public_code, "", ""]
        + [b.display_name, b.public_code, "", ""]
        + ["11", "4"]
        + [""] * 8
    )


@pytest.mark.django_db
def test_the_upload_fails_a_row_outside_the_dates_on_its_date_column(
    event: Event, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tasks.process_results_file, "delay", lambda *a, **k: None)
    member = _member(event)
    players = [PlayerFactory.create() for _ in range(4)]
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(COLUMNS)
    writer.writerow(_csv_row(players[0], players[1], END + timedelta(days=1)))
    writer.writerow(_csv_row(players[2], players[3], END))
    results_file = create_results_file(
        account=member,
        event=event,
        content=out.getvalue().encode(),
        game_target=GameTarget.ELEVEN,
        location="",
        players_13_or_older=True,
    )

    process_results_file(results_file.pk)
    results_file.refresh_from_db()

    outside, inside = results_file.report
    assert outside["result"] == "failed"
    assert [(i["columns"], i["code"], i["message"]) for i in outside["issues"]] == [
        (["date"], "outside_event_dates", MESSAGE)
    ]
    assert inside["result"] == "recorded"
    assert results_file.submission_batch.matches.count() == 1
