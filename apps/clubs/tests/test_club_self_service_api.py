"""Club self-service operations at /api/v1/clubs/ (#230).

Create, read, profile changes, admin invites and accepting, the admin
list and removal, and Club Organizer Grants, plus the Organizer side
grant list at /api/v1/organizer/. Every operation with a Club code in
its path that changes the Club or lists admin data answers a non-admin
and an unknown or malformed code with one identical 404, and an Account
without MFA with 403 ``mfa_required`` before any lookup. Made-up data
only.
"""

import json
from collections.abc import Iterator
from datetime import datetime
from typing import Any

import pytest
from allauth.account.models import EmailAddress
from django.core.cache import cache
from django.test import Client

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory, enroll_totp
from apps.audit.models import AuditAction, AuditEvent
from apps.clubs.models import (
    Club,
    ClubInvite,
    ClubOrganizerGrant,
    ClubRole,
    MemberRole,
)
from apps.clubs.tests.factories import (
    ClubFactory,
    ClubOrganizerGrantFactory,
    ClubRoleFactory,
    OrganizerFactory,
    OrganizerMemberFactory,
)
from apps.emails.models import QueuedEmail
from apps.players.tests.factories import PlayerFactory
from apps.public_ids.api import PublicCodePath
from apps.public_ids.codes import CodeType
from config.api import api
from config.auth import MFA_REQUIRED_MESSAGE

pytestmark = pytest.mark.django_db

BASE = "/api/v1/clubs"
MALFORMED_CODE = "not-a-code"
NOT_FOUND = {"code": "not_found", "message": "Not found.", "errors": []}
INVITE_SENT = {
    "message": "If the address can be invited, an invite email is on its way."
}
MFA_REQUIRED = (
    403,
    {"code": "mfa_required", "message": MFA_REQUIRED_MESSAGE, "errors": []},
)


@pytest.fixture(autouse=True)
def _clear_cache() -> Iterator[None]:
    """The API throttles use the cache."""
    cache.clear()
    yield
    cache.clear()


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


def _unknown_code(code_type: CodeType = CodeType.CLUB) -> str:
    """A well-formed code no record has (the documentation's example)."""
    code = PublicCodePath(code_type).example
    assert not Club.objects.filter(public_code=code).exists()
    return code


def _send(account: User | None, method: str, path: str, body: object = None) -> Any:
    client = Client()
    if account is not None:
        client.force_login(account)
    kwargs: dict[str, Any] = {}
    if body is not None:
        kwargs = {"data": json.dumps(body), "content_type": "application/json"}
    return getattr(client, method)(path, **kwargs)


def _admin_of(club: Club) -> User:
    account = _account()
    ClubRoleFactory.create(club=club, account=account)
    return account


def _invite_key(admin: User, club: Club, email: str) -> str:
    response = _send(
        admin, "post", f"{BASE}/{club.public_code}/invites", {"email": email}
    )
    assert response.status_code == 202
    body = QueuedEmail.objects.filter(to=email).latest("pk").body
    return body.split("/clubs/invites/")[1].split()[0]


def _when(text: str) -> datetime:
    return datetime.fromisoformat(text)


def _millis(moment: datetime) -> datetime:
    """``moment`` as the API renders it, to the millisecond."""
    return moment.replace(microsecond=moment.microsecond // 1000 * 1000)


def _club_out(club: Club) -> dict[str, str]:
    club.refresh_from_db()
    return {
        "club": club.public_code,
        "name": club.name,
        "city": club.city,
        "region": club.region,
    }


# Create (AC1)


def test_create_makes_the_account_the_only_admin() -> None:
    account = _account(mfa=False, player=False)

    response = _send(
        account,
        "post",
        BASE,
        {"name": " Made-up Courts ", "city": "Made-up City", "region": "MU"},
    )

    assert response.status_code == 201
    club = Club.objects.get()
    assert response.json() == {
        "club": club.public_code,
        "name": "Made-up Courts",
        "city": "Made-up City",
        "region": "MU",
    }
    assert list(ClubRole.objects.values_list("account_id", "role")) == [
        (account.pk, "admin")
    ]
    actions = sorted(AuditEvent.objects.values_list("action", flat=True))
    assert actions == ["club.create", "club_role.add"]


def test_create_needs_only_a_name() -> None:
    response = _send(_account(), "post", BASE, {"name": "Made-up Courts"})

    assert response.status_code == 201
    assert response.json()["city"] == ""
    assert response.json()["region"] == ""


def test_create_with_an_unverified_login_address_is_403_and_writes_nothing() -> None:
    response = _send(_account(verified=False), "post", BASE, {"name": "Made-up"})

    assert response.status_code == 403
    assert response.json()["message"] == (
        "Verify your email address before creating a club."
    )
    assert not Club.objects.exists()
    assert not AuditEvent.objects.exists()


def test_create_past_the_limit_is_409(settings: Any) -> None:
    settings.CLUB_CREATE_LIMIT_PER_ACCOUNT = 1
    account = _account()
    assert _send(account, "post", BASE, {"name": "One"}).status_code == 201

    response = _send(account, "post", BASE, {"name": "Two"})

    assert response.status_code == 409
    assert Club.objects.count() == 1


def test_create_signed_out_is_401() -> None:
    assert _send(None, "post", BASE, {"name": "Made-up"}).status_code == 401


@pytest.mark.parametrize(
    ("body", "field", "message"),
    [
        ({"name": "   "}, "name", "Enter a name."),
        ({"name": "x" * 201}, "name", "Keep the name to 200 characters or fewer."),
        ({"name": "Made-up\x07Bell"}, "name", "Remove control or invalid characters."),
        ({"name": 12}, "name", "Enter text."),
        (
            {"name": "Made-up", "city": "x" * 101},
            "city",
            "Keep the city to 100 characters or fewer.",
        ),
        (
            {"name": "Made-up", "region": "Made-up\x00"},
            "region",
            "Remove control or invalid characters.",
        ),
    ],
)
def test_a_bad_value_is_422_with_fixed_text(
    body: dict[str, object], field: str, message: str
) -> None:
    response = _send(_account(), "post", BASE, body)

    assert response.status_code == 422
    [error] = response.json()["errors"]
    assert error["field"].endswith(field)
    assert error["message"] == message
    assert not Club.objects.exists()


def test_create_refuses_unknown_keys() -> None:
    response = _send(_account(), "post", BASE, {"name": "Made-up", "id": 1})

    assert response.status_code == 422
    assert not Club.objects.exists()


# Read (AC2)


def test_any_signed_in_account_reads_a_club() -> None:
    club = ClubFactory.create(name="Made-up Courts", city="Made-up City")

    response = _send(
        _account(mfa=False, player=False), "get", f"{BASE}/{club.public_code}"
    )

    assert response.status_code == 200
    assert response.json() == _club_out(club)


def test_reading_an_unknown_or_malformed_club_is_404_and_signed_out_401() -> None:
    account = _account()
    club = ClubFactory.create()

    for code in (_unknown_code(), MALFORMED_CODE):
        response = _send(account, "get", f"{BASE}/{code}")
        assert (response.status_code, response.json()) == (404, NOT_FOUND)
    assert _send(None, "get", f"{BASE}/{club.public_code}").status_code == 401


# Club-code operations: identical 404s, MFA first (AC8)


def _code_operations(
    code: str, player: str, organizer: str
) -> list[tuple[str, str, object]]:
    return [
        ("patch", f"{BASE}/{code}", {"name": "Made-up Rename"}),
        ("post", f"{BASE}/{code}/invites", {"email": "x@example.com"}),
        ("get", f"{BASE}/{code}/admins", None),
        ("delete", f"{BASE}/{code}/admins/{player}", None),
        ("get", f"{BASE}/{code}/organizer-grants", None),
        ("post", f"{BASE}/{code}/organizer-grants", {"organizer": organizer}),
        ("delete", f"{BASE}/{code}/organizer-grants/{organizer}", None),
    ]


def _refusals(
    actor: User, code: str, player: str, organizer: str
) -> list[tuple[int, Any]]:
    answers = []
    for method, path, body in _code_operations(code, player, organizer):
        response = _send(actor, method, path, body)
        answers.append((response.status_code, response.json()))
    return answers


def test_a_non_admin_unknown_and_malformed_codes_get_one_404() -> None:
    club = ClubFactory.create(name="Made-up Hidden")
    admin = _admin_of(club)
    organizer = OrganizerFactory.create()
    grant = ClubOrganizerGrantFactory.create(club=club, organizer=organizer)
    outsider = _account()
    player = admin.player.public_code
    code = organizer.public_code
    expected = [(404, NOT_FOUND)] * 7

    assert _refusals(outsider, club.public_code, player, code) == expected
    assert _refusals(admin, _unknown_code(), player, code) == expected
    assert _refusals(admin, MALFORMED_CODE, player, code) == expected
    club.refresh_from_db()
    assert club.name == "Made-up Hidden"
    assert not ClubInvite.objects.exists()
    assert ClubRole.objects.count() == 1
    assert list(ClubOrganizerGrant.objects.values_list("pk", flat=True)) == [grant.pk]
    assert not QueuedEmail.objects.exists()


def test_an_account_without_mfa_gets_mfa_required_on_every_code() -> None:
    club = ClubFactory.create()
    admin = _account(mfa=False)
    ClubRoleFactory.create(club=club, account=admin)
    organizer = OrganizerFactory.create().public_code
    player = admin.player.public_code

    for code in (club.public_code, _unknown_code()):
        assert _refusals(admin, code, player, organizer) == [MFA_REQUIRED] * 7


def test_an_unknown_admin_player_or_grant_is_the_same_404() -> None:
    club = ClubFactory.create()
    admin = _admin_of(club)
    elsewhere = _admin_of(ClubFactory.create())
    not_granted = OrganizerFactory.create()
    code = club.public_code

    for player in (elsewhere.player.public_code, _unknown_code(CodeType.PLAYER), "bad"):
        response = _send(admin, "delete", f"{BASE}/{code}/admins/{player}")
        assert (response.status_code, response.json()) == (404, NOT_FOUND)
    for organizer in (
        not_granted.public_code,
        _unknown_code(CodeType.ORGANIZER),
        "bad",
    ):
        response = _send(admin, "delete", f"{BASE}/{code}/organizer-grants/{organizer}")
        assert (response.status_code, response.json()) == (404, NOT_FOUND)


# Profile changes (AC3)


def test_an_admin_changes_the_profile_with_a_field_name_event() -> None:
    club = ClubFactory.create(name="Made-up Old", city="Made-up Town", region="AA")
    admin = _admin_of(club)

    response = _send(
        admin,
        "patch",
        f"{BASE}/{club.public_code}",
        {"name": "Made-up New", "region": ""},
    )

    assert response.status_code == 200
    assert response.json() == {
        "club": club.public_code,
        "name": "Made-up New",
        "city": "Made-up Town",
        "region": "",
    }
    [event] = AuditEvent.objects.filter(action=AuditAction.CLUB_CHANGE)
    assert event.before is None
    assert event.after == {"fields": ["name", "region"]}


def test_an_unchanged_or_empty_patch_records_nothing() -> None:
    club = ClubFactory.create(name="Made-up Same")
    admin = _admin_of(club)
    path = f"{BASE}/{club.public_code}"

    same = _send(admin, "patch", path, {"name": "Made-up Same"})
    empty = _send(admin, "patch", path, {})

    assert same.status_code == 200
    assert empty.status_code == 200
    assert empty.json() == _club_out(club)
    assert not AuditEvent.objects.filter(action=AuditAction.CLUB_CHANGE).exists()


def test_a_null_name_in_a_patch_changes_nothing() -> None:
    club = ClubFactory.create(name="Made-up Kept")
    admin = _admin_of(club)

    response = _send(admin, "patch", f"{BASE}/{club.public_code}", {"name": None})

    assert response.status_code == 200
    assert response.json()["name"] == "Made-up Kept"


def test_a_blank_name_in_a_patch_is_422() -> None:
    club = ClubFactory.create(name="Made-up Kept")
    admin = _admin_of(club)

    response = _send(admin, "patch", f"{BASE}/{club.public_code}", {"name": " "})

    assert response.status_code == 422
    assert response.json()["errors"][0]["message"] == "Enter a name."
    club.refresh_from_db()
    assert club.name == "Made-up Kept"


# Invites (AC4)


def test_the_invite_answer_is_the_same_for_any_address() -> None:
    club = ClubFactory.create()
    admin = _admin_of(club)
    with_account = _account()
    other_admin = _admin_of(club)
    path = f"{BASE}/{club.public_code}/invites"
    answers = []
    for email in (
        with_account.email,
        "nobody.made-up@example.com",
        other_admin.email,
        with_account.email,
    ):
        response = _send(admin, "post", path, {"email": email})
        answers.append((response.status_code, response.json()))

    assert answers == [(202, INVITE_SENT)] * 4
    assert QueuedEmail.objects.filter(to=with_account.email).count() == 2
    assert QueuedEmail.objects.filter(to="nobody.made-up@example.com").count() == 1
    assert not QueuedEmail.objects.filter(to=other_admin.email).exists()
    assert ClubInvite.objects.count() == 2
    for event in AuditEvent.objects.filter(action=AuditAction.CLUB_INVITE_CREATE):
        assert "@" not in json.dumps([event.before, event.after, event.reason])


def test_the_invite_email_has_the_code_and_link_and_no_inviter_or_name() -> None:
    club = ClubFactory.create(name="Made-up Secret Courts")
    admin = _admin_of(club)

    key = _invite_key(admin, club, "new.made-up@example.com")

    [email] = QueuedEmail.objects.all()
    assert f"/clubs/invites/{key}" in email.body
    assert club.public_code in email.body
    for part in (email.subject, email.body, email.html):
        assert admin.email not in part
        assert "Made-up Secret Courts" not in part
        assert admin.player.display_name not in part


def test_an_invalid_address_is_422_with_fixed_text() -> None:
    club = ClubFactory.create()
    admin = _admin_of(club)

    response = _send(
        admin, "post", f"{BASE}/{club.public_code}/invites", {"email": "not an address"}
    )

    assert response.status_code == 422
    [error] = response.json()["errors"]
    assert error["message"] == "Enter a valid email address."
    assert "not an address" not in json.dumps(response.json())


def test_open_invites_past_the_cap_are_409(settings: Any) -> None:
    settings.CLUB_OPEN_INVITES_LIMIT = 1
    club = ClubFactory.create()
    admin = _admin_of(club)
    path = f"{BASE}/{club.public_code}/invites"
    _send(admin, "post", path, {"email": "a@example.com"})

    response = _send(admin, "post", path, {"email": "b@example.com"})

    assert response.status_code == 409
    assert response.json()["message"] == (
        "This club has too many open invites. Wait until some are accepted or expire."
    )


def test_the_invite_has_its_own_day_throttle(settings: Any) -> None:
    settings.API_CLUB_INVITE_THROTTLE_PER_DAY = 2
    club = ClubFactory.create()
    admin = _admin_of(club)
    path = f"{BASE}/{club.public_code}/invites"
    statuses = [
        _send(admin, "post", path, {"email": f"t{n}@example.com"}).status_code
        for n in range(3)
    ]

    assert statuses == [202, 202, 429]
    # The Account's other operations are not counted against it.
    assert _send(admin, "get", f"{BASE}/{club.public_code}/admins").status_code == 200


# Accept (AC4)


def test_accept_makes_the_account_an_admin() -> None:
    club = ClubFactory.create(name="Made-up Courts")
    admin = _admin_of(club)
    invitee = _account(mfa=False)
    key = _invite_key(admin, club, invitee.email)

    response = _send(invitee, "post", f"{BASE}/invites/accept", {"key": key})

    assert response.status_code == 200
    assert response.json() == _club_out(club)
    assert ClubRole.objects.filter(club=club, account=invitee).exists()
    assert not ClubInvite.objects.exists()
    again = _send(invitee, "post", f"{BASE}/invites/accept", {"key": key})
    assert (again.status_code, again.json()) == (404, NOT_FOUND)


def test_every_accept_refusal_is_one_404() -> None:
    club = ClubFactory.create()
    admin = _admin_of(club)
    intended = _account()
    key = _invite_key(admin, club, intended.email)
    unverified = _account(verified=False)
    unverified_key = _invite_key(admin, club, unverified.email)
    no_player = _account(player=False)
    no_player_key = _invite_key(admin, club, no_player.email)
    path = f"{BASE}/invites/accept"

    answers = [
        _send(_account(), "post", path, {"key": key}),
        _send(intended, "post", path, {"key": "made-up-wrong"}),
        _send(intended, "post", path, {"key": "x" * 500}),
        _send(unverified, "post", path, {"key": unverified_key}),
        _send(no_player, "post", path, {"key": no_player_key}),
    ]

    assert [(a.status_code, a.json()) for a in answers] == [(404, NOT_FOUND)] * 5
    assert ClubRole.objects.count() == 1


def test_accept_by_an_admin_is_409_and_uses_the_invite() -> None:
    club = ClubFactory.create()
    admin = _admin_of(club)
    invitee = _account()
    key = _invite_key(admin, club, invitee.email)
    ClubRoleFactory.create(club=club, account=invitee)

    response = _send(invitee, "post", f"{BASE}/invites/accept", {"key": key})

    assert response.status_code == 409
    assert response.json()["message"] == "You are already an admin of this club."
    assert not ClubInvite.objects.exists()


# Admins (AC4)


def test_the_admin_list_shows_players_and_never_an_email() -> None:
    club = ClubFactory.create()
    admin = _admin_of(club)
    other = _admin_of(club)

    response = _send(admin, "get", f"{BASE}/{club.public_code}/admins")

    assert response.status_code == 200
    expected = sorted(
        [
            {
                "player": admin.player.public_code,
                "display_name": admin.player.display_name,
                "you": True,
            },
            {
                "player": other.player.public_code,
                "display_name": other.player.display_name,
                "you": False,
            },
        ],
        key=lambda row: str(row["player"]),
    )
    assert response.json() == expected
    text = response.content.decode()
    assert admin.email not in text
    assert other.email not in text


def test_remove_an_admin_then_the_last_admin_is_409() -> None:
    club = ClubFactory.create()
    admin = _admin_of(club)
    other = _admin_of(club)
    _invite_key(other, club, "from.other@example.com")

    removed = _send(
        admin, "delete", f"{BASE}/{club.public_code}/admins/{other.player.public_code}"
    )
    last = _send(
        admin, "delete", f"{BASE}/{club.public_code}/admins/{admin.player.public_code}"
    )

    assert removed.status_code == 204
    assert not ClubRole.objects.filter(account=other).exists()
    assert not ClubInvite.objects.exists()
    assert last.status_code == 409
    assert last.json()["message"] == "A club must keep at least one admin."
    assert ClubRole.objects.filter(account=admin).exists()


# Grants (AC5)


def test_grant_list_and_remove_an_organizer_from_the_club_side() -> None:
    club = ClubFactory.create()
    admin = _admin_of(club)
    organizer = OrganizerFactory.create(name="Made-up Hidden Organizer")
    path = f"{BASE}/{club.public_code}/organizer-grants"

    granted = _send(admin, "post", path, {"organizer": organizer.public_code.lower()})
    again = _send(admin, "post", path, {"organizer": organizer.public_code})
    listed = _send(admin, "get", path)

    assert granted.status_code == 201
    grant = ClubOrganizerGrant.objects.get()
    assert granted.json()["organizer"] == organizer.public_code
    assert _when(granted.json()["created_at"]) == _millis(grant.created_at)
    assert again.status_code == 409
    assert listed.status_code == 200
    assert listed.json() == [granted.json()]
    assert "Made-up Hidden Organizer" not in listed.content.decode()

    removed = _send(admin, "delete", f"{path}/{organizer.public_code}")

    assert removed.status_code == 204
    assert not ClubOrganizerGrant.objects.exists()
    actions = list(
        AuditEvent.objects.filter(action__startswith="club_organizer_grant.")
        .order_by("pk")
        .values_list("action", "actor_user_id")
    )
    assert actions == [
        ("club_organizer_grant.add", admin.pk),
        ("club_organizer_grant.remove", admin.pk),
    ]


def test_granting_an_unknown_or_malformed_organizer_is_404() -> None:
    club = ClubFactory.create()
    admin = _admin_of(club)
    path = f"{BASE}/{club.public_code}/organizer-grants"

    for organizer in (_unknown_code(CodeType.ORGANIZER), "bad", ""):
        response = _send(admin, "post", path, {"organizer": organizer})
        assert (response.status_code, response.json()) == (404, NOT_FOUND)
    assert not ClubOrganizerGrant.objects.exists()


def test_the_organizer_side_lists_club_grants_for_an_admin_member() -> None:
    organizer = OrganizerFactory.create()
    member = _account()
    OrganizerMemberFactory.create(
        organizer=organizer, account=member, role=MemberRole.ADMIN
    )
    manager = _account()
    OrganizerMemberFactory.create(organizer=organizer, account=manager)
    club = ClubFactory.create(name="Made-up Courts", city="Made-up City", region="MU")
    grant = ClubOrganizerGrantFactory.create(club=club, organizer=organizer)
    path = f"/api/v1/organizer/organizers/{organizer.public_code}/club-grants"

    response = _send(member, "get", path)

    assert response.status_code == 200
    [row] = response.json()
    assert _when(row.pop("created_at")) == _millis(grant.created_at)
    assert row == {
        "club": club.public_code,
        "name": "Made-up Courts",
        "city": "Made-up City",
        "region": "MU",
    }
    for actor in (manager, _account()):
        refused = _send(actor, "get", path)
        assert (refused.status_code, refused.json()) == (404, NOT_FOUND)
    no_mfa = _account(mfa=False)
    OrganizerMemberFactory.create(
        organizer=organizer, account=no_mfa, role=MemberRole.ADMIN, mfa=False
    )
    refused = _send(no_mfa, "get", path)
    assert (refused.status_code, refused.json()) == MFA_REQUIRED


def test_the_accept_path_does_not_collide_with_a_club_code() -> None:
    account = _account()

    as_code = _send(account, "get", f"{BASE}/invites")
    wrong_method = _send(account, "get", f"{BASE}/invites/accept")
    accept = _send(account, "post", f"{BASE}/invites/accept", {"key": "made-up"})

    assert (as_code.status_code, as_code.json()) == (404, NOT_FOUND)
    assert wrong_method.status_code == 405
    assert (accept.status_code, accept.json()) == (404, NOT_FOUND)


def test_the_invite_answer_has_its_own_schema() -> None:
    schema = api.get_openapi_schema()
    operation = schema["paths"]["/api/v1/clubs/{code}/invites"]["post"]
    accepted = operation["responses"][202]["content"]["application/json"]["schema"]

    assert accepted == {"$ref": "#/components/schemas/ClubInviteSentOut"}
