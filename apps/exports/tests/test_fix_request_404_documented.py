"""The data export request documents every answer it gives (#39).

``request_data_export`` answers 404 when the Account is no longer
active by the time its row is locked (``ExportNotFound``, a race with
Account deletion), but its OpenAPI entry listed only 202 and 403.
Made-up data only.
"""

from unittest import mock

import pytest
from django.core.cache import cache
from django.test import Client

from apps.accounts.tests.factories import UserFactory
from apps.exports import api as export_api
from apps.exports.services import ExportNotFound
from config.api import api

pytestmark = pytest.mark.django_db

REQUEST = "/api/v1/me/export"


def test_the_document_lists_the_404() -> None:
    operation = api.get_openapi_schema()["paths"][REQUEST]["post"]

    assert operation["operationId"] == "request_data_export"
    response = operation["responses"]["404"]
    assert response["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ErrorResponse"
    }


def test_an_account_gone_inactive_gets_the_404() -> None:
    cache.clear()
    client = Client()
    client.force_login(UserFactory.create())

    with (
        mock.patch.object(export_api, "require_recent_reauthentication"),
        mock.patch.object(export_api, "recently_reauthenticated", return_value=True),
        mock.patch.object(export_api, "request_export", side_effect=ExportNotFound),
    ):
        response = client.post(REQUEST)

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"
