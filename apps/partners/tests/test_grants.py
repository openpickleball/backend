"""An Organizer Application Grant links one Organizer to one application."""

import pytest
from django.db import IntegrityError, transaction

from apps.clubs.models import Organizer
from apps.clubs.tests.factories import OrganizerFactory
from apps.partners.models import OrganizerApplicationGrant, PartnerApplication
from apps.partners.tests.factories import (
    OrganizerApplicationGrantFactory,
    PartnerApplicationFactory,
)

pytestmark = pytest.mark.django_db


def test_grant_links_organizer_and_application() -> None:
    organizer = OrganizerFactory.create()
    application = PartnerApplicationFactory.create()

    grant = OrganizerApplicationGrantFactory.create(
        organizer=organizer, application=application
    )

    assert grant.organizer == organizer
    assert grant.application == application
    assert grant.created_at is not None


def test_same_pair_twice_is_refused() -> None:
    grant = OrganizerApplicationGrantFactory.create()

    with pytest.raises(IntegrityError), transaction.atomic():
        OrganizerApplicationGrant.objects.create(
            organizer=grant.organizer, application=grant.application
        )


def test_one_organizer_can_grant_two_applications() -> None:
    organizer = OrganizerFactory.create()

    OrganizerApplicationGrantFactory.create(organizer=organizer)
    OrganizerApplicationGrantFactory.create(organizer=organizer)

    assert OrganizerApplicationGrant.objects.filter(organizer=organizer).count() == 2


def test_str_has_codes_not_names() -> None:
    organizer = OrganizerFactory.create(name="Made-up Organizer Name")
    application = PartnerApplicationFactory.create(name="Made-up App Name")
    grant = OrganizerApplicationGrantFactory.create(
        organizer=organizer, application=application
    )

    text = str(grant)

    assert organizer.public_code in text
    assert application.client_id in text
    assert "Made-up" not in text


def test_deleting_organizer_deletes_its_grants() -> None:
    grant = OrganizerApplicationGrantFactory.create()

    Organizer.objects.filter(pk=grant.organizer_id).delete()

    assert not OrganizerApplicationGrant.objects.filter(pk=grant.pk).exists()


def test_deleting_application_deletes_its_grants() -> None:
    grant = OrganizerApplicationGrantFactory.create()

    PartnerApplication.objects.filter(pk=grant.application_id).delete()

    assert not OrganizerApplicationGrant.objects.filter(pk=grant.pk).exists()
