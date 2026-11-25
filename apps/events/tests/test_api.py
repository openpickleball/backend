"""The Organizer's Event operations at /api/v1/organizer/events (#125).

A member (either role) of a verified Organizer lists, creates, changes,
and deletes its Events. Create answers every refused or unknown organizer
with one 403; change and delete answer every refused actor with the same
404 as an unknown code. Made-up data only.
"""

from datetime import date
from typing import Any

import pytest
from django.contrib.auth.models import Permission
from django.test import Client

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditAction, AuditEvent
from apps.clubs.models import MemberRole, Organizer
from apps.clubs.services import unverify_organizer
from apps.clubs.tests.factories import (
    ClubFactory,
    ClubOrganizerGrantFactory,
    OrganizerFactory,
    OrganizerMemberFactory,
    verified_organizer,
)
from apps.events.models import Event, EventKind
from apps.events.tests.factories import EventFactory
from apps.matches.models import Match, MatchFormat, MatchStatus, SubmissionBatch
from apps.matches.services import record_match
from apps.matches.workflow import void_match
from apps.players.tests.factories import PlayerFactory
from apps.public_ids.codes import CodeType, generate

pytestmark = pytest.mark.django_db

EVENTS = "/api/v1/organizer/events"
UNKNOWN_EVENT = generate(CodeType.EVENT)
UNKNOWN_CLUB = generate(CodeType.CLUB)
UNKNOWN_ORGANIZER = generate(CodeType.ORGANIZER)
FORBIDDEN_TEXT = "You may not manage this organizer's events."
LOCKED_TEXT = "This event has matches, so its name and dates cannot change."
DELETE_TEXT = "This event has matches or uploads, so it cannot be deleted."
MADE_UP_NAME = "Made-up Spring Ladder"


def _member(organizer: Organizer, role: MemberRole = MemberRole.ADMIN) -> User:
    account: User = UserFactory.create()
    OrganizerMemberFactory.create(organizer=organizer, account=account, role=role)
    return account


def _client(account: User) -> Client:
    client = Client()
    client.force_login(account)
    return client


def _body(owner: Organizer, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "organizer": owner.public_code,
        "name": MADE_UP_NAME,
        "kind": "league",
        "start_date": "2026-03-01",
        "end_date": "2026-05-31",
    }
    body.update(overrides)
    return body


def _post(client: Client, body: dict[str, Any]) -> Any:
    return client.post(EVENTS, body, content_type="application/json")


def _patch(client: Client, code: str, body: dict[str, Any]) -> Any:
    return client.patch(f"{EVENTS}/{code}", body, content_type="application/json")


def _delete(client: Client, code: str) -> Any:
    return client.delete(f"{EVENTS}/{code}")


def _audit(action: str) -> list[AuditEvent]:
    return list(AuditEvent.objects.filter(action=action))


def _match_on(event: Event, submitter: User) -> Match:
    return record_match(
        format=MatchFormat.SINGLES,
        team_a=[PlayerFactory.create()],
        team_b=[PlayerFactory.create()],
        games=((11, 7), (11, 9)),
        played_on=date(2026, 3, 2),
        submitted_by_user=submitter,
        event=event,
    ).match


def _field_errors(response: Any) -> dict[str, str]:
    return {item["field"]: item["message"] for item in response.json()["errors"]}


# List


def test_list_shows_events_of_every_organizer_the_account_is_member_of() -> None:
    verified = verified_organizer()
    unverified = OrganizerFactory.create()
    account = _member(verified, MemberRole.MATCH_MANAGER)
    OrganizerMemberFactory.create(organizer=unverified, account=account)
    older = EventFactory.create(organizer=verified, start_date=date(2026, 1, 1))
    newer = EventFactory.create(
        organizer=unverified, start_date=date(2026, 6, 1), end_date=date(2026, 6, 2)
    )
    EventFactory.create(organizer=verified_organizer())

    response = _client(account).get(EVENTS)

    assert response.status_code == 200
    page = response.json()
    assert page["count"] == 2
    assert [item["code"] for item in page["items"]] == [
        newer.public_code,
        older.public_code,
    ]
    assert page["items"][1] == {
        "code": older.public_code,
        "name": older.name,
        "kind": "league",
        "organizer": verified.public_code,
        "club": None,
        "start_date": older.start_date.isoformat(),
        "end_date": older.end_date.isoformat(),
    }


def test_list_without_membership_is_empty() -> None:
    EventFactory.create(organizer=verified_organizer())
    response = _client(UserFactory.create()).get(EVENTS)
    assert response.status_code == 200
    assert response.json() == {"items": [], "count": 0}


def test_list_ties_on_start_date_newest_row_first() -> None:
    organizer = verified_organizer()
    account = _member(organizer)
    day = date(2026, 2, 1)
    first = EventFactory.create(organizer=organizer, start_date=day, end_date=day)
    second = EventFactory.create(organizer=organizer, start_date=day, end_date=day)
    items = _client(account).get(EVENTS).json()["items"]
    assert [item["code"] for item in items] == [second.public_code, first.public_code]


def test_list_shows_club_code() -> None:
    organizer = verified_organizer()
    club = ClubFactory.create()
    ClubOrganizerGrantFactory.create(club=club, organizer=organizer)
    EventFactory.create(organizer=organizer, club=club)
    items = _client(_member(organizer)).get(EVENTS).json()["items"]
    assert items[0]["club"] == club.public_code


@pytest.mark.parametrize(
    "query", ["limit=0", "limit=101", "offset=-1", "offset=9223372036854775808"]
)
def test_list_paging_bounds(query: str) -> None:
    response = _client(UserFactory.create()).get(f"{EVENTS}?{query}")
    assert response.status_code == 422


def test_list_paging() -> None:
    organizer = verified_organizer()
    account = _member(organizer)
    for _ in range(3):
        EventFactory.create(organizer=organizer)
    page = _client(account).get(f"{EVENTS}?limit=2&offset=2").json()
    assert page["count"] == 3
    assert len(page["items"]) == 1


def test_list_needs_sign_in() -> None:
    assert Client().get(EVENTS).status_code == 401


# Create


@pytest.mark.parametrize("role", list(MemberRole))
def test_create_by_member_of_verified_organizer(role: MemberRole) -> None:
    organizer = verified_organizer()
    account = _member(organizer, role)

    response = _post(_client(account), _body(organizer, kind="tournament"))

    assert response.status_code == 201
    event = Event.objects.get()
    assert response.json() == {
        "code": event.public_code,
        "name": MADE_UP_NAME,
        "kind": "tournament",
        "organizer": organizer.public_code,
        "club": None,
        "start_date": "2026-03-01",
        "end_date": "2026-05-31",
    }
    [audit] = _audit(AuditAction.EVENT_CREATE)
    assert audit.actor_user_id == account.pk
    assert audit.target_object_id == str(event.pk)


def test_create_accepts_a_code_in_any_form() -> None:
    organizer = verified_organizer()
    loose = organizer.public_code.lower().replace("-", " ")
    response = _post(_client(_member(organizer)), _body(organizer, organizer=loose))
    assert response.status_code == 201
    assert response.json()["organizer"] == organizer.public_code


def test_create_with_granted_club() -> None:
    organizer = verified_organizer()
    club = ClubFactory.create()
    ClubOrganizerGrantFactory.create(club=club, organizer=organizer)
    response = _post(
        _client(_member(organizer)), _body(organizer, club=club.public_code)
    )
    assert response.status_code == 201
    assert response.json()["club"] == club.public_code


def _refused_bodies() -> dict[str, tuple[User, dict[str, Any]]]:
    verified = verified_organizer()
    unverified = OrganizerFactory.create()
    return {
        "unverified member": (_member(unverified), _body(unverified)),
        "non-member": (UserFactory.create(), _body(verified)),
        "member of another organizer": (
            _member(verified_organizer()),
            _body(verified),
        ),
        "unknown code": (
            _member(verified),
            _body(verified, organizer=UNKNOWN_ORGANIZER),
        ),
        "malformed code": (_member(verified), _body(verified, organizer="not a code")),
    }


def test_create_refused_gets_one_403_and_writes_nothing() -> None:
    bodies = []
    for account, body in _refused_bodies().values():
        # An unknown club too: the organizer is refused before it is read.
        response = _post(_client(account), {**body, "club": UNKNOWN_CLUB})
        assert response.status_code == 403
        bodies.append(response.content)
    assert len(set(bodies)) == 1
    error = _post(_client(UserFactory.create()), _body(verified_organizer())).json()
    assert error == {"code": "forbidden", "message": FORBIDDEN_TEXT, "errors": []}
    assert not Event.objects.exists()
    assert not _audit(AuditAction.EVENT_CREATE)


def test_create_after_unverify_is_refused() -> None:
    organizer = verified_organizer()
    account = _member(organizer)
    staff: User = UserFactory.create()
    staff.user_permissions.add(
        Permission.objects.get(
            content_type__app_label="clubs", codename="verify_organizer"
        )
    )
    unverify_organizer(organizer, actor=staff)
    assert _post(_client(account), _body(organizer)).status_code == 403


def test_create_needs_sign_in() -> None:
    response = _post(Client(), _body(verified_organizer()))
    assert response.status_code == 401


@pytest.mark.parametrize("club", [UNKNOWN_CLUB, "not a club", UNKNOWN_EVENT])
def test_create_with_unknown_or_malformed_club(club: str) -> None:
    organizer = verified_organizer()
    response = _post(_client(_member(organizer)), _body(organizer, club=club))
    assert response.status_code == 422
    assert _field_errors(response) == {"body.club": "Choose an existing club."}
    assert club not in response.content.decode()
    assert not Event.objects.exists()


def test_create_with_club_without_grant() -> None:
    organizer = verified_organizer()
    club = ClubFactory.create()
    response = _post(
        _client(_member(organizer)), _body(organizer, club=club.public_code)
    )
    assert response.status_code == 422
    assert _field_errors(response) == {
        "body.club": "This club has not allowed the organizer to name it."
    }
    assert not Event.objects.exists()
    assert not _audit(AuditAction.EVENT_CREATE)


def test_create_with_end_before_start() -> None:
    organizer = verified_organizer()
    response = _post(
        _client(_member(organizer)),
        _body(organizer, start_date="2026-05-02", end_date="2026-05-01"),
    )
    assert response.status_code == 422
    assert _field_errors(response) == {
        "body.end_date": "The end date cannot be before the start date."
    }
    assert "2026-05" not in response.content.decode()


@pytest.mark.parametrize("name", ["", "   "])
def test_create_with_blank_name(name: str) -> None:
    organizer = verified_organizer()
    response = _post(_client(_member(organizer)), _body(organizer, name=name))
    assert response.status_code == 422
    assert list(_field_errors(response)) == ["body.name"]
    assert not Event.objects.exists()


def test_create_with_control_character_name_does_not_echo() -> None:
    organizer = verified_organizer()
    name = "Made-up\x07Bell Cup"
    response = _post(_client(_member(organizer)), _body(organizer, name=name))
    assert response.status_code == 422
    assert list(_field_errors(response)) == ["body.name"]
    assert "Bell Cup" not in response.content.decode()


@pytest.mark.parametrize(
    ("override", "field"),
    [
        ({"kind": "self_reported"}, "body.kind"),
        ({"kind": "Made-up kind"}, "body.kind"),
        ({"start_date": "Made-up date"}, "body.start_date"),
        ({"id": 5}, "body.id"),
    ],
)
def test_create_with_invalid_input_does_not_echo(
    override: dict[str, Any], field: str
) -> None:
    organizer = verified_organizer()
    response = _post(_client(_member(organizer)), _body(organizer, **override))
    assert response.status_code == 422
    assert field in _field_errors(response)
    assert "Made-up kind" not in response.content.decode()
    assert "Made-up date" not in response.content.decode()


# Change


def _event_with_member() -> tuple[Event, User]:
    organizer = verified_organizer()
    event = EventFactory.create(
        organizer=organizer,
        kind=EventKind.LEAGUE,
        start_date=date(2026, 3, 1),
        end_date=date(2026, 3, 31),
    )
    return event, _member(organizer)


def test_patch_changes_only_the_fields_sent() -> None:
    event, account = _event_with_member()
    response = _patch(
        _client(account), event.public_code, {"name": "  Made-up Fall Ladder  "}
    )
    assert response.status_code == 200
    assert response.json()["name"] == "Made-up Fall Ladder"
    assert response.json()["kind"] == "league"
    stored = Event.objects.get(pk=event.pk)
    assert stored.name == "Made-up Fall Ladder"
    assert stored.start_date == date(2026, 3, 1)
    [audit] = _audit(AuditAction.EVENT_CHANGE)
    assert audit.actor_user_id == account.pk
    assert set(audit.after or {}) == {"name"}


def test_patch_kind_on_event_with_voided_match() -> None:
    event, account = _event_with_member()
    match = _match_on(event, account)
    void_match(match, actor=account)
    response = _patch(_client(account), event.public_code, {"kind": "tournament"})
    assert response.status_code == 200
    assert Event.objects.get(pk=event.pk).kind == EventKind.TOURNAMENT
    assert len(_audit(AuditAction.EVENT_CHANGE)) == 1


@pytest.mark.parametrize(
    "change",
    [
        {"name": "Made-up Other Name"},
        {"start_date": "2026-02-28"},
        {"end_date": "2026-04-01"},
        {"kind": "tournament", "end_date": "2026-04-01"},
    ],
)
def test_patch_name_or_dates_on_event_with_match_is_409(
    change: dict[str, Any],
) -> None:
    event, account = _event_with_member()
    match = _match_on(event, account)
    void_match(match, actor=account)
    assert Match.objects.get(pk=match.pk).status == MatchStatus.VOIDED
    before = Event.objects.filter(pk=event.pk).values().get()

    response = _patch(_client(account), event.public_code, change)

    assert response.status_code == 409
    assert response.json() == {
        "code": "conflict",
        "message": LOCKED_TEXT,
        "errors": [],
    }
    assert Event.objects.filter(pk=event.pk).values().get() == before
    assert not _audit(AuditAction.EVENT_CHANGE)


def test_patch_unchanged_name_and_dates_on_event_with_match() -> None:
    event, account = _event_with_member()
    _match_on(event, account)
    response = _patch(
        _client(account),
        event.public_code,
        {
            "name": f" {event.name} ",
            "start_date": "2026-03-01",
            "end_date": "2026-03-31",
            "kind": "rec_verified",
        },
    )
    assert response.status_code == 200
    [audit] = _audit(AuditAction.EVENT_CHANGE)
    assert set(audit.after or {}) == {"kind"}


def test_patch_with_nothing_changed_records_nothing() -> None:
    event, account = _event_with_member()
    response = _patch(_client(account), event.public_code, {"kind": "league"})
    assert response.status_code == 200
    response = _patch(_client(account), event.public_code, {})
    assert response.status_code == 200
    assert not _audit(AuditAction.EVENT_CHANGE)


@pytest.mark.parametrize("field", ["name", "kind", "start_date", "end_date"])
def test_patch_refuses_null(field: str) -> None:
    event, account = _event_with_member()
    response = _patch(_client(account), event.public_code, {field: None})
    assert response.status_code == 422
    assert f"body.{field}" in _field_errors(response)


@pytest.mark.parametrize("field", ["organizer", "club", "code"])
def test_patch_refuses_other_keys(field: str) -> None:
    event, account = _event_with_member()
    response = _patch(
        _client(account), event.public_code, {field: event.organizer.public_code}
    )
    assert response.status_code == 422
    assert f"body.{field}" in _field_errors(response)
    assert event.organizer.public_code not in response.content.decode()


def test_patch_end_before_start_is_422() -> None:
    event, account = _event_with_member()
    response = _patch(_client(account), event.public_code, {"end_date": "2026-02-01"})
    assert response.status_code == 422
    assert _field_errors(response) == {
        "body.end_date": "The end date cannot be before the start date."
    }
    assert not _audit(AuditAction.EVENT_CHANGE)


def test_patch_blank_name_is_422() -> None:
    event, account = _event_with_member()
    response = _patch(_client(account), event.public_code, {"name": "  "})
    assert response.status_code == 422
    assert list(_field_errors(response)) == ["body.name"]


def test_patch_accepts_a_code_in_any_form() -> None:
    event, account = _event_with_member()
    loose = event.public_code.lower()
    response = _patch(_client(account), loose, {"kind": "tournament"})
    assert response.status_code == 200
    assert response.json()["code"] == event.public_code


# Refused actors get the unknown code's 404


def _refused_actors(event: Event) -> list[User]:
    unverified = OrganizerFactory.create()
    other = EventFactory.create(organizer=unverified)
    return [
        UserFactory.create(),
        _member(verified_organizer()),
        _member(other.organizer),
    ]


def _unverified_member_event() -> tuple[Event, User]:
    organizer = OrganizerFactory.create()
    event = EventFactory.create(organizer=organizer)
    return event, _member(organizer)


def test_patch_by_refused_actor_is_the_unknown_404() -> None:
    event, _ = _event_with_member()
    unverified_event, unverified_member = _unverified_member_event()
    unknown = _patch(_client(UserFactory.create()), UNKNOWN_EVENT, {"kind": "league"})
    malformed = _patch(_client(UserFactory.create()), "nope", {"kind": "league"})
    assert unknown.status_code == malformed.status_code == 404
    assert unknown.content == malformed.content
    refused = [(account, event) for account in _refused_actors(event)] + [
        (unverified_member, unverified_event)
    ]
    for account, target in refused:
        response = _patch(_client(account), target.public_code, {"kind": "tournament"})
        assert response.status_code == 404
        assert response.content == unknown.content
    assert Event.objects.get(pk=event.pk).kind == EventKind.LEAGUE
    assert not _audit(AuditAction.EVENT_CHANGE)


def test_delete_by_refused_actor_is_the_unknown_404() -> None:
    event, _ = _event_with_member()
    unverified_event, unverified_member = _unverified_member_event()
    unknown = _delete(_client(UserFactory.create()), UNKNOWN_EVENT)
    assert unknown.status_code == 404
    refused = [(account, event) for account in _refused_actors(event)] + [
        (unverified_member, unverified_event)
    ]
    for account, target in refused:
        response = _delete(_client(account), target.public_code)
        assert response.status_code == 404
        assert response.content == unknown.content
    assert Event.objects.filter(pk=event.pk).exists()
    assert Event.objects.filter(pk=unverified_event.pk).exists()
    assert not _audit(AuditAction.EVENT_DELETE)


def test_patch_and_delete_need_sign_in() -> None:
    event, _ = _event_with_member()
    assert _patch(Client(), event.public_code, {"kind": "league"}).status_code == 401
    assert _delete(Client(), event.public_code).status_code == 401


# Delete


def test_delete_event() -> None:
    event, account = _event_with_member()
    response = _delete(_client(account), event.public_code)
    assert response.status_code == 204
    assert not Event.objects.filter(pk=event.pk).exists()
    [audit] = _audit(AuditAction.EVENT_DELETE)
    assert audit.actor_user_id == account.pk
    assert _delete(_client(account), event.public_code).status_code == 404


def test_delete_event_with_match_is_409() -> None:
    event, account = _event_with_member()
    match = _match_on(event, account)
    void_match(match, actor=account)
    response = _delete(_client(account), event.public_code)
    assert response.status_code == 409
    assert response.json() == {"code": "conflict", "message": DELETE_TEXT, "errors": []}
    assert Event.objects.filter(pk=event.pk).exists()
    assert not _audit(AuditAction.EVENT_DELETE)


def test_delete_event_with_submission_batch_is_409() -> None:
    event, account = _event_with_member()
    SubmissionBatch.objects.create(submitted_by_user=account, event=event)
    response = _delete(_client(account), event.public_code)
    assert response.status_code == 409
    assert Event.objects.filter(pk=event.pk).exists()
    assert not _audit(AuditAction.EVENT_DELETE)


# An Event made through the API takes matches (the CSV upload path, #71)


def test_event_created_through_api_takes_a_counted_match() -> None:
    organizer = verified_organizer()
    account = _member(organizer, MemberRole.MATCH_MANAGER)
    response = _post(_client(account), _body(organizer, kind="tournament"))
    event = Event.objects.get(public_code=response.json()["code"])

    match = _match_on(event, account)

    assert match.status == MatchStatus.CONFIRMED
    assert match.match_type == EventKind.TOURNAMENT
    assert match.event_id == event.pk
