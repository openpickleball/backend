"""The upload's response and the report read never load the stored file
(#71, review finding G). Made-up data only."""

from typing import Any
from unittest import mock

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.clubs.tests.factories import OrganizerMemberFactory, verified_organizer
from apps.events.models import Event
from apps.events.tests.factories import EventFactory
from apps.matches import tasks

pytestmark = pytest.mark.django_db

CONTENT_COLUMN = '"matches_resultsfile"."content"'


@pytest.fixture(autouse=True)
def no_queue() -> Any:
    with mock.patch.object(tasks.process_results_file, "delay") as patched:
        yield patched


def _selects_content(queries: CaptureQueriesContext) -> bool:
    return any(
        query["sql"].startswith("SELECT") and CONTENT_COLUMN in query["sql"]
        for query in queries.captured_queries
    )


def test_neither_the_upload_response_nor_the_report_loads_the_file() -> None:
    event: Event = EventFactory.create(organizer=verified_organizer())
    member: User = UserFactory.create()
    OrganizerMemberFactory.create(organizer=event.organizer, account=member)
    client = Client()
    client.force_login(member)

    with CaptureQueriesContext(connection) as upload_queries:
        response = client.post(
            f"/api/v1/organizer/events/{event.public_code}/results-files",
            {
                "file": SimpleUploadedFile("results.csv", b"made-up", "text/csv"),
                "players_13_or_older": "true",
            },
        )
    assert response.status_code == 202
    with CaptureQueriesContext(connection) as report_queries:
        report = client.get(
            f"/api/v1/organizer/results-files/{response.json()['code']}"
        )
    assert report.status_code == 200

    assert not _selects_content(upload_queries)
    assert not _selects_content(report_queries)
