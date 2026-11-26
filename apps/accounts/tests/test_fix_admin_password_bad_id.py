"""A malformed Account id on the set-password page is a 404 (#100 review).

The page read the stored password hash by primary key before Django's
own lookup, so a non-numeric id raised ValueError and gave a 500 where
Django gives a 404. Made-up data only.
"""

import pytest
from django.test import Client

from apps.accounts.tests.factories import UserFactory

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("method", ["get", "post"])
def test_a_malformed_id_is_not_found(client: Client, method: str) -> None:
    client.force_login(UserFactory.create(is_staff=True, is_superuser=True))

    response = getattr(client, method)("/admin/accounts/user/abc/password/")

    assert response.status_code == 404
