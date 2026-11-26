"""The CSV results upload at /api/v1/organizer/ (#71).

A member of the Event's verified Organizer uploads a results file
(``create_results_file``) and reads its report (``get_results_file``).
Anyone else gets the same 404 as an unknown code. Made-up data only.
"""

import csv
import io
from collections.abc import Iterator
from typing import Any
from unittest import mock

import pytest
from allauth.account.models import EmailAddress
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory, totp_code, totp_secret
from apps.accounts.tests.headless import (
    APP,
    BROWSER,
    PASSWORD,
    HeadlessSession,
    csrf_header,
)
from apps.audit.models import AuditAction, AuditEvent
from apps.clubs.models import Organizer
from apps.clubs.tests.factories import (
    OrganizerFactory,
    OrganizerMemberFactory,
    verified_organizer,
)
from apps.events.models import Event
from apps.events.tests.factories import EventFactory
from apps.matches import results_files
from apps.matches.csv_format import COLUMNS
from apps.matches.models import Match, ResultsFile, SubmissionBatch
from apps.matches.results_files import MAX_FILE_BYTES
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory
from apps.public_ids.codes import CodeType, generate

pytestmark = pytest.mark.django_db

ORGANIZER = "/api/v1/organizer"
LOCATION = "Made-up Park"
NOT_FOUND = {"code": "not_found", "message": "Not found.", "errors": []}
CONFIRMATION_FIELD = "form.players_13_or_older"
UNKNOWN_EVENT = generate(CodeType.EVENT)
UNKNOWN_RESULTS_FILE = generate(CodeType.RESULTS_FILE)


@pytest.fixture(autouse=True)
def _clear_cache() -> Iterator[None]:
    """allauth's rate limits and the API throttles use the cache."""
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def event() -> Event:
    made: Event = EventFactory.create(organizer=verified_organizer())
    return made


def _member(organizer: Organizer, **kwargs: Any) -> User:
    account: User = UserFactory.create(**kwargs)
    OrganizerMemberFactory.create(organizer=organizer, account=account)
    return account


@pytest.fixture
def member(event: Event) -> User:
    return _member(event.organizer)


def _client(account: User) -> Client:
    client = Client()
    client.force_login(account)
    return client


def _content(players: list[Player]) -> bytes:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(COLUMNS)
    row = ["S", "SIDEOUT", "Made-up Round", "2026-06-01"]
    row += [players[0].display_name, players[0].public_code, "", ""]
    row += [players[1].display_name, players[1].public_code, "", ""]
    row += ["11", "7"] + [""] * 8
    writer.writerow(row)
    bad = list(row)
    bad[13] = "11"  # a tie
    writer.writerow(bad)
    unknown = list(row)
    unknown[9] = generate(CodeType.PLAYER)
    unknown[12:14] = ["11", "5"]
    writer.writerow(unknown)
    return out.getvalue().encode()


def _form(content: bytes = b"made-up", **fields: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "file": SimpleUploadedFile("results.csv", content, "text/csv"),
        "players_13_or_older": "true",
    }
    data.update(fields)
    return {key: value for key, value in data.items() if value is not ...}


def _upload(client: Client, code: str, data: dict[str, Any], **headers: str) -> Any:
    return client.post(
        f"{ORGANIZER}/events/{code}/results-files", data, headers=headers
    )


def _get(client: Client, code: str, **headers: str) -> Any:
    return client.get(f"{ORGANIZER}/results-files/{code}", headers=headers)


def _nothing_stored() -> bool:
    return (
        ResultsFile.objects.count() == 0
        and SubmissionBatch.objects.count() == 0
        and not AuditEvent.objects.filter(
            action=AuditAction.RESULTS_FILE_CREATE
        ).exists()
    )


# Upload


def test_an_upload_is_accepted_and_its_report_follows(
    event: Event,
    member: User,
    django_capture_on_commit_callbacks: Any,
) -> None:
    players = [PlayerFactory.create(), PlayerFactory.create()]
    client = _client(member)

    with django_capture_on_commit_callbacks(execute=True):
        response = _upload(
            client,
            event.public_code,
            _form(_content(players), game_target="11", location=LOCATION),
        )

    assert response.status_code == 202
    accepted = response.json()
    assert accepted["code"].startswith("F-")
    assert accepted["event"] == event.public_code
    assert accepted["status"] == "pending"
    assert accepted["error"] is None
    assert accepted["rows"] == []

    report = _get(client, accepted["code"])

    assert report.status_code == 200
    body = report.json()
    assert body["status"] == "done"
    assert body["error"] is None
    assert body["game_target"] == 11
    assert body["location"] == LOCATION
    assert body["processed_at"] is not None
    assert (body["recorded"], body["duplicates"], body["failed"]) == (1, 0, 2)
    recorded, tie, unknown = body["rows"]
    assert recorded["result"] == "recorded"
    assert Match.objects.get(public_code=recorded["match"]).event_id == event.pk
    assert tie["issues"] == [
        {
            "columns": ["teamAGame1", "teamBGame1"],
            "code": "tie",
            "message": tie["issues"][0]["message"],
        }
    ]
    assert [issue["code"] for issue in unknown["issues"]] == ["unknown_player"]
    assert unknown["issues"][0]["columns"] == ["playerB1Id"]


def test_the_same_file_again_reports_duplicates_with_the_codes(
    event: Event,
    member: User,
    django_capture_on_commit_callbacks: Any,
) -> None:
    players = [PlayerFactory.create(), PlayerFactory.create()]
    client = _client(member)
    content = _content(players)
    with django_capture_on_commit_callbacks(execute=True):
        first = _upload(client, event.public_code, _form(content)).json()
    with django_capture_on_commit_callbacks(execute=True):
        second = _upload(client, event.public_code, _form(content)).json()

    first_rows = _get(client, first["code"]).json()["rows"]
    second_body = _get(client, second["code"]).json()

    assert second_body["duplicates"] == 1
    assert second_body["rows"][0]["result"] == "duplicate"
    assert second_body["rows"][0]["match"] == first_rows[0]["match"]


@pytest.mark.parametrize("base", [APP, BROWSER])
def test_both_headless_clients_can_upload(
    event: Event, base: str, django_capture_on_commit_callbacks: Any
) -> None:
    account = _member(event.organizer, password=PASSWORD)
    EmailAddress.objects.create(
        user=account, email=account.email, verified=True, primary=True
    )
    session = HeadlessSession(base)
    # A member has TOTP (ADR 0043), so the login passes the MFA stage too.
    login = session.mfa_login(account.email, totp_code(totp_secret(account)))
    assert login.status_code == 200
    headers = (
        csrf_header(session.client)
        if base == BROWSER
        else {"X-Session-Token": session.token or ""}
    )

    with django_capture_on_commit_callbacks(execute=True):
        response = _upload(session.client, event.public_code, _form(), **headers)

    assert response.status_code == 202
    assert _get(session.client, response.json()["code"], **headers).status_code == 200


def test_a_signed_out_upload_is_refused(event: Event) -> None:
    response = _upload(Client(), event.public_code, _form())

    assert response.status_code == 401
    assert _nothing_stored()


# AC7: the age confirmation


@pytest.mark.parametrize("value", ["false", ...])
def test_without_the_age_confirmation_nothing_is_created(
    event: Event, member: User, value: Any
) -> None:
    with mock.patch.object(results_files, "create_results_file") as create:
        response = _upload(
            _client(member),
            event.public_code,
            _form(players_13_or_older=value),
        )

    assert response.status_code == 422
    fields = [item["field"] for item in response.json()["errors"]]
    assert fields == [CONFIRMATION_FIELD]
    create.assert_not_called()
    assert _nothing_stored()


def test_the_confirmation_is_checked_before_the_event(member: User) -> None:
    response = _upload(
        _client(member),
        generate(CodeType.EVENT),
        _form(players_13_or_older="false"),
    )

    assert response.status_code == 422


def test_the_confirmation_message_is_fixed_text(event: Event, member: User) -> None:
    response = _upload(
        _client(member), event.public_code, _form(players_13_or_older="false")
    )

    (item,) = response.json()["errors"]
    assert item["message"] == results_files.AGE_CONFIRMATION_REQUIRED


# AC6: size


def test_an_oversized_file_is_refused_with_413(event: Event, member: User) -> None:
    response = _upload(
        _client(member), event.public_code, _form(b"x" * (MAX_FILE_BYTES + 1))
    )

    assert response.status_code == 413
    assert response.json() == {
        "code": "too_large",
        "message": results_files.FILE_TOO_LARGE,
        "errors": [],
    }
    assert _nothing_stored()


def test_a_file_of_the_size_limit_is_accepted(event: Event, member: User) -> None:
    response = _upload(_client(member), event.public_code, _form(b"x" * MAX_FILE_BYTES))

    assert response.status_code == 202


def test_a_missing_file_is_a_422(event: Event, member: User) -> None:
    response = _upload(_client(member), event.public_code, _form(file=...))

    assert response.status_code == 422
    assert [item["field"] for item in response.json()["errors"]] == ["file.file"]
    assert _nothing_stored()


# AC18: game target and location


def test_a_game_target_of_15_is_stored(event: Event, member: User) -> None:
    response = _upload(_client(member), event.public_code, _form(game_target="15"))

    assert response.status_code == 202
    assert response.json()["game_target"] == 15


def test_another_game_target_is_a_422(event: Event, member: User) -> None:
    response = _upload(_client(member), event.public_code, _form(game_target="12"))

    assert response.status_code == 422
    assert [item["field"] for item in response.json()["errors"]] == ["form.game_target"]
    assert "12" not in response.json()["errors"][0]["message"]
    assert _nothing_stored()


def test_a_location_over_the_limit_is_a_422(event: Event, member: User) -> None:
    response = _upload(_client(member), event.public_code, _form(location="x" * 201))

    assert response.status_code == 422
    assert [item["field"] for item in response.json()["errors"]] == ["form.location"]
    assert _nothing_stored()


# AC9: who may upload


def test_a_non_member_gets_404(event: Event) -> None:
    response = _upload(_client(UserFactory.create()), event.public_code, _form())

    assert response.status_code == 404
    assert response.json() == NOT_FOUND
    assert _nothing_stored()


def test_a_member_of_an_unverified_organizer_gets_404() -> None:
    event: Event = EventFactory.create(organizer=OrganizerFactory.create())
    member = _member(event.organizer)

    response = _upload(_client(member), event.public_code, _form())

    assert response.status_code == 404
    assert _nothing_stored()


@pytest.mark.parametrize(
    "code", [UNKNOWN_EVENT, "E-NOPE"], ids=["unknown", "malformed"]
)
def test_an_unknown_or_malformed_event_gets_404(member: User, code: str) -> None:
    response = _upload(_client(member), code, _form())

    assert response.status_code == 404
    assert response.json() == NOT_FOUND
    assert _nothing_stored()


def test_a_refusal_under_the_lock_is_a_404(event: Event, member: User) -> None:
    with mock.patch.object(results_files, "can_manage_events", return_value=False):
        response = _upload(_client(member), event.public_code, _form())

    assert response.status_code == 404
    assert _nothing_stored()


# AC17: who may read the report


def test_another_account_gets_404_for_the_report(event: Event, member: User) -> None:
    code = _upload(_client(member), event.public_code, _form()).json()["code"]
    other = _member(event.organizer)

    response = _get(_client(other), code)

    assert response.status_code == 404
    assert response.json() == NOT_FOUND


@pytest.mark.parametrize(
    "code", [UNKNOWN_RESULTS_FILE, "F-NOPE"], ids=["unknown", "malformed"]
)
def test_an_unknown_or_malformed_report_code_gets_404(member: User, code: str) -> None:
    response = _get(_client(member), code)

    assert response.status_code == 404
    assert response.json() == NOT_FOUND


def test_a_pending_report_has_no_rows(event: Event, member: User) -> None:
    client = _client(member)
    code = _upload(client, event.public_code, _form()).json()["code"]

    body = _get(client, code).json()

    assert body["status"] == "pending"
    assert body["rows"] == []
    assert (body["recorded"], body["duplicates"], body["failed"]) == (0, 0, 0)


def test_a_failed_file_reports_its_error(
    event: Event, member: User, django_capture_on_commit_callbacks: Any
) -> None:
    client = _client(member)
    with django_capture_on_commit_callbacks(execute=True):
        code = _upload(client, event.public_code, _form(b"a,b\n")).json()["code"]

    body = _get(client, code).json()

    assert body["status"] == "failed"
    assert body["error"] == "bad_header"
