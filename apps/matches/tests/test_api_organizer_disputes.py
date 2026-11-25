"""The Organizer's dispute operations (#269, ADR 0034).

A member of an Event's verified Organizer lists the disputed matches of
its Events, resolves one as confirmed or voided, or corrects it, with a
required reason. Anyone who may not settle the dispute gets the same 404
as an unknown code, checked before any team code is read. Made-up data
only.
"""

from datetime import date
from typing import Any

import pytest
from django.contrib.auth.models import Permission
from django.test import Client

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditAction, AuditEvent
from apps.clubs.models import ClubRoleKind, MemberRole
from apps.clubs.services import unverify_organizer
from apps.clubs.tests.factories import (
    ClubFactory,
    ClubOrganizerGrantFactory,
    ClubRoleFactory,
    OrganizerMemberFactory,
    verified_organizer,
)
from apps.events.models import Event
from apps.events.tests.factories import EventFactory
from apps.matches import workflow
from apps.matches.models import Match, MatchFormat, MatchStatus
from apps.matches.services import record_match
from apps.matches.workflow import confirm_match, dispute_match
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory
from apps.public_ids.codes import CodeType, generate
from config.api import api

pytestmark = pytest.mark.django_db

LIST = "/api/v1/me/matches/organizer-disputes"
UNKNOWN_MATCH = "M-0000-0000-0000"
PLAYED_ON = date(2026, 6, 1)
GAMES = ((11, 7), (11, 9))
REASON = "Made-up reason for a test."
SECRET_REASON = "   "
LONG_REASON = "x" * 1001


def signed_in(account: Any) -> Client:
    client = Client()
    client.force_login(account)
    return client


def _member(event: Event, account: User | None = None) -> User:
    member: User = account or UserFactory.create()
    OrganizerMemberFactory.create(
        organizer=event.organizer, account=member, role=MemberRole.MATCH_MANAGER
    )
    return member


def _record(players: list[Player], event: Event, played_on: date = PLAYED_ON) -> Match:
    return record_match(
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=GAMES,
        played_on=played_on,
        submitted_by_user=_member(event),
        event=event,
    ).match


def _disputed(
    players: list[Player], event: Event, played_on: date = PLAYED_ON
) -> Match:
    match = _record(players, event, played_on)
    return dispute_match(match, actor=players[0].account)


def _status(match: Match) -> str:
    return Match.objects.get(pk=match.pk).status


def _resolve(client: Client, code: str, outcome: str, reason: str = REASON) -> Any:
    return client.post(
        f"/api/v1/matches/{code}/resolve-dispute",
        {"outcome": outcome, "reason": reason},
        content_type="application/json",
    )


def _correction(players: list[Player], **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "format": "doubles",
        "played_on": PLAYED_ON.isoformat(),
        "team_a": [p.public_code for p in players[:2]],
        "team_b": [p.public_code for p in players[2:4]],
        "games": [{"team_a": 11, "team_b": 3}, {"team_a": 11, "team_b": 5}],
        "reason": REASON,
    }
    body.update(overrides)
    return body


def _correct(client: Client, code: str, body: dict[str, Any]) -> Any:
    return client.post(
        f"/api/v1/matches/{code}/dispute-correction",
        body,
        content_type="application/json",
    )


def _same_404(refused: Any, unknown: Any) -> None:
    assert refused.status_code == unknown.status_code == 404
    assert refused.content == unknown.content


@pytest.fixture
def players() -> list[Player]:
    return [PlayerFactory.create() for _ in range(4)]


@pytest.fixture
def event() -> Event:
    made: Event = EventFactory.create(organizer=verified_organizer())
    return made


@pytest.fixture
def disputed(players: list[Player], event: Event) -> Match:
    return _disputed(players, event)


# Listing (AC 10).


def test_a_member_lists_the_disputes_of_its_events(
    players: list[Player], event: Event, disputed: Match
) -> None:
    older = _disputed(
        [PlayerFactory.create() for _ in range(4)], event, date(2026, 5, 1)
    )
    _record([PlayerFactory.create() for _ in range(4)], event)  # confirmed
    other_event = EventFactory.create(organizer=verified_organizer())
    _disputed([PlayerFactory.create() for _ in range(4)], other_event)

    response = signed_in(_member(event)).get(LIST)

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 2
    assert [item["code"] for item in body["items"]] == [
        disputed.public_code,
        older.public_code,
    ]
    first = body["items"][0]
    assert first["status"] == MatchStatus.DISPUTED
    assert [p["code"] for p in first["team_a"]] == [p.public_code for p in players[:2]]
    assert first["games"] == [
        {"number": 1, "team_a": 11, "team_b": 7},
        {"number": 2, "team_a": 11, "team_b": 9},
    ]
    assert first["my_team"] is None


def test_disputes_of_two_events_of_one_organizer_are_listed_once_each(
    event: Event, disputed: Match
) -> None:
    member = _member(event)
    other_event = EventFactory.create(organizer=event.organizer)
    _disputed([PlayerFactory.create() for _ in range(4)], other_event)

    body = signed_in(member).get(LIST).json()

    assert body["count"] == 2
    assert len({item["code"] for item in body["items"]}) == 2


def test_others_see_an_empty_list(
    players: list[Player], event: Event, disputed: Match
) -> None:
    other = _member(EventFactory.create(organizer=verified_organizer()))
    for account in (other, players[0].account, UserFactory.create()):
        body = signed_in(account).get(LIST).json()
        assert body == {"items": [], "count": 0}


def test_an_unverified_organizer_sees_an_empty_list(
    event: Event, disputed: Match
) -> None:
    member = _member(event)
    verifier: User = UserFactory.create()
    verifier.user_permissions.add(
        Permission.objects.get(
            content_type__app_label="clubs", codename="verify_organizer"
        )
    )
    unverify_organizer(event.organizer, actor=verifier, reason=REASON)

    assert signed_in(member).get(LIST).json() == {"items": [], "count": 0}


def test_the_list_needs_sign_in() -> None:
    assert Client().get(LIST).status_code == 401


# Resolving (AC 1, 4, 5, 6, 7, 10).


def test_a_member_resolves_as_confirmed(event: Event, disputed: Match) -> None:
    member = _member(event)

    response = _resolve(signed_in(member), disputed.public_code, "confirmed")

    assert response.status_code == 200
    assert response.json()["status"] == MatchStatus.CONFIRMED
    assert _status(disputed) == MatchStatus.CONFIRMED
    assert Match.objects.counted().filter(pk=disputed.pk).exists()
    [audit] = AuditEvent.objects.filter(
        action=AuditAction.DISPUTE_OUTCOME, target_object_id=str(disputed.pk)
    )
    assert audit.reason == REASON
    assert audit.actor_user == member


def test_a_member_resolves_as_voided(event: Event, disputed: Match) -> None:
    response = _resolve(signed_in(_member(event)), disputed.public_code, "voided")

    assert response.status_code == 200
    assert _status(disputed) == MatchStatus.VOIDED


@pytest.mark.parametrize("reason", ["", SECRET_REASON, LONG_REASON])
def test_a_blank_or_long_reason_is_a_422_without_the_value(
    event: Event, disputed: Match, reason: str
) -> None:
    response = _resolve(
        signed_in(_member(event)), disputed.public_code, "confirmed", reason
    )

    assert response.status_code == 422
    [error] = response.json()["errors"]
    assert error["field"] == "body.reason"
    if reason.strip():
        assert reason not in response.content.decode()
    assert _status(disputed) == MatchStatus.DISPUTED
    assert not AuditEvent.objects.filter(
        action=AuditAction.DISPUTE_OUTCOME, target_object_id=str(disputed.pk)
    ).exists()


def test_an_unknown_outcome_is_a_422(event: Event, disputed: Match) -> None:
    response = _resolve(signed_in(_member(event)), disputed.public_code, "pending")

    assert response.status_code == 422
    assert _status(disputed) == MatchStatus.DISPUTED


def test_a_match_that_is_not_disputed_is_a_409(
    players: list[Player], event: Event
) -> None:
    match = _record(players, event)

    response = _resolve(signed_in(_member(event)), match.public_code, "voided")

    assert response.status_code == 409
    assert response.json()["code"] == "conflict"
    assert _status(match) == MatchStatus.CONFIRMED


def _refused_accounts(players: list[Player], event: Event) -> list[User]:
    other = _member(EventFactory.create(organizer=verified_organizer()))
    playing = _member(event, players[2].account)
    return [other, playing, players[0].account, UserFactory.create()]


def test_refused_actors_get_the_unknown_code_404_on_resolve(
    players: list[Player], event: Event, disputed: Match
) -> None:
    for account in _refused_accounts(players, event):
        client = signed_in(account)
        refused = _resolve(client, disputed.public_code, "confirmed")
        unknown = _resolve(client, UNKNOWN_MATCH, "confirmed")
        _same_404(refused, unknown)

    assert _status(disputed) == MatchStatus.DISPUTED


def test_a_moderator_who_is_not_a_member_gets_404(
    disputed: Match, moderator: User
) -> None:
    client = signed_in(moderator)

    _same_404(
        _resolve(client, disputed.public_code, "confirmed"),
        _resolve(client, UNKNOWN_MATCH, "confirmed"),
    )


def test_resolve_needs_sign_in(disputed: Match) -> None:
    assert _resolve(Client(), disputed.public_code, "confirmed").status_code == 401


# Correcting (AC 8, 10).


def test_a_member_corrects_a_disputed_match(
    players: list[Player], event: Event, disputed: Match
) -> None:
    member = _member(event)

    response = _correct(signed_in(member), disputed.public_code, _correction(players))

    assert response.status_code == 200
    body = response.json()
    assert body["code"] != disputed.public_code
    assert body["status"] == MatchStatus.CONFIRMED
    assert body["games"] == [
        {"number": 1, "team_a": 11, "team_b": 3},
        {"number": 2, "team_a": 11, "team_b": 5},
    ]
    assert _status(disputed) == MatchStatus.VOIDED
    corrected = Match.objects.get(public_code=body["code"])
    assert corrected.corrects_id == disputed.pk
    assert corrected.event_id == event.pk
    [audit] = AuditEvent.objects.filter(
        action=AuditAction.MATCH_EDIT, target_object_id=str(disputed.pk)
    )
    assert audit.reason == REASON


def test_a_correction_keeps_the_stored_details(
    players: list[Player], event: Event
) -> None:
    match = record_match(
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=GAMES,
        played_on=PLAYED_ON,
        submitted_by_user=_member(event),
        event=event,
        location="Made-up Park",
        event_label="Made-up Open",
        bracket_label="Made-up bracket",
        sequence=3,
    ).match
    dispute_match(match, actor=players[0].account)

    response = _correct(
        signed_in(_member(event)), match.public_code, _correction(players)
    )

    corrected = Match.objects.get(public_code=response.json()["code"])
    assert (
        corrected.location,
        corrected.event_label,
        corrected.bracket_label,
        corrected.sequence,
    ) == ("Made-up Park", "Made-up Open", "Made-up bracket", 3)


def test_a_correction_with_a_blank_reason_is_a_422(
    players: list[Player], event: Event, disputed: Match
) -> None:
    count = Match.objects.count()

    response = _correct(
        signed_in(_member(event)),
        disputed.public_code,
        _correction(players, reason=SECRET_REASON),
    )

    assert response.status_code == 422
    assert response.json()["errors"] == [
        {"field": "body.reason", "code": "blank", "message": "Give a reason."}
    ]
    assert Match.objects.count() == count
    assert _status(disputed) == MatchStatus.DISPUTED


def test_a_correction_with_bad_scores_is_a_422_in_the_body(
    players: list[Player], event: Event, disputed: Match
) -> None:
    response = _correct(
        signed_in(_member(event)),
        disputed.public_code,
        _correction(players, games=[{"team_a": 11, "team_b": 10}]),
    )

    assert response.status_code == 422
    assert all(
        error["field"].startswith("body.") for error in response.json()["errors"]
    )
    assert _status(disputed) == MatchStatus.DISPUTED


def test_refused_actors_get_the_404_before_any_code_is_read(
    players: list[Player], event: Event, disputed: Match
) -> None:
    unknown_player = generate(CodeType.PLAYER)
    bodies = [
        _correction(players),
        _correction(players, team_a=[unknown_player, players[1].public_code]),
    ]
    for account in _refused_accounts(players, event):
        client = signed_in(account)
        for body in bodies:
            _same_404(
                _correct(client, disputed.public_code, body),
                _correct(client, UNKNOWN_MATCH, body),
            )

    assert _status(disputed) == MatchStatus.DISPUTED


def test_a_member_who_puts_their_player_in_the_correction_gets_404(
    players: list[Player], event: Event, disputed: Match
) -> None:
    member = _member(event)
    own = PlayerFactory.create(account=member)
    client = signed_in(member)
    body = _correction(players, team_a=[players[0].public_code, own.public_code])

    _same_404(
        _correct(client, disputed.public_code, body),
        _correct(client, UNKNOWN_MATCH, body),
    )
    assert _status(disputed) == MatchStatus.DISPUTED


def test_correcting_a_match_that_is_not_disputed_is_a_409(
    players: list[Player], event: Event
) -> None:
    match = _record(players, event)

    response = _correct(
        signed_in(_member(event)), match.public_code, _correction(players)
    )

    assert response.status_code == 409
    assert response.json()["code"] == "conflict"
    assert _status(match) == MatchStatus.CONFIRMED


def test_the_operations_are_documented() -> None:
    schema = api.get_openapi_schema()
    ids = {
        operation["operationId"]
        for path in schema["paths"].values()
        for operation in path.values()
    }
    assert {
        "list_organizer_disputes",
        "resolve_match_dispute",
        "correct_disputed_match",
    } <= ids


# More refused actors on both operations (#269 review).


def _both_refused(account: User, match: Match, players: list[Player]) -> None:
    client = signed_in(account)
    _same_404(
        _resolve(client, match.public_code, "confirmed"),
        _resolve(client, UNKNOWN_MATCH, "confirmed"),
    )
    body = _correction(players)
    _same_404(
        _correct(client, match.public_code, body),
        _correct(client, UNKNOWN_MATCH, body),
    )
    assert _status(match) == MatchStatus.DISPUTED


def test_a_club_admin_of_the_matchs_club_gets_404(
    players: list[Player], event: Event
) -> None:
    club = ClubFactory.create()
    ClubOrganizerGrantFactory.create(club=club, organizer=event.organizer)
    match = record_match(
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=GAMES,
        played_on=PLAYED_ON,
        submitted_by_user=_member(event),
        event=event,
        club=club,
    ).match
    dispute_match(match, actor=players[0].account)
    club_admin: User = UserFactory.create()
    ClubRoleFactory.create(club=club, account=club_admin, role=ClubRoleKind.ADMIN)

    _both_refused(club_admin, match, players)


def test_a_member_of_an_organizer_unverified_after_the_dispute_gets_404(
    players: list[Player], event: Event, disputed: Match
) -> None:
    member = _member(event)
    verifier: User = UserFactory.create()
    verifier.user_permissions.add(
        Permission.objects.get(
            content_type__app_label="clubs", codename="verify_organizer"
        )
    )
    unverify_organizer(event.organizer, actor=verifier, reason=REASON)

    _both_refused(member, disputed, players)


def test_a_self_reported_match_gets_404(players: list[Player], event: Event) -> None:
    match = record_match(
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=GAMES,
        played_on=PLAYED_ON,
        submitted_by_user=players[0].account,
    ).match
    confirm_match(match, actor=players[0].account)
    confirm_match(match, actor=players[2].account)
    dispute_match(match, actor=players[0].account)

    for account in (_member(event), players[1].account):
        _both_refused(account, match, players)


def test_a_dispute_settled_during_the_correction_is_a_409(
    players: list[Player],
    event: Event,
    disputed: Match,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_edit = workflow.edit_match
    other = _member(event)

    def settled_first(match: Match, **kwargs: Any) -> Match:
        # Another member resolves the dispute after the view read the
        # match as disputed and before the edit reads it.
        workflow.resolve_dispute(
            match, actor=other, outcome=MatchStatus.CONFIRMED, reason=REASON
        )
        return real_edit(match, **kwargs)

    monkeypatch.setattr(workflow, "edit_match", settled_first)
    count = Match.objects.count()

    response = _correct(
        signed_in(_member(event)), disputed.public_code, _correction(players)
    )

    assert response.status_code == 409
    assert response.json()["code"] == "conflict"
    assert Match.objects.count() == count
    # The stand-in resolve ran inside the view's transaction, so the 409
    # rolled it back too.
    assert _status(disputed) == MatchStatus.DISPUTED
