"""Unusual submission flags and batch sizes in the database (#206).

An ``unusual_submission`` flag names its rule and no other match, a
``possible_duplicate`` flag names no rule, and a match carries at most one
flag per rule. A Submission Batch's ``expected_size`` is set at most once,
and nothing else about a batch ever changes. Each database rule is tested
with a direct save that skips ``full_clean()``.
"""

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from apps.accounts.tests.factories import UserFactory
from apps.matches.models import (
    SIGNAL_FLAG_REASONS,
    FlagReason,
    FlagRule,
    Match,
    MatchFlag,
    MatchImmutable,
    SubmissionBatch,
)
from apps.matches.tests.factories import MatchFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def match() -> Match:
    made: Match = MatchFactory.create(games=False, participants=False)
    return made


@pytest.fixture
def other() -> Match:
    made: Match = MatchFactory.create(games=False, participants=False)
    return made


@pytest.fixture
def batch() -> SubmissionBatch:
    made: SubmissionBatch = SubmissionBatch.objects.create(
        submitted_by_user=UserFactory.create()
    )
    return made


def test_unusual_submission_is_a_signal_reason() -> None:
    assert FlagReason.UNUSUAL_SUBMISSION.value in SIGNAL_FLAG_REASONS
    assert FlagReason.POSSIBLE_DUPLICATE.value not in SIGNAL_FLAG_REASONS


def test_rules() -> None:
    assert set(FlagRule.values) == {
        "first_matches",
        "large_batch",
        "daily_volume",
        "player_daily_matches",
    }


@pytest.mark.parametrize("rule", FlagRule.values)
def test_an_unusual_submission_flag_with_a_rule_saves(match: Match, rule: str) -> None:
    flag = MatchFlag.objects.create(
        match=match, reason=FlagReason.UNUSUAL_SUBMISSION, rule=rule
    )
    flag.full_clean()

    assert list(match.flags.all()) == [flag]


def test_one_match_may_carry_every_rule(match: Match) -> None:
    for rule in FlagRule.values:
        MatchFlag.objects.create(
            match=match, reason=FlagReason.UNUSUAL_SUBMISSION, rule=rule
        )

    assert match.flags.count() == len(FlagRule.values)


def test_an_unusual_submission_flag_needs_a_rule(match: Match) -> None:
    with pytest.raises(IntegrityError) as error, transaction.atomic():
        MatchFlag.objects.create(match=match, reason=FlagReason.UNUSUAL_SUBMISSION)

    assert "matches_matchflag_rule_matches_reason" in str(error.value)


def test_a_rule_outside_choices_is_refused(match: Match) -> None:
    with pytest.raises(IntegrityError) as error, transaction.atomic():
        MatchFlag.objects.create(
            match=match, reason=FlagReason.UNUSUAL_SUBMISSION, rule="made_up"
        )

    assert "matches_matchflag_rule" in str(error.value)


def test_a_possible_duplicate_flag_has_no_rule(match: Match, other: Match) -> None:
    with pytest.raises(IntegrityError) as error, transaction.atomic():
        MatchFlag.objects.create(
            match=match,
            reason=FlagReason.POSSIBLE_DUPLICATE,
            duplicate_of=other,
            rule=FlagRule.FIRST_MATCHES,
        )

    assert "matches_matchflag_rule_matches_reason" in str(error.value)


def test_an_unusual_submission_flag_names_no_other_match(
    match: Match, other: Match
) -> None:
    with pytest.raises(IntegrityError) as error, transaction.atomic():
        MatchFlag.objects.create(
            match=match,
            reason=FlagReason.UNUSUAL_SUBMISSION,
            rule=FlagRule.FIRST_MATCHES,
            duplicate_of=other,
        )

    assert "matches_matchflag_duplicate_of_matches_reason" in str(error.value)


def test_a_second_flag_with_the_same_rule_is_refused(match: Match) -> None:
    MatchFlag.objects.create(
        match=match, reason=FlagReason.UNUSUAL_SUBMISSION, rule=FlagRule.LARGE_BATCH
    )

    with pytest.raises(IntegrityError) as error, transaction.atomic():
        MatchFlag.objects.create(
            match=match,
            reason=FlagReason.UNUSUAL_SUBMISSION,
            rule=FlagRule.LARGE_BATCH,
        )

    assert "matches_matchflag_once_per_rule" in str(error.value)


def test_full_clean_reports_a_repeated_rule_without_a_value(match: Match) -> None:
    MatchFlag.objects.create(
        match=match, reason=FlagReason.UNUSUAL_SUBMISSION, rule=FlagRule.DAILY_VOLUME
    )
    flag = MatchFlag(
        match=match, reason=FlagReason.UNUSUAL_SUBMISSION, rule=FlagRule.DAILY_VOLUME
    )

    with pytest.raises(ValidationError) as error:
        flag.full_clean()

    assert "daily_volume" not in str(error.value)


def test_many_possible_duplicate_flags_on_one_match(match: Match, other: Match) -> None:
    third = MatchFactory.create(games=False, participants=False)
    MatchFlag.objects.create(
        match=match, reason=FlagReason.POSSIBLE_DUPLICATE, duplicate_of=other
    )
    MatchFlag.objects.create(
        match=match, reason=FlagReason.POSSIBLE_DUPLICATE, duplicate_of=third
    )

    assert match.flags.count() == 2


# Expected batch size


def test_a_new_batch_has_no_expected_size(batch: SubmissionBatch) -> None:
    assert batch.expected_size is None


def test_a_batch_may_be_created_with_an_expected_size() -> None:
    made = SubmissionBatch.objects.create(
        submitted_by_user=UserFactory.create(), expected_size=12
    )

    made.refresh_from_db()
    assert made.expected_size == 12


def test_the_expected_size_is_set_once(batch: SubmissionBatch) -> None:
    batch.set_expected_size(40)

    batch.refresh_from_db()
    assert batch.expected_size == 40


def test_a_second_set_is_refused(batch: SubmissionBatch) -> None:
    batch.set_expected_size(40)

    with pytest.raises(MatchImmutable):
        batch.set_expected_size(41)
    batch.refresh_from_db()
    assert batch.expected_size == 40


def test_a_stale_instance_cannot_set_it_again(batch: SubmissionBatch) -> None:
    stale = SubmissionBatch.objects.get(pk=batch.pk)
    batch.set_expected_size(40)

    with pytest.raises(MatchImmutable):
        stale.set_expected_size(41)
    batch.refresh_from_db()
    assert batch.expected_size == 40


def test_a_batch_created_with_a_size_cannot_be_given_another() -> None:
    made = SubmissionBatch.objects.create(
        submitted_by_user=UserFactory.create(), expected_size=12
    )

    with pytest.raises(MatchImmutable):
        made.set_expected_size(13)


@pytest.mark.parametrize("size", [0, -1])
def test_a_size_below_one_is_refused(batch: SubmissionBatch, size: int) -> None:
    with pytest.raises(ValueError, match="at least one match"):
        batch.set_expected_size(size)
    batch.refresh_from_db()
    assert batch.expected_size is None


def test_the_database_refuses_a_size_below_one() -> None:
    with pytest.raises(IntegrityError) as error, transaction.atomic():
        SubmissionBatch.objects.create(
            submitted_by_user=UserFactory.create(), expected_size=0
        )

    assert "matches_submissionbatch_expected_size_positive" in str(error.value)


def test_an_unsaved_batch_cannot_set_it() -> None:
    unsaved = SubmissionBatch(submitted_by_user=UserFactory.create())

    with pytest.raises(MatchImmutable):
        unsaved.set_expected_size(5)


def test_a_full_save_is_still_refused(batch: SubmissionBatch) -> None:
    batch.expected_size = 5

    with pytest.raises(MatchImmutable):
        batch.save()


def test_saving_another_field_is_still_refused(batch: SubmissionBatch) -> None:
    batch.expected_size = 5

    with pytest.raises(MatchImmutable):
        batch.save(update_fields=["expected_size", "event"])
    with pytest.raises(MatchImmutable):
        batch.save(update_fields=["event"])


def test_a_save_with_only_the_size_is_the_set(batch: SubmissionBatch) -> None:
    batch.expected_size = 5
    batch.save(update_fields=["expected_size"])

    batch.refresh_from_db()
    assert batch.expected_size == 5
    batch.expected_size = None
    with pytest.raises(MatchImmutable):
        batch.save(update_fields=["expected_size"])


def test_a_bulk_update_is_still_refused(batch: SubmissionBatch) -> None:
    with pytest.raises(MatchImmutable):
        SubmissionBatch.objects.filter(pk=batch.pk).update(expected_size=5)
    batch.refresh_from_db()
    assert batch.expected_size is None


def test_a_batch_still_cannot_be_deleted(batch: SubmissionBatch) -> None:
    with pytest.raises(MatchImmutable):
        batch.delete()
