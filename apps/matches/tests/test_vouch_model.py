"""The Match Vouch row (#270, ADR 0034). Made-up data only.

A vouch says one match of a possible duplicate flag is not a duplicate.
Each vouch is its own append-only row: the ORM refuses to change or
delete one, and the database allows exactly one voucher (an Account or a
Partner Application) and one row per flag, match, and voucher.
"""

import pytest
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.matches.models import (
    FlagReason,
    Match,
    MatchFlag,
    MatchImmutable,
    MatchVouch,
)
from apps.matches.tests.factories import MatchFactory
from apps.partners.tests.factories import PartnerApplicationFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def flag() -> MatchFlag:
    earlier: Match = MatchFactory.create()
    later: Match = MatchFactory.create()
    made: MatchFlag = MatchFlag.objects.create(
        match=later, duplicate_of=earlier, reason=FlagReason.POSSIBLE_DUPLICATE
    )
    return made


@pytest.fixture
def account() -> User:
    made: User = UserFactory.create()
    return made


def test_a_vouch_saves(flag: MatchFlag, account: User) -> None:
    row = MatchVouch.objects.create(flag=flag, match=flag.match, account=account)

    assert row.created_at is not None
    assert str(row) == f"{flag.match.public_code} vouch"


def test_an_application_may_vouch(flag: MatchFlag) -> None:
    application = PartnerApplicationFactory.create()

    row = MatchVouch.objects.create(
        flag=flag, match=flag.match, application=application
    )

    assert row.application_id == application.pk


def test_a_vouch_needs_a_voucher(flag: MatchFlag) -> None:
    with pytest.raises(IntegrityError) as error, transaction.atomic():
        MatchVouch.objects.create(flag=flag, match=flag.match)

    assert "matches_matchvouch_one_voucher" in str(error.value)


def test_a_vouch_has_one_voucher_only(flag: MatchFlag, account: User) -> None:
    application = PartnerApplicationFactory.create()

    with pytest.raises(IntegrityError) as error, transaction.atomic():
        MatchVouch.objects.create(
            flag=flag, match=flag.match, account=account, application=application
        )

    assert "matches_matchvouch_one_voucher" in str(error.value)


def test_an_account_vouches_once_per_flag_and_match(
    flag: MatchFlag, account: User
) -> None:
    MatchVouch.objects.create(flag=flag, match=flag.match, account=account)

    with pytest.raises(IntegrityError) as error, transaction.atomic():
        MatchVouch.objects.create(flag=flag, match=flag.match, account=account)

    assert "matches_matchvouch_account_once" in str(error.value)


def test_an_application_vouches_once_per_flag_and_match(flag: MatchFlag) -> None:
    application = PartnerApplicationFactory.create()
    MatchVouch.objects.create(flag=flag, match=flag.match, application=application)

    with pytest.raises(IntegrityError) as error, transaction.atomic():
        MatchVouch.objects.create(flag=flag, match=flag.match, application=application)

    assert "matches_matchvouch_application_once" in str(error.value)


def test_one_account_may_vouch_for_both_matches(flag: MatchFlag, account: User) -> None:
    MatchVouch.objects.create(flag=flag, match=flag.match, account=account)
    assert flag.duplicate_of is not None
    MatchVouch.objects.create(flag=flag, match=flag.duplicate_of, account=account)

    assert MatchVouch.objects.filter(flag=flag).count() == 2


def test_a_vouch_is_never_changed_or_deleted(flag: MatchFlag, account: User) -> None:
    row = MatchVouch.objects.create(flag=flag, match=flag.match, account=account)

    with pytest.raises(MatchImmutable):
        row.save()
    with pytest.raises(MatchImmutable):
        row.delete()
    with pytest.raises(MatchImmutable):
        MatchVouch.objects.all().update(account=None)
    with pytest.raises(MatchImmutable):
        MatchVouch.objects.all().delete()
    with pytest.raises(MatchImmutable):
        MatchVouch.objects.bulk_update([row], ["account"])
    with pytest.raises(MatchImmutable):
        MatchVouch.objects.bulk_create(
            [MatchVouch(flag=flag, match=flag.match, account=account)],
            update_conflicts=True,
            update_fields=["created_at"],
            unique_fields=["flag", "match", "account"],
        )
    with pytest.raises(MatchImmutable):
        MatchVouch._base_manager.all().delete()

    assert MatchVouch.objects.filter(pk=row.pk).exists()


def test_the_guard_message_names_vouches() -> None:
    assert "vouches" in MatchImmutable.message


def test_a_vouched_flag_cannot_be_deleted(flag: MatchFlag, account: User) -> None:
    MatchVouch.objects.create(flag=flag, match=flag.match, account=account)

    with pytest.raises(ProtectedError), transaction.atomic():
        flag.delete()
    assert MatchFlag.objects.filter(pk=flag.pk).exists()
