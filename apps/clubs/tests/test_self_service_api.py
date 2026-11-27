"""Organizer self-service operations at /api/v1/organizer/ (#229, ADR 0054).

Create, rename, invite, accept, members, and verification requests.
Every operation with an Organizer code in its path answers a non-member,
a match manager on an admin operation, and an unknown or malformed code
with one identical 404, and an Account without MFA with 403
``mfa_required`` before any lookup. Made-up data only.
"""

import json
from datetime import date
from typing import Any

import pytest
from allauth.account.models import EmailAddress
from django.test import Client

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory, enroll_totp
from apps.audit.models import AuditAction, AuditEvent
from apps.clubs.models import (
    MemberRole,
    Organizer,
    OrganizerInvite,
    OrganizerMember,
    OrganizerVerificationRequest,
)
from apps.clubs.tests.factories import (
    OrganizerFactory,
    OrganizerMemberFactory,
    verify,
)
from apps.emails.models import QueuedEmail
from apps.events.models import EventKind
from apps.events.tests.factories import EventFactory
from apps.matches.models import MatchFormat
from apps.matches.permissions import MatchPermissionDenied, can_manage_events
from apps.matches.services import record_match
from apps.players.tests.factories import PlayerFactory
from apps.public_ids.api import PublicCodePath
from apps.public_ids.codes import CodeType
from config.auth import MFA_REQUIRED_MESSAGE

pytestmark = pytest.mark.django_db

BASE = "/api/v1/organizer"
MALFORMED_CODE = "not-a-code"
NOT_FOUND = {"code": "not_found", "message": "Not found.", "errors": []}
INVITE_SENT = {
    "message": "If the address can be invited, an invite email is on its way."
}


def _account(*, mfa: bool = True, player: bool = True, verified: bool = True) -> User:
    account: User = UserFactory.create()
    EmailAddress.objects.create(
        user=account, email=account.email, verified=verified, primary=True
    )
    if mfa:
        enroll_totp(account)
    if player:
        PlayerFactory.create(account=account)
    return account


def _unknown_code(code_type: CodeType = CodeType.ORGANIZER) -> str:
    """A well-formed code no record has (the documentation's example)."""
    code = PublicCodePath(code_type).example
    assert not Organizer.objects.filter(public_code=code).exists()
    return code


def _client(account: User) -> Client:
    client = Client()
    client.force_login(account)
    return client


def _send(account: User, method: str, path: str, body: object = None) -> Any:
    client = _client(account)
    kwargs: dict[str, Any] = {}
    if body is not None:
        kwargs = {"data": json.dumps(body), "content_type": "application/json"}
    return getattr(client, method)(f"{BASE}{path}", **kwargs)


def _admin_of(organizer: Organizer) -> User:
    account = _account()
    OrganizerMemberFactory.create(
        organizer=organizer, account=account, role=MemberRole.ADMIN
    )
    return account


def _manager_of(organizer: Organizer) -> User:
    account = _account()
    OrganizerMemberFactory.create(organizer=organizer, account=account)
    return account


def _invite_key(admin: User, organizer: Organizer, email: str, role: str) -> str:
    response = _send(
        admin,
        "post",
        f"/organizers/{organizer.public_code}/invites",
        {"email": email, "role": role},
    )
    assert response.status_code == 202
    body = QueuedEmail.objects.filter(to=email).latest("pk").body
    return body.split("/organizer/invites/")[1].split()[0]


# Create (AC1-3)


def test_create_makes_the_account_the_only_admin_of_an_unverified_organizer() -> None:
    account = _account(mfa=False, player=False)

    response = _send(account, "post", "/organizers", {"name": " Made-up Ladder "})

    assert response.status_code == 201
    organizer = Organizer.objects.get()
    assert response.json() == {
        "organizer": organizer.public_code,
        "name": "Made-up Ladder",
        "verified": False,
        "verification_requested": False,
        "role": "admin",
    }
    assert organizer.verified_at is None
    assert list(OrganizerMember.objects.values_list("account_id", "role")) == [
        (account.pk, "admin")
    ]
    actions = sorted(AuditEvent.objects.values_list("action", flat=True))
    assert actions == ["organizer.create", "organizer_member.add"]
    for event in AuditEvent.objects.all():
        assert "Made-up Ladder" not in json.dumps([event.before, event.after])


def test_create_with_an_unverified_login_address_is_403_and_writes_nothing() -> None:
    account = _account(verified=False)

    response = _send(account, "post", "/organizers", {"name": "Made-up Ladder"})

    assert response.status_code == 403
    assert response.json()["message"] == (
        "Verify your email address before creating an organizer."
    )
    assert not Organizer.objects.exists()
    assert not AuditEvent.objects.exists()


def test_create_past_the_limit_is_409(settings: Any) -> None:
    settings.ORGANIZER_CREATE_LIMIT_PER_ACCOUNT = 1
    account = _account()
    assert _send(account, "post", "/organizers", {"name": "One"}).status_code == 201

    response = _send(account, "post", "/organizers", {"name": "Two"})

    assert response.status_code == 409
    assert Organizer.objects.count() == 1


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("   ", "Enter a name."),
        ("x" * 201, "Keep the name to 200 characters or fewer."),
        ("Made-up\x07Bell", "Remove control or invalid characters."),
        (12, "Enter text."),
    ],
)
def test_a_bad_name_is_422_with_fixed_text(name: object, message: str) -> None:
    response = _send(_account(), "post", "/organizers", {"name": name})

    assert response.status_code == 422
    [error] = response.json()["errors"]
    assert error["field"] == "body.payload.name" or error["field"].endswith("name")
    assert error["message"] == message
    assert not Organizer.objects.exists()


# Organizer-code operations: identical 404s, MFA first (AC4)


def _code_operations(code: str, player: str) -> list[tuple[str, str, object]]:
    return [
        ("patch", f"/organizers/{code}", {"name": "Made-up Rename"}),
        (
            "post",
            f"/organizers/{code}/invites",
            {"email": "x@example.com", "role": "admin"},
        ),
        ("get", f"/organizers/{code}/members", None),
        ("patch", f"/organizers/{code}/members/{player}", {"role": "admin"}),
        ("delete", f"/organizers/{code}/members/{player}", None),
        ("post", f"/organizers/{code}/verification-request", {"statement": "Ladder."}),
    ]


def _refusals(actor: User, code: str, player: str) -> list[tuple[int, Any]]:
    answers = []
    for method, path, body in _code_operations(code, player):
        response = _send(actor, method, path, body)
        answers.append((response.status_code, response.json()))
    return answers


def test_non_member_match_manager_unknown_and_malformed_codes_get_one_404() -> None:
    organizer = OrganizerFactory.create(name="Made-up Hidden")
    admin = _admin_of(organizer)
    manager = _manager_of(organizer)
    outsider = _account()
    player = admin.player.public_code
    expected = [(404, NOT_FOUND)] * 6

    assert _refusals(outsider, organizer.public_code, player) == expected
    assert _refusals(manager, organizer.public_code, player) == expected
    assert _refusals(admin, _unknown_code(), player) == expected
    assert _refusals(admin, MALFORMED_CODE, player) == expected
    organizer.refresh_from_db()
    assert organizer.name == "Made-up Hidden"
    assert not OrganizerInvite.objects.exists()
    assert not OrganizerVerificationRequest.objects.exists()
    assert OrganizerMember.objects.count() == 2
    assert not QueuedEmail.objects.exists()


def test_an_account_without_mfa_gets_mfa_required_on_every_code() -> None:
    organizer = OrganizerFactory.create()
    member = _account(mfa=False)
    OrganizerMemberFactory.create(
        organizer=organizer, account=member, role=MemberRole.ADMIN, mfa=False
    )
    expected = (
        403,
        {"code": "mfa_required", "message": MFA_REQUIRED_MESSAGE, "errors": []},
    )
    player = member.player.public_code

    for code in (organizer.public_code, _unknown_code()):
        answers = _refusals(member, code, player)
        assert answers == [expected] * 6


def test_an_unknown_member_player_is_the_same_404() -> None:
    organizer = OrganizerFactory.create()
    admin = _admin_of(organizer)
    elsewhere = _manager_of(OrganizerFactory.create())
    code = organizer.public_code

    for method, body in (("patch", {"role": "admin"}), ("delete", None)):
        for player in (
            elsewhere.player.public_code,
            _unknown_code(CodeType.PLAYER),
            "bad",
        ):
            response = _send(
                admin, method, f"/organizers/{code}/members/{player}", body
            )
            assert (response.status_code, response.json()) == (404, NOT_FOUND)


# Rename (AC5)


def test_an_admin_renames_with_a_field_name_event() -> None:
    organizer = OrganizerFactory.create(name="Made-up Old")
    admin = _admin_of(organizer)

    response = _send(
        admin, "patch", f"/organizers/{organizer.public_code}", {"name": "Made-up New"}
    )

    assert response.status_code == 200
    assert response.json()["name"] == "Made-up New"
    [event] = AuditEvent.objects.filter(action=AuditAction.ORGANIZER_CHANGE)
    assert event.after == {"fields": ["name"]}


def test_an_unchanged_name_records_nothing() -> None:
    organizer = OrganizerFactory.create(name="Made-up Same")
    admin = _admin_of(organizer)

    response = _send(
        admin, "patch", f"/organizers/{organizer.public_code}", {"name": "Made-up Same"}
    )

    assert response.status_code == 200
    assert not AuditEvent.objects.filter(action=AuditAction.ORGANIZER_CHANGE).exists()


# Invites (AC6)


def test_the_invite_answer_is_the_same_for_any_address() -> None:
    organizer = OrganizerFactory.create()
    admin = _admin_of(organizer)
    with_account = _account()
    member = _manager_of(organizer)
    code = organizer.public_code
    answers = []
    for email in (with_account.email, "nobody.made-up@example.com", member.email):
        response = _send(
            admin,
            "post",
            f"/organizers/{code}/invites",
            {"email": email, "role": "admin"},
        )
        answers.append((response.status_code, response.json()))
    # Already invited: the same again.
    response = _send(
        admin,
        "post",
        f"/organizers/{code}/invites",
        {"email": with_account.email, "role": "match_manager"},
    )
    answers.append((response.status_code, response.json()))

    assert answers == [(202, INVITE_SENT)] * 4
    assert QueuedEmail.objects.filter(to=with_account.email).count() == 2
    assert QueuedEmail.objects.filter(to="nobody.made-up@example.com").count() == 1
    assert not QueuedEmail.objects.filter(to=member.email).exists()
    assert OrganizerInvite.objects.count() == 2
    for event in AuditEvent.objects.filter(action=AuditAction.ORGANIZER_INVITE_CREATE):
        text = json.dumps([event.before, event.after, event.reason])
        assert "@" not in text


def test_the_invite_email_has_the_link_and_no_inviter() -> None:
    organizer = OrganizerFactory.create(name="Made-up Secret Name")
    admin = _admin_of(organizer)

    key = _invite_key(admin, organizer, "new.made-up@example.com", "admin")

    [email] = QueuedEmail.objects.all()
    assert f"/organizer/invites/{key}" in email.body
    for part in (email.subject, email.body, email.html):
        assert admin.email not in part
        assert "Made-up Secret Name" not in part
        assert admin.player.display_name not in part


def test_an_invalid_address_is_422_with_fixed_text() -> None:
    organizer = OrganizerFactory.create()
    admin = _admin_of(organizer)

    response = _send(
        admin,
        "post",
        f"/organizers/{organizer.public_code}/invites",
        {"email": "not an address", "role": "admin"},
    )

    assert response.status_code == 422
    [error] = response.json()["errors"]
    assert error["message"] == "Enter a valid email address."
    assert "not an address" not in json.dumps(response.json())


def test_open_invites_past_the_cap_are_409(settings: Any) -> None:
    settings.ORGANIZER_OPEN_INVITES_LIMIT = 1
    organizer = OrganizerFactory.create()
    admin = _admin_of(organizer)
    path = f"/organizers/{organizer.public_code}/invites"
    _send(admin, "post", path, {"email": "a@example.com", "role": "admin"})

    response = _send(admin, "post", path, {"email": "b@example.com", "role": "admin"})

    assert response.status_code == 409


def test_the_invite_has_its_own_day_throttle(settings: Any) -> None:
    settings.API_ORGANIZER_INVITE_THROTTLE_PER_DAY = 2
    organizer = OrganizerFactory.create()
    admin = _admin_of(organizer)
    path = f"/organizers/{organizer.public_code}/invites"
    statuses = [
        _send(
            admin, "post", path, {"email": f"t{n}@example.com", "role": "admin"}
        ).status_code
        for n in range(3)
    ]

    assert statuses == [202, 202, 429]


# Accept (AC7, AC14)


def test_accept_makes_the_account_a_member_with_the_role() -> None:
    organizer = OrganizerFactory.create(name="Made-up Club Ladder")
    admin = _admin_of(organizer)
    invitee = _account(mfa=False)
    key = _invite_key(admin, organizer, invitee.email, "match_manager")

    response = _send(invitee, "post", "/invites/accept", {"key": key})

    assert response.status_code == 200
    assert response.json() == {
        "organizer": organizer.public_code,
        "name": "Made-up Club Ladder",
        "verified": False,
        "verification_requested": False,
        "role": "match_manager",
    }
    assert not OrganizerInvite.objects.exists()
    # The key works once.
    again = _send(invitee, "post", "/invites/accept", {"key": key})
    assert (again.status_code, again.json()) == (404, NOT_FOUND)


def test_every_accept_refusal_is_one_404() -> None:
    organizer = OrganizerFactory.create()
    admin = _admin_of(organizer)
    intended = _account()
    key = _invite_key(admin, organizer, intended.email, "admin")
    unverified = _account(verified=False)
    unverified_key = _invite_key(admin, organizer, unverified.email, "admin")
    no_player = _account(player=False)
    no_player_key = _invite_key(admin, organizer, no_player.email, "admin")

    answers = [
        _send(_account(), "post", "/invites/accept", {"key": key}),
        _send(intended, "post", "/invites/accept", {"key": "made-up-wrong"}),
        _send(intended, "post", "/invites/accept", {"key": "x" * 500}),
        _send(unverified, "post", "/invites/accept", {"key": unverified_key}),
        _send(no_player, "post", "/invites/accept", {"key": no_player_key}),
    ]

    assert [(a.status_code, a.json()) for a in answers] == [(404, NOT_FOUND)] * 5
    assert OrganizerMember.objects.count() == 1


def test_the_verifier_cannot_accept() -> None:
    from django.contrib.auth.models import Permission

    organizer = OrganizerFactory.create()
    admin = _admin_of(organizer)
    verifier = _account()
    verifier.user_permissions.add(Permission.objects.get(codename="verify_organizer"))
    from apps.clubs.services import verify_organizer

    verify_organizer(organizer, actor=User.objects.get(pk=verifier.pk))
    key = _invite_key(admin, organizer, verifier.email, "admin")

    response = _send(verifier, "post", "/invites/accept", {"key": key})

    assert (response.status_code, response.json()) == (404, NOT_FOUND)
    assert not OrganizerMember.objects.filter(account=verifier).exists()


def test_accept_by_a_member_is_409_and_uses_the_invite() -> None:
    organizer = OrganizerFactory.create()
    admin = _admin_of(organizer)
    invitee = _account()
    key = _invite_key(admin, organizer, invitee.email, "admin")
    OrganizerMemberFactory.create(organizer=organizer, account=invitee)

    response = _send(invitee, "post", "/invites/accept", {"key": key})

    assert response.status_code == 409
    assert not OrganizerInvite.objects.exists()


# Members (AC8, AC9)


def test_members_list_shows_players_and_never_an_email() -> None:
    organizer = OrganizerFactory.create()
    admin = _admin_of(organizer)
    manager = _manager_of(organizer)

    response = _send(admin, "get", f"/organizers/{organizer.public_code}/members")

    assert response.status_code == 200
    assert response.json() == [
        {
            "player": admin.player.public_code,
            "display_name": admin.player.display_name,
            "role": "admin",
            "you": True,
        },
        {
            "player": manager.player.public_code,
            "display_name": manager.player.display_name,
            "role": "match_manager",
            "you": False,
        },
    ]
    text = response.content.decode()
    assert admin.email not in text
    assert manager.email not in text


def test_change_role_and_remove_a_member() -> None:
    organizer = OrganizerFactory.create()
    admin = _admin_of(organizer)
    manager = _manager_of(organizer)
    path = f"/organizers/{organizer.public_code}/members/{manager.player.public_code}"

    changed = _send(admin, "patch", path, {"role": "admin"})
    removed = _send(admin, "delete", path)

    assert changed.status_code == 200
    assert changed.json()["role"] == "admin"
    assert changed.json()["you"] is False
    assert removed.status_code == 204
    assert not OrganizerMember.objects.filter(account=manager).exists()


def test_the_last_admin_cannot_be_demoted_or_removed() -> None:
    organizer = OrganizerFactory.create()
    admin = _admin_of(organizer)
    path = f"/organizers/{organizer.public_code}/members/{admin.player.public_code}"

    demoted = _send(admin, "patch", path, {"role": "match_manager"})
    removed = _send(admin, "delete", path)

    assert demoted.status_code == 409
    assert removed.status_code == 409
    assert demoted.json()["message"] == "An organizer must keep at least one admin."
    assert OrganizerMember.objects.get(account=admin).role == "admin"


# Verification requests (AC10, AC12, AC13)


def test_request_verification_then_a_second_is_409() -> None:
    organizer = OrganizerFactory.create()
    admin = _admin_of(organizer)
    path = f"/organizers/{organizer.public_code}/verification-request"

    first = _send(admin, "post", path, {"statement": "Made-up\nweekly ladder.\t"})
    second = _send(admin, "post", path, {"statement": "Again."})

    assert first.status_code == 201
    assert first.json()["organizer"] == organizer.public_code
    assert "statement" not in first.json()
    assert second.status_code == 409
    assert OrganizerVerificationRequest.objects.get().statement == (
        "Made-up\nweekly ladder."
    )
    listed = _send(admin, "get", "/organizers").json()
    assert listed[0]["verification_requested"] is True
    [event] = AuditEvent.objects.filter(
        action=AuditAction.ORGANIZER_VERIFICATION_REQUEST_CREATE
    )
    assert event.after == {"organizer": organizer.public_code}


def test_a_verified_organizer_request_is_409() -> None:
    organizer = OrganizerFactory.create()
    admin = _admin_of(organizer)
    verify(organizer)

    response = _send(
        admin,
        "post",
        f"/organizers/{organizer.public_code}/verification-request",
        {"statement": "Made-up."},
    )

    assert response.status_code == 409


@pytest.mark.parametrize(
    ("statement", "message"),
    [
        ("  ", "Describe what the organizer runs."),
        ("x" * 1001, "Keep the statement to 1000 characters or fewer."),
        ("Made-up\x00nul", "Remove control or invalid characters."),
    ],
)
def test_a_bad_statement_is_422_with_fixed_text(statement: str, message: str) -> None:
    organizer = OrganizerFactory.create()
    admin = _admin_of(organizer)

    response = _send(
        admin,
        "post",
        f"/organizers/{organizer.public_code}/verification-request",
        {"statement": statement},
    )

    assert response.status_code == 422
    [error] = response.json()["errors"]
    assert error["message"] == message
    assert not OrganizerVerificationRequest.objects.exists()


def test_a_self_made_organizer_gives_no_authority_until_verified() -> None:
    account = _account()
    created = _send(account, "post", "/organizers", {"name": "Made-up Ladder"})
    organizer = Organizer.objects.get(public_code=created.json()["organizer"])
    invitee = _account()
    key = _invite_key(account, organizer, invitee.email, "match_manager")
    _send(invitee, "post", "/invites/accept", {"key": key})
    _send(
        account,
        "post",
        f"/organizers/{organizer.public_code}/verification-request",
        {"statement": "Made-up ladder."},
    )
    event = EventFactory.create(organizer=organizer)

    def record(submitter: User) -> object:
        return record_match(
            format=MatchFormat.SINGLES,
            team_a=[PlayerFactory.create()],
            team_b=[PlayerFactory.create()],
            games=((11, 7),),
            played_on=date(2026, 6, 1),
            event=event,
            submitted_by_user=submitter,
        )

    for member in (account, invitee):
        assert can_manage_events(member, organizer.pk) is False
        with pytest.raises(MatchPermissionDenied):
            record(member)
    events = _send(
        account,
        "post",
        "/events",
        {
            "organizer": organizer.public_code,
            "name": "Made-up Night",
            "kind": EventKind.LEAGUE,
            "start_date": "2026-11-01",
            "end_date": "2026-11-01",
        },
    )
    assert events.status_code == 403

    verify(organizer)
    assert can_manage_events(account, organizer.pk) is True
    record(account)
