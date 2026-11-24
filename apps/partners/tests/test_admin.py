"""The admin shows a new secret once, never a stored one, and no approval edits."""

from typing import TYPE_CHECKING

import pytest
from django.contrib import admin
from django.contrib.auth.hashers import check_password
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.tests.factories import UserFactory
from apps.clubs.tests.factories import OrganizerFactory
from apps.partners.admin import OrganizerApplicationGrantAdmin, PartnerApplicationAdmin
from apps.partners.models import OrganizerApplicationGrant, PartnerApplication
from apps.partners.tests.factories import (
    OrganizerApplicationGrantFactory,
    PartnerApplicationFactory,
)

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

pytestmark = pytest.mark.django_db

APPLICATION_ADD = "admin:partners_partnerapplication_add"
APPLICATION_CHANGE = "admin:partners_partnerapplication_change"
APPLICATION_CHANGELIST = "admin:partners_partnerapplication_changelist"
GRANT_ADD = "admin:partners_organizerapplicationgrant_add"
GRANT_CHANGELIST = "admin:partners_organizerapplicationgrant_changelist"
RAW_SECRET = "made-up-client-secret-for-tests-only"


@pytest.fixture
def superuser() -> "_User":
    account: _User = UserFactory.create(is_staff=True, is_superuser=True)
    return account


@pytest.fixture
def superuser_client(client: Client, superuser: "_User") -> Client:
    client.force_login(superuser)
    return client


def _add_data(**extra: str) -> dict[str, str]:
    data = {
        "client_id": "made-up-admin-client-id",
        "user": "",
        "organizer": "",
        "redirect_uris": "",
        "post_logout_redirect_uris": "",
        "client_type": PartnerApplication.CLIENT_CONFIDENTIAL,
        "authorization_grant_type": PartnerApplication.GRANT_CLIENT_CREDENTIALS,
        "client_secret": RAW_SECRET,
        "name": "Made-up Admin App",
        "algorithm": "",
        "allowed_origins": "",
        "is_sandbox": "on",
    }
    data.update(extra)
    return data


def test_our_admins_are_registered() -> None:
    assert isinstance(admin.site._registry[PartnerApplication], PartnerApplicationAdmin)
    assert isinstance(
        admin.site._registry[OrganizerApplicationGrant], OrganizerApplicationGrantAdmin
    )


def test_add_form_shows_a_generated_secret(superuser_client: Client) -> None:
    response = superuser_client.get(reverse(APPLICATION_ADD))

    assert response.status_code == 200
    form = response.context["adminform"].form
    assert "client_secret" in form.fields
    assert form["client_secret"].value()


def test_add_form_has_no_hash_choice(superuser_client: Client) -> None:
    response = superuser_client.get(reverse(APPLICATION_ADD))

    assert "hash_client_secret" not in response.context["adminform"].form.fields
    assert b"hash_client_secret" not in response.content


def test_add_stores_the_secret_hashed(superuser_client: Client) -> None:
    response = superuser_client.post(reverse(APPLICATION_ADD), _add_data())

    assert response.status_code == 302
    application = PartnerApplication.objects.get(client_id="made-up-admin-client-id")
    assert application.hash_client_secret is True
    assert application.client_secret != RAW_SECRET
    assert check_password(RAW_SECRET, application.client_secret) is True
    assert application.is_approved is False
    assert application.is_sandbox is True


def test_add_with_organizer(superuser_client: Client) -> None:
    organizer = OrganizerFactory.create()

    response = superuser_client.post(
        reverse(APPLICATION_ADD), _add_data(organizer=str(organizer.pk))
    )

    assert response.status_code == 302
    application = PartnerApplication.objects.get(client_id="made-up-admin-client-id")
    assert application.organizer == organizer


def test_add_ignores_posted_approval_and_hash_choice(
    superuser_client: Client, superuser: "_User"
) -> None:
    response = superuser_client.post(
        reverse(APPLICATION_ADD),
        _add_data(
            approved_at=timezone.now().isoformat(),
            approved_by=str(superuser.pk),
            hash_client_secret="",
        ),
    )

    assert response.status_code == 302
    application = PartnerApplication.objects.get(client_id="made-up-admin-client-id")
    assert application.is_approved is False
    assert application.approved_by_id is None
    assert application.hash_client_secret is True


def test_change_form_never_shows_the_secret(superuser_client: Client) -> None:
    application = PartnerApplicationFactory.create(client_secret=RAW_SECRET)
    stored = PartnerApplication.objects.values_list("client_secret", flat=True).get(
        pk=application.pk
    )

    response = superuser_client.get(reverse(APPLICATION_CHANGE, args=[application.pk]))

    assert response.status_code == 200
    form = response.context["adminform"].form
    assert "client_secret" not in form.fields
    assert "hash_client_secret" not in form.fields
    assert stored.encode() not in response.content
    assert RAW_SECRET.encode() not in response.content
    assert b'name="client_secret"' not in response.content


def test_change_form_shows_approval_read_only(superuser_client: Client) -> None:
    application = PartnerApplicationFactory.create()

    response = superuser_client.get(reverse(APPLICATION_CHANGE, args=[application.pk]))

    form = response.context["adminform"].form
    assert "approved_at" not in form.fields
    assert "approved_by" not in form.fields
    readonly = response.context["adminform"].readonly_fields
    assert "approved_at" in readonly
    assert "approved_by" in readonly


def test_change_keeps_secret_and_ignores_approval(
    superuser_client: Client, superuser: "_User"
) -> None:
    application = PartnerApplicationFactory.create(client_secret=RAW_SECRET)
    stored = PartnerApplication.objects.values_list("client_secret", flat=True).get(
        pk=application.pk
    )
    data = _add_data(
        client_id=application.client_id,
        name="Made-up Renamed App",
        client_secret="made-up-replacement-secret",
        approved_at=timezone.now().isoformat(),
        approved_by=str(superuser.pk),
    )

    response = superuser_client.post(
        reverse(APPLICATION_CHANGE, args=[application.pk]), data
    )

    assert response.status_code == 302
    application.refresh_from_db()
    assert application.name == "Made-up Renamed App"
    assert application.client_secret == stored
    assert application.is_approved is False


def test_organizer_uses_a_raw_id_widget() -> None:
    model_admin = admin.site._registry[PartnerApplication]

    assert "organizer" in model_admin.raw_id_fields
    assert "user" in model_admin.raw_id_fields


def test_changelist_loads(superuser_client: Client) -> None:
    PartnerApplicationFactory.create()

    response = superuser_client.get(reverse(APPLICATION_CHANGELIST))

    assert response.status_code == 200


def test_grant_add_and_changelist(superuser_client: Client) -> None:
    organizer = OrganizerFactory.create()
    application = PartnerApplicationFactory.create()

    assert superuser_client.get(reverse(GRANT_ADD)).status_code == 200
    response = superuser_client.post(
        reverse(GRANT_ADD),
        {"organizer": str(organizer.pk), "application": str(application.pk)},
    )

    assert response.status_code == 302
    assert OrganizerApplicationGrant.objects.filter(
        organizer=organizer, application=application
    ).exists()
    OrganizerApplicationGrantFactory.create()
    assert superuser_client.get(reverse(GRANT_CHANGELIST)).status_code == 200
