"""Submission Batches: one upload's matches, linked for cleanup (#205).

A batch names exactly one submitter, optionally an Event and a Club, and
is inserted once and never changed or deleted, like the ledger rows.
Made-up data only.
"""

from typing import Any

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, models, transaction
from django.db.models import ProtectedError

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.clubs.tests.factories import ClubFactory
from apps.events.tests.factories import EventFactory
from apps.matches.models import Match, MatchImmutable, SubmissionBatch
from apps.partners.tests.factories import PartnerApplicationFactory

pytestmark = pytest.mark.django_db

MARKER = "zqxjmarker"


@pytest.fixture
def account() -> User:
    made: User = UserFactory.create()
    return made


@pytest.fixture
def batch(account: User) -> SubmissionBatch:
    made: SubmissionBatch = SubmissionBatch.objects.create(submitted_by_user=account)
    return made


# One submitter


def test_a_batch_with_an_account_is_saved(batch: SubmissionBatch) -> None:
    assert SubmissionBatch.objects.filter(pk=batch.pk).exists()
    assert batch.created_at is not None
    assert batch.event_id is None
    assert batch.club_id is None


def test_a_batch_with_an_application_is_saved() -> None:
    application = PartnerApplicationFactory.create()

    made = SubmissionBatch.objects.create(submitted_by_application=application)

    assert made.submitted_by_application_id == application.pk


def test_a_batch_with_an_event_and_a_club_is_saved(account: User) -> None:
    event = EventFactory.create()
    club = ClubFactory.create()

    made = SubmissionBatch.objects.create(
        submitted_by_user=account, event=event, club=club
    )

    assert (made.event_id, made.club_id) == (event.pk, club.pk)


def test_a_batch_with_no_submitter_is_refused_by_the_database() -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        SubmissionBatch.objects.create()


def test_a_batch_with_two_submitters_is_refused_by_the_database(
    account: User,
) -> None:
    application = PartnerApplicationFactory.create()

    with pytest.raises(IntegrityError), transaction.atomic():
        SubmissionBatch.objects.create(
            submitted_by_user=account, submitted_by_application=application
        )


def test_full_clean_reports_no_submitter() -> None:
    with pytest.raises(ValidationError):
        SubmissionBatch().full_clean()


@pytest.mark.parametrize(
    "field", ["event", "club", "submitted_by_user", "submitted_by_application"]
)
def test_non_integer_link_is_refused_without_the_value(field: str) -> None:
    instance = SubmissionBatch(**{f"{field}_id": MARKER})

    with pytest.raises(ValidationError) as error:
        instance.full_clean()

    assert field in error.value.message_dict
    messages = " ".join(m for ms in error.value.message_dict.values() for m in ms)
    assert MARKER not in messages


# Insert only


def test_saving_an_existing_batch_is_refused(batch: SubmissionBatch) -> None:
    batch.club = ClubFactory.create()

    with pytest.raises(MatchImmutable):
        batch.save()

    assert SubmissionBatch.objects.get(pk=batch.pk).club_id is None


def test_saving_with_update_fields_is_refused(batch: SubmissionBatch) -> None:
    with pytest.raises(MatchImmutable):
        batch.save(update_fields=["club"])


def test_deleting_a_batch_is_refused(batch: SubmissionBatch) -> None:
    with pytest.raises(MatchImmutable):
        batch.delete()

    assert SubmissionBatch.objects.filter(pk=batch.pk).exists()


@pytest.mark.parametrize(
    "change",
    [
        lambda qs, batch: qs.update(club=None),
        lambda qs, batch: qs.delete(),
        lambda qs, batch: qs.bulk_update([batch], ["club"]),
        lambda qs, batch: qs.bulk_create(
            [
                SubmissionBatch(
                    pk=batch.pk, submitted_by_user_id=batch.submitted_by_user_id
                )
            ],
            update_conflicts=True,
            update_fields=["club"],
            unique_fields=["id"],
        ),
    ],
    ids=["update", "delete", "bulk_update", "upsert"],
)
@pytest.mark.parametrize("manager", ["objects", "_base_manager"])
def test_queryset_changes_are_refused(
    batch: SubmissionBatch, change: Any, manager: str
) -> None:
    queryset = getattr(SubmissionBatch, manager).filter(pk=batch.pk)

    with pytest.raises(MatchImmutable):
        change(queryset, batch)

    assert SubmissionBatch.objects.filter(pk=batch.pk).exists()


# What it shows


def test_str_names_only_the_batch(batch: SubmissionBatch, account: User) -> None:
    assert str(batch) == f"Submission batch {batch.pk}"
    assert account.email not in str(batch)


# Links


def test_an_account_with_a_batch_cannot_be_deleted(
    batch: SubmissionBatch, account: User
) -> None:
    with pytest.raises(ProtectedError):
        account.delete()


def test_an_application_with_a_batch_cannot_be_deleted() -> None:
    application = PartnerApplicationFactory.create()
    SubmissionBatch.objects.create(submitted_by_application=application)

    with pytest.raises(ProtectedError):
        application.delete()


def test_a_club_with_a_batch_cannot_be_deleted(account: User) -> None:
    club = ClubFactory.create()
    SubmissionBatch.objects.create(submitted_by_user=account, club=club)

    with pytest.raises(ProtectedError):
        club.delete()


def test_an_event_with_a_batch_cannot_be_deleted(account: User) -> None:
    event = EventFactory.create()
    SubmissionBatch.objects.create(submitted_by_user=account, event=event)

    with pytest.raises(ProtectedError):
        event.delete()


@pytest.mark.parametrize(
    "field", ["submitted_by_user", "submitted_by_application", "event", "club"]
)
def test_batch_links_are_protected(field: str) -> None:
    link = SubmissionBatch._meta.get_field(field)

    assert isinstance(link, models.ForeignKey)
    assert link.remote_field.on_delete is models.PROTECT


def test_the_match_link_is_nullable_and_protected() -> None:
    link = Match._meta.get_field("submission_batch")

    assert isinstance(link, models.ForeignKey)
    assert link.null
    assert link.remote_field.on_delete is models.PROTECT
    assert link.remote_field.related_name == "matches"
