"""Step-up for Organizer and Club admin changes and results uploads (#232).

The nine operations need a second factor step within
``ORGANIZER_STEP_UP_SECONDS`` in the session behind the request. A
session whose latest step is older gets 403 ``reauthentication_required``
with fixed text, before any lookup of the path's records, so an unknown
code gets the same answer. After ``auth/2fa/reauthenticate`` the same
request succeeds. Made-up data only.
"""

import csv
import io
import json
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from allauth.account.authentication import AUTHENTICATION_METHODS_SESSION_KEY
from allauth.account.models import EmailAddress
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, override_settings

from apps.accounts.models import User
from apps.accounts.tests.factories import (
    UserFactory,
    enroll_totp,
    totp_code,
    totp_secret,
)
from apps.accounts.tests.headless import APP, PASSWORD, HeadlessSession
from apps.clubs.models import Club, ClubRole, MemberRole, Organizer
from apps.clubs.tests.factories import (
    ClubFactory,
    ClubOrganizerGrantFactory,
    ClubRoleFactory,
    OrganizerFactory,
    OrganizerMemberFactory,
    verified_organizer,
)
from apps.events.tests.factories import EventFactory
from apps.matches.csv_format import COLUMNS
from apps.players.tests.factories import PlayerFactory
from apps.public_ids.api import PublicCodePath
from apps.public_ids.codes import CodeType
from config.auth import REAUTHENTICATION_REQUIRED_MESSAGE, STEP_UP_MESSAGE

pytestmark = pytest.mark.django_db

API = "/api/v1"
STEP_UP = {
    "code": "reauthentication_required",
    "message": STEP_UP_MESSAGE,
    "errors": [],
}
WINDOW = 300


@pytest.fixture(autouse=True)
def _tolerance(settings: Any) -> None:
    """One period either side, so a second code in a test is accepted."""
    settings.MFA_TOTP_TOLERANCE = 1


@pytest.fixture(autouse=True)
def _clear_cache() -> Iterator[None]:
    """allauth's rate limits and the API throttles use the cache."""
    cache.clear()
    yield
    cache.clear()


def _account() -> User:
    account: User = UserFactory.create()
    EmailAddress.objects.create(
        user=account, email=account.email, verified=True, primary=True
    )
    PlayerFactory.create(account=account)
    return account


def _age_steps(client: Client, seconds: int) -> None:
    """Move every authentication record of the client's session back."""
    session = client.session
    records = session[AUTHENTICATION_METHODS_SESSION_KEY]
    for record in records:
        record["at"] = record["at"] - seconds
    session[AUTHENTICATION_METHODS_SESSION_KEY] = records
    session.save()


def _signed_in(account: User) -> Client:
    client = Client()
    client.force_login(account)
    return client


@dataclass(frozen=True)
class Case:
    """One operation: its method, its path from the setup, its body, and
    the status it answers after a fresh step."""

    name: str
    method: str
    path: Callable[[dict[str, Any]], str]
    unknown: Callable[[dict[str, Any]], str]
    body: Callable[[dict[str, Any]], Any]
    status: int
    multipart: bool = False


def _organizer_setup() -> dict[str, Any]:
    organizer = OrganizerFactory.create()
    admin = _account()
    enroll_totp(admin)
    OrganizerMemberFactory.create(
        organizer=organizer, account=admin, role=MemberRole.ADMIN
    )
    other = _account()
    OrganizerMemberFactory.create(organizer=organizer, account=other)
    return {"actor": admin, "organizer": organizer, "other": other}


def _club_setup() -> dict[str, Any]:
    club: Club = ClubFactory.create()
    admin = _account()
    enroll_totp(admin)
    ClubRoleFactory.create(club=club, account=admin)
    other = _account()
    enroll_totp(other)
    ClubRoleFactory.create(club=club, account=other)
    granted = OrganizerFactory.create()
    ClubOrganizerGrantFactory.create(club=club, organizer=granted)
    return {
        "actor": admin,
        "club": club,
        "other": other,
        "granted": granted,
        "new_organizer": OrganizerFactory.create(),
    }


def _event_setup() -> dict[str, Any]:
    organizer = verified_organizer()
    member = _account()
    OrganizerMemberFactory.create(organizer=organizer, account=member)
    return {"actor": member, "event": EventFactory.create(organizer=organizer)}


def _unknown(code_type: CodeType) -> str:
    return PublicCodePath(code_type).example


def _player(account: User) -> str:
    return str(account.player.public_code)


def _csv() -> bytes:
    players = [PlayerFactory.create(), PlayerFactory.create()]
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(COLUMNS)
    row = ["S", "SIDEOUT", "Made-up Round", "2026-06-01"]
    row += [players[0].display_name, players[0].public_code, "", ""]
    row += [players[1].display_name, players[1].public_code, "", ""]
    row += ["11", "7"] + [""] * 8
    writer.writerow(row)
    return out.getvalue().encode()


ORG = "/organizer/organizers"
CASES = [
    Case(
        "invite_organizer_member",
        "post",
        lambda s: f"{ORG}/{s['organizer'].public_code}/invites",
        lambda s: f"{ORG}/{_unknown(CodeType.ORGANIZER)}/invites",
        lambda s: {"email": "made.up.invitee@example.com", "role": "match_manager"},
        202,
    ),
    Case(
        "change_organizer_member_role",
        "patch",
        lambda s: f"{ORG}/{s['organizer'].public_code}/members/{_player(s['other'])}",
        lambda s: f"{ORG}/{_unknown(CodeType.ORGANIZER)}/members/{_player(s['other'])}",
        lambda s: {"role": "admin"},
        200,
    ),
    Case(
        "remove_organizer_member",
        "delete",
        lambda s: f"{ORG}/{s['organizer'].public_code}/members/{_player(s['other'])}",
        lambda s: f"{ORG}/{_unknown(CodeType.ORGANIZER)}/members/{_player(s['other'])}",
        lambda s: None,
        204,
    ),
    Case(
        "request_organizer_verification",
        "post",
        lambda s: f"{ORG}/{s['organizer'].public_code}/verification-request",
        lambda s: f"{ORG}/{_unknown(CodeType.ORGANIZER)}/verification-request",
        lambda s: {"statement": "A made-up weekly ladder."},
        201,
    ),
    Case(
        "invite_club_admin",
        "post",
        lambda s: f"/clubs/{s['club'].public_code}/invites",
        lambda s: f"/clubs/{_unknown(CodeType.CLUB)}/invites",
        lambda s: {"email": "made.up.club.invitee@example.com"},
        202,
    ),
    Case(
        "remove_club_admin",
        "delete",
        lambda s: f"/clubs/{s['club'].public_code}/admins/{_player(s['other'])}",
        lambda s: f"/clubs/{_unknown(CodeType.CLUB)}/admins/{_player(s['other'])}",
        lambda s: None,
        204,
    ),
    Case(
        "grant_club_organizer",
        "post",
        lambda s: f"/clubs/{s['club'].public_code}/organizer-grants",
        lambda s: f"/clubs/{_unknown(CodeType.CLUB)}/organizer-grants",
        lambda s: {"organizer": s["new_organizer"].public_code},
        201,
    ),
    Case(
        "remove_club_organizer_grant",
        "delete",
        lambda s: (
            f"/clubs/{s['club'].public_code}/organizer-grants/"
            f"{s['granted'].public_code}"
        ),
        lambda s: (
            f"/clubs/{_unknown(CodeType.CLUB)}/organizer-grants/"
            f"{s['granted'].public_code}"
        ),
        lambda s: None,
        204,
    ),
    Case(
        "create_results_file",
        "post",
        lambda s: f"/organizer/events/{s['event'].public_code}/results-files",
        lambda s: f"/organizer/events/{_unknown(CodeType.EVENT)}/results-files",
        lambda s: {
            "file": SimpleUploadedFile("results.csv", _csv(), "text/csv"),
            "players_13_or_older": "true",
        },
        202,
        multipart=True,
    ),
]
SETUPS: dict[str, Callable[[], dict[str, Any]]] = {
    "invite_organizer_member": _organizer_setup,
    "change_organizer_member_role": _organizer_setup,
    "remove_organizer_member": _organizer_setup,
    "request_organizer_verification": _organizer_setup,
    "invite_club_admin": _club_setup,
    "remove_club_admin": _club_setup,
    "grant_club_organizer": _club_setup,
    "remove_club_organizer_grant": _club_setup,
    "create_results_file": _event_setup,
}


def _send(client: Client, case: Case, path: str, setup: dict[str, Any]) -> Any:
    body = case.body(setup)
    if case.multipart:
        return client.post(f"{API}{path}", body)
    kwargs: dict[str, Any] = {}
    if body is not None:
        kwargs = {"data": json.dumps(body), "content_type": "application/json"}
    return getattr(client, case.method)(f"{API}{path}", **kwargs)


def _reauthenticate(client: Client, account: User) -> None:
    response = client.post(
        "/_allauth/browser/v1/auth/2fa/reauthenticate",
        {"code": totp_code(totp_secret(account), 1)},
        content_type="application/json",
    )
    assert response.status_code == 200, response.content


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_an_old_step_is_refused_and_a_fresh_one_succeeds(case: Case) -> None:
    setup = SETUPS[case.name]()
    client = _signed_in(setup["actor"])
    _age_steps(client, WINDOW + 5)

    refused = _send(client, case, case.path(setup), setup)

    assert refused.status_code == 403
    assert refused.json() == STEP_UP

    _reauthenticate(client, setup["actor"])
    done = _send(client, case, case.path(setup), setup)

    assert done.status_code == case.status, done.content


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_an_unknown_code_gets_the_same_refusal_before_any_lookup(case: Case) -> None:
    setup = SETUPS[case.name]()
    client = _signed_in(setup["actor"])
    _age_steps(client, WINDOW + 5)

    refused = _send(client, case, case.unknown(setup), setup)

    assert refused.status_code == 403
    assert refused.json() == STEP_UP


def test_a_step_within_the_window_needs_no_reauthentication() -> None:
    setup = _organizer_setup()
    client = _signed_in(setup["actor"])
    _age_steps(client, WINDOW - 30)

    response = _send(client, CASES[3], CASES[3].path(setup), setup)

    assert response.status_code == 201


@override_settings(ORGANIZER_STEP_UP_SECONDS=60)
def test_the_window_comes_from_the_setting() -> None:
    setup = _organizer_setup()
    client = _signed_in(setup["actor"])
    _age_steps(client, 90)

    response = _send(client, CASES[3], CASES[3].path(setup), setup)

    assert response.status_code == 403
    assert response.json() == STEP_UP


def test_a_password_reauthentication_does_not_count_for_an_account_with_mfa() -> None:
    setup = _organizer_setup()
    client = _signed_in(setup["actor"])
    _age_steps(client, WINDOW + 5)
    session = client.session
    records = session[AUTHENTICATION_METHODS_SESSION_KEY]
    records.append({"method": "password", "at": time.time(), "reauthenticated": True})
    session[AUTHENTICATION_METHODS_SESSION_KEY] = records
    session.save()

    response = _send(client, CASES[3], CASES[3].path(setup), setup)

    assert response.status_code == 403
    assert response.json() == STEP_UP


def test_an_account_without_mfa_counts_a_recent_login_on_the_upload() -> None:
    """The upload is the one operation an Account without MFA reaches: a
    recent login counts, and it then gets the lookup's 404."""
    setup = _event_setup()
    account = _account()
    client = _signed_in(account)
    session = client.session
    session[AUTHENTICATION_METHODS_SESSION_KEY] = [
        {"method": "password", "at": time.time() - WINDOW - 5, "email": "x"}
    ]
    session["account_authentication_methods_user"] = str(account.pk)
    session.save()
    case = CASES[-1]

    refused = _send(client, case, case.path(setup), setup)
    assert refused.status_code == 403
    # No code to give: the general reauthentication text (#232 review).
    assert refused.json() == {
        "code": "reauthentication_required",
        "message": REAUTHENTICATION_REQUIRED_MESSAGE,
        "errors": [],
    }

    session = client.session
    session[AUTHENTICATION_METHODS_SESSION_KEY] = [
        {"method": "password", "at": time.time()}
    ]
    session.save()
    after = _send(client, case, case.path(setup), setup)
    assert after.status_code == 404


def test_the_app_client_refused_then_accepted_after_reauthenticating() -> None:
    setup = _organizer_setup()
    admin: User = setup["actor"]
    admin.set_password(PASSWORD)
    admin.save()
    session = HeadlessSession(APP)
    secret = totp_secret(admin)
    logged_in = session.mfa_login(admin.email, totp_code(secret))
    assert logged_in.status_code == 200
    store = session.store()
    records = store[AUTHENTICATION_METHODS_SESSION_KEY]
    for record in records:
        record["at"] -= WINDOW + 5
    store[AUTHENTICATION_METHODS_SESSION_KEY] = records
    store.save()
    path = f"{API}{ORG}/{setup['organizer'].public_code}/invites"
    body = json.dumps({"email": "made.up.app@example.com", "role": "admin"})
    headers = {"X-Session-Token": str(session.token)}

    refused = session.client.post(
        path, body, content_type="application/json", headers=headers
    )
    assert refused.status_code == 403
    assert refused.json() == STEP_UP

    again = session.post("auth/2fa/reauthenticate", {"code": totp_code(secret, 1)})
    assert again.status_code == 200
    done = session.client.post(
        path, body, content_type="application/json", headers=headers
    )
    assert done.status_code == 202


def test_reads_need_no_fresh_step() -> None:
    setup = _organizer_setup()
    client = _signed_in(setup["actor"])
    _age_steps(client, WINDOW + 5)

    members = client.get(f"{API}{ORG}/{setup['organizer'].public_code}/members")
    admins_setup = _club_setup()
    club_client = _signed_in(admins_setup["actor"])
    _age_steps(club_client, WINDOW + 5)
    admins = club_client.get(f"{API}/clubs/{admins_setup['club'].public_code}/admins")

    assert members.status_code == 200
    assert admins.status_code == 200
    assert ClubRole.objects.filter(club=admins_setup["club"]).count() == 2
    assert Organizer.objects.filter(pk=setup["organizer"].pk).exists()
