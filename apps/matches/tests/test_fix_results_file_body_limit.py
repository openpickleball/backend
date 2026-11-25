"""The upload route refuses an oversized body without reading it (#71,
review finding C).

A declared Content-Length over the cap (the file limit plus a fixed
allowance for the multipart framing) gets the 413 ``ErrorResponse``
before any byte of the body is read, signed in or not. A file that
streams past the file limit is stopped by an upload handler. Other
routes are not limited. Made-up data only.
"""

import io
from typing import Any
from unittest import mock

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.http import HttpRequest, HttpResponse
from django.test import Client, RequestFactory
from ninja.errors import HttpError

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.clubs.tests.factories import OrganizerMemberFactory, verified_organizer
from apps.events.models import Event
from apps.events.tests.factories import EventFactory
from apps.matches import tasks
from apps.matches.models import ResultsFile, SubmissionBatch
from apps.matches.results_files import FILE_TOO_LARGE, MAX_FILE_BYTES

pytestmark = pytest.mark.django_db

TOO_LARGE = {"code": "too_large", "message": FILE_TOO_LARGE, "errors": []}


class Unreadable(io.RawIOBase):
    """A request body that fails the test if anything reads it."""

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        raise AssertionError("The body was read.")


class Counting(io.BytesIO):
    """A request body that counts the bytes read from it."""

    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.read_bytes = 0

    def read(self, size: int | None = -1) -> bytes:
        chunk = super().read(size)
        self.read_bytes += len(chunk)
        return chunk

    def readline(self, size: int | None = -1) -> bytes:
        line = super().readline(size)
        self.read_bytes += len(line)
        return line


@pytest.fixture(autouse=True)
def no_queue() -> Any:
    with mock.patch.object(tasks.process_results_file, "delay") as patched:
        yield patched


@pytest.fixture
def event() -> Event:
    made: Event = EventFactory.create(organizer=verified_organizer())
    return made


@pytest.fixture
def client(event: Event) -> Client:
    member: User = UserFactory.create()
    OrganizerMemberFactory.create(organizer=event.organizer, account=member)
    signed_in = Client()
    signed_in.force_login(member)
    return signed_in


def _path(event: Event) -> str:
    return f"/api/v1/organizer/events/{event.public_code}/results-files"


def _form() -> dict[str, Any]:
    return {
        "file": SimpleUploadedFile("results.csv", b"made-up", "text/csv"),
        "players_13_or_older": "true",
    }


def _nothing_stored() -> bool:
    return not ResultsFile.objects.exists() and not SubmissionBatch.objects.exists()


@pytest.mark.parametrize("signed_in", [True, False], ids=["signed-in", "signed-out"])
def test_a_declared_length_over_the_cap_is_refused_unread(
    event: Event, client: Client, signed_in: bool
) -> None:
    from apps.matches.upload_limits import MAX_REQUEST_BYTES

    sender = client if signed_in else Client()
    extra: dict[str, Any] = {
        "CONTENT_LENGTH": str(MAX_REQUEST_BYTES + 1),
        "wsgi.input": Unreadable(),
    }
    response = sender.post(_path(event), _form(), **extra)

    assert response.status_code == 413
    assert response.json() == TOO_LARGE
    assert _nothing_stored()


def test_a_body_with_no_length_reads_no_more_than_the_cap(
    event: Event, client: Client
) -> None:
    body = Counting(b"x" * (2 * MAX_FILE_BYTES))

    extra: dict[str, Any] = {"CONTENT_LENGTH": "", "wsgi.input": body}
    response = client.post(_path(event), _form(), **extra)

    assert response.status_code in (413, 422)
    assert body.read_bytes <= MAX_FILE_BYTES
    assert _nothing_stored()


def test_the_upload_handler_stops_a_file_past_the_limit() -> None:
    from apps.matches.upload_limits import ResultsFileUploadHandler

    handler = ResultsFileUploadHandler()
    assert handler.receive_data_chunk(b"x" * 10, MAX_FILE_BYTES - 10) == b"x" * 10

    with pytest.raises(HttpError) as caught:
        handler.receive_data_chunk(b"x" * 11, MAX_FILE_BYTES - 10)

    assert caught.value.status_code == 413
    assert str(caught.value) == FILE_TOO_LARGE


def test_a_file_just_over_the_limit_still_gets_413(
    event: Event, client: Client
) -> None:
    form = _form()
    form["file"] = SimpleUploadedFile("results.csv", b"x" * (MAX_FILE_BYTES + 1))

    response = client.post(_path(event), form)

    assert response.status_code == 413
    assert response.json() == TOO_LARGE
    assert _nothing_stored()


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/api/v1/matches/"),
        ("post", "/api/v1/organizer/events"),
        ("get", "/api/v1/organizer/events/E-AAAA-AAAA/results-files"),
    ],
)
def test_other_routes_are_not_limited(method: str, path: str) -> None:
    from apps.matches.upload_limits import (
        MAX_REQUEST_BYTES,
        ResultsFileBodyLimitMiddleware,
    )

    passed = HttpResponse("passed")

    def get_response(request: HttpRequest) -> HttpResponse:
        return passed

    request = getattr(RequestFactory(), method)(
        path, CONTENT_LENGTH=str(MAX_REQUEST_BYTES + 1)
    )

    assert ResultsFileBodyLimitMiddleware(get_response)(request) is passed


def test_the_cap_allows_the_largest_file_and_its_framing() -> None:
    from apps.matches.upload_limits import MAX_REQUEST_BYTES

    assert MAX_FILE_BYTES < MAX_REQUEST_BYTES <= MAX_FILE_BYTES + 65_536
