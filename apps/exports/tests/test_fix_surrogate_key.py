"""A download key that is not UTF-8 text gets the fixed 404 (#39).

JSON can carry a lone surrogate (``"\\ud800"``). It reached ``hash_key``,
whose ``encode()`` raised ``UnicodeEncodeError``, a 500. Every key that
cannot be one the build made gets the same 404 as an unknown key.
Made-up data only.
"""

import json

import pytest
from django.core.cache import cache
from django.test import Client

from apps.accounts.tests.factories import UserFactory
from apps.exports.services import ExportNotFound, download_export

pytestmark = pytest.mark.django_db

DOWNLOAD = "/api/v1/me/export/download"
NOT_FOUND = {"code": "not_found", "message": "Not found.", "errors": []}


@pytest.mark.parametrize("key", ["\ud800", "abc\udfffdef"])
def test_the_service_refuses_a_lone_surrogate(key: str) -> None:
    with pytest.raises(ExportNotFound):
        download_export(UserFactory.create(), key)


def test_the_api_answers_a_lone_surrogate_with_the_same_404() -> None:
    cache.clear()
    client = Client()
    client.force_login(UserFactory.create())

    response = client.post(
        DOWNLOAD, json.dumps({"key": "\ud800"}), content_type="application/json"
    )

    assert response.status_code == 404
    assert response.json() == NOT_FOUND
