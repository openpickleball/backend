"""The step-up asks an Account without MFA for its password, not a code
(#232 review).

An Account without MFA reaches the step-up only on the results upload.
It has no code to give, so its 403 ``reauthentication_required`` carries
the general reauthentication text. Made-up data only.
"""

import pytest
from allauth.account.authentication import AUTHENTICATION_METHODS_SESSION_KEY
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from apps.accounts.tests.factories import UserFactory
from apps.clubs.tests.factories import verified_organizer
from apps.events.tests.factories import EventFactory
from config.auth import REAUTHENTICATION_REQUIRED_MESSAGE

pytestmark = pytest.mark.django_db


def test_an_account_without_mfa_is_asked_to_reauthenticate() -> None:
    event = EventFactory.create(organizer=verified_organizer())
    client = Client()
    client.force_login(UserFactory.create())
    session = client.session
    session[AUTHENTICATION_METHODS_SESSION_KEY] = []
    session.save()

    response = client.post(
        f"/api/v1/organizer/events/{event.public_code}/results-files",
        {
            "file": SimpleUploadedFile("results.csv", b"made-up", "text/csv"),
            "players_13_or_older": "true",
        },
    )

    assert response.status_code == 403
    assert response.json() == {
        "code": "reauthentication_required",
        "message": REAUTHENTICATION_REQUIRED_MESSAGE,
        "errors": [],
    }
