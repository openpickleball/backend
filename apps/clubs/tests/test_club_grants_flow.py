"""Club Organizer Grants end to end through the API (#230, ADR 0021).

A Club admin grants a verified Organizer; a member of that Organizer
with MFA then creates an Event at the Club and uploads its results.
Without the grant, both are refused. After the grant is removed, a new
Event and a new match naming the Club are refused, and the matches
already recorded are unchanged. A Club admin who is not a member of a
verified Organizer cannot submit a match naming the Club at all. Made-up
data only.
"""

import csv
import io
import json
from collections.abc import Iterator
from datetime import date
from typing import Any

import pytest
from allauth.account.models import EmailAddress
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory, enroll_totp
from apps.clubs.models import Club, MemberRole, Organizer
from apps.clubs.tests.factories import (
    ClubFactory,
    ClubRoleFactory,
    OrganizerMemberFactory,
    verified_organizer,
)
from apps.events.models import Event
from apps.events.tests.factories import EventFactory
from apps.matches.csv_format import COLUMNS
from apps.matches.models import Match, MatchFormat, MatchStatus
from apps.matches.permissions import MatchPermissionDenied
from apps.matches.services import record_match
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db

ORGANIZER_API = "/api/v1/organizer"
CLUBS_API = "/api/v1/clubs"


@pytest.fixture(autouse=True)
def _clear_cache() -> Iterator[None]:
    """The API throttles use the cache."""
    cache.clear()
    yield
    cache.clear()


def _account() -> User:
    account: User = UserFactory.create()
    EmailAddress.objects.create(
        user=account, email=account.email, verified=True, primary=True
    )
    enroll_totp(account)
    PlayerFactory.create(account=account)
    return account


def _client(account: User) -> Client:
    client = Client()
    client.force_login(account)
    return client


def _json(account: User, method: str, path: str, body: object = None) -> Any:
    kwargs: dict[str, Any] = {}
    if body is not None:
        kwargs = {"data": json.dumps(body), "content_type": "application/json"}
    return getattr(_client(account), method)(path, **kwargs)


def _create_event(member: User, organizer: Organizer, club: Club, name: str) -> Any:
    return _json(
        member,
        "post",
        f"{ORGANIZER_API}/events",
        {
            "organizer": organizer.public_code,
            "name": name,
            "kind": "league",
            "club": club.public_code,
            "start_date": "2026-11-01",
            "end_date": "2026-11-30",
        },
    )


def _csv(a: Player, b: Player, loser: int) -> bytes:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(COLUMNS)
    row = ["S", "SIDEOUT", "", "2026-11-10"]
    row += [a.display_name, a.public_code, "", ""]
    row += [b.display_name, b.public_code, "", ""]
    row += ["11", str(loser)] + [""] * 8
    writer.writerow(row)
    return out.getvalue().encode()


def _upload(member: User, event_code: str, content: bytes) -> Any:
    return _client(member).post(
        f"{ORGANIZER_API}/events/{event_code}/results-files",
        {
            "file": SimpleUploadedFile("results.csv", content, "text/csv"),
            "players_13_or_older": "true",
        },
    )


def _setup() -> tuple[Club, User, Organizer, User]:
    club = ClubFactory.create()
    club_admin = _account()
    ClubRoleFactory.create(club=club, account=club_admin)
    organizer = verified_organizer()
    member = _account()
    OrganizerMemberFactory.create(
        organizer=organizer, account=member, role=MemberRole.MATCH_MANAGER
    )
    return club, club_admin, organizer, member


def test_a_grant_lets_the_organizer_name_the_club_until_it_is_removed(
    django_capture_on_commit_callbacks: Any,
) -> None:
    club, club_admin, organizer, member = _setup()
    players = [PlayerFactory.create() for _ in range(4)]

    # No grant: the Event is refused.
    refused = _create_event(member, organizer, club, "Made-up Before")
    assert refused.status_code == 422
    assert refused.json()["errors"][0]["field"] == "body.club"
    assert not Event.objects.exists()

    granted = _json(
        club_admin,
        "post",
        f"{CLUBS_API}/{club.public_code}/organizer-grants",
        {"organizer": organizer.public_code},
    )
    assert granted.status_code == 201

    created = _create_event(member, organizer, club, "Made-up League")
    assert created.status_code == 201
    event_code = created.json()["code"]
    event = Event.objects.get(public_code=event_code)
    assert event.club_id == club.pk

    # The file is processed after commit, by an eager task in tests.
    with django_capture_on_commit_callbacks(execute=True):
        uploaded = _upload(member, event_code, _csv(players[0], players[1], 7))
    assert uploaded.status_code == 202
    [recorded] = Match.objects.filter(event=event)
    assert recorded.status == MatchStatus.CONFIRMED

    removed = _json(
        club_admin,
        "delete",
        f"{CLUBS_API}/{club.public_code}/organizer-grants/{organizer.public_code}",
    )
    assert removed.status_code == 204

    # A new Event at the Club is refused.
    again = _create_event(member, organizer, club, "Made-up Later")
    assert again.status_code == 422
    assert again.json()["errors"][0]["field"] == "body.club"
    # A new match on the Club's Event is refused.
    with django_capture_on_commit_callbacks(execute=True):
        later = _upload(member, event_code, _csv(players[2], players[3], 5))
    assert later.status_code == 202
    report = _json(
        member, "get", f"{ORGANIZER_API}/results-files/{later.json()['code']}"
    )
    assert report.json()["recorded"] == 0
    assert (report.json()["status"], report.json()["failed"]) == ("done", 1)
    [row] = report.json()["rows"]
    assert [issue["code"] for issue in row["issues"]] == ["forbidden"]
    assert Match.objects.filter(event=event).count() == 1
    with pytest.raises(MatchPermissionDenied):
        record_match(
            format=MatchFormat.SINGLES,
            team_a=[players[2]],
            team_b=[players[3]],
            games=((11, 4),),
            played_on=date(2026, 11, 11),
            event=event,
            submitted_by_user=member,
        )
    # The recorded match is unchanged.
    recorded.refresh_from_db()
    assert recorded.status == MatchStatus.CONFIRMED
    assert recorded.event_id == event.pk
    event.refresh_from_db()
    assert event.club_id == club.pk
    assert Match.objects.count() == 1


def test_a_club_admin_who_is_no_organizer_member_cannot_submit_for_the_club(
    django_capture_on_commit_callbacks: Any,
) -> None:
    club, club_admin, organizer, _member = _setup()
    _json(
        club_admin,
        "post",
        f"{CLUBS_API}/{club.public_code}/organizer-grants",
        {"organizer": organizer.public_code},
    )
    event = EventFactory.create(organizer=organizer, club=club)
    a, b = PlayerFactory.create(), PlayerFactory.create()

    for kwargs in ({"club": club}, {"event": event}, {"event": event, "club": club}):
        with pytest.raises(MatchPermissionDenied):
            record_match(
                format=MatchFormat.SINGLES,
                team_a=[a],
                team_b=[b],
                games=((11, 6),),
                played_on=date(2026, 11, 12),
                submitted_by_user=club_admin,
                **kwargs,
            )
    with django_capture_on_commit_callbacks(execute=True):
        upload = _upload(club_admin, event.public_code, _csv(a, b, 6))
    assert upload.status_code == 404
    event_create = _create_event(club_admin, organizer, club, "Made-up Club Night")
    assert event_create.status_code == 403
    assert not Match.objects.exists()
    assert Event.objects.count() == 1
