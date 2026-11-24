"""Factories for Partner Applications. Made-up data only."""

from typing import Any

from django.utils import timezone
from factory.declarations import LazyFunction, SubFactory
from factory.django import DjangoModelFactory
from faker import Faker

from apps.accounts.tests.factories import UserFactory
from apps.clubs.tests.factories import OrganizerFactory
from apps.partners.models import (
    APPROVAL_FIELDS,
    OrganizerApplicationGrant,
    PartnerApplication,
)

_faker = Faker()


class PartnerApplicationFactory(DjangoModelFactory[PartnerApplication]):
    """A saved, unapproved, confidential client credentials application.

    Its client secret is stored hashed, so a test that needs the raw secret
    passes ``client_secret`` itself.
    """

    class Meta:
        model = PartnerApplication

    # factory_boy's declarations carry no type hints.
    name = LazyFunction(lambda: f"{_faker.company()} App"[:255])  # type: ignore[no-untyped-call]
    client_type = PartnerApplication.CLIENT_CONFIDENTIAL
    authorization_grant_type = PartnerApplication.GRANT_CLIENT_CREDENTIALS


class OrganizerApplicationGrantFactory(DjangoModelFactory[OrganizerApplicationGrant]):
    """An Organizer's grant to a new Partner Application."""

    class Meta:
        model = OrganizerApplicationGrant

    organizer = SubFactory(OrganizerFactory)  # type: ignore[no-untyped-call]
    application = SubFactory(PartnerApplicationFactory)  # type: ignore[no-untyped-call]


def approve(application: PartnerApplication) -> PartnerApplication:
    """Approve a saved application, for tests only.

    It stands in for the approval service (#29), which does not exist yet.
    It writes ``approved_at`` and ``approved_by`` through ``update_fields``,
    the only write path the model allows. It records no Audit Event.
    """
    application.approved_at = timezone.now()
    application.approved_by = UserFactory.create()
    application.save(update_fields=list(APPROVAL_FIELDS))
    return application


def approved_application(**kwargs: Any) -> PartnerApplication:
    """A new application, approved with ``approve``."""
    application: PartnerApplication = PartnerApplicationFactory.create(**kwargs)
    return approve(application)
