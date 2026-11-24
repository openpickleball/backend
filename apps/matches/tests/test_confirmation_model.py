"""Match Confirmations, the corrects link, the moderate permission, and the
external id rule for a corrected match (#26). Made-up data only."""

from datetime import date

import pytest
from django.contrib.auth.models import Permission
from django.db import IntegrityError, transaction

from apps.clubs.tests.factories import verified_organizer
from apps.events.tests.factories import EventFactory
from apps.matches.models import (
    ConfirmationDecision,
    Match,
    MatchConfirmation,
    MatchFormat,
    MatchImmutable,
    MatchStatus,
)
from apps.matches.services import DuplicateKind, DuplicateMatchError, record_match
from apps.matches.tests.factories import MatchFactory, raw_update
from apps.partners.tests.factories import OrganizerApplicationGrantFactory, approve
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def match() -> Match:
    made: Match = MatchFactory.create()
    return made


def _player(match: Match) -> Player:
    return match.participants.first().player  # type: ignore[union-attr]


def test_a_confirmation_saves(match: Match) -> None:
    row = MatchConfirmation.objects.create(
        match=match, player=_player(match), decision=ConfirmationDecision.CONFIRM
    )

    assert row.created_at is not None
    assert str(row) == f"{match.public_code} confirm"


def test_a_player_confirms_a_match_once(match: Match) -> None:
    player = _player(match)
    MatchConfirmation.objects.create(
        match=match, player=player, decision=ConfirmationDecision.CONFIRM
    )

    with pytest.raises(IntegrityError) as error, transaction.atomic():
        MatchConfirmation.objects.create(
            match=match, player=player, decision=ConfirmationDecision.CONFIRM
        )

    assert "matches_matchconfirmation_once_per_decision" in str(error.value)


def test_a_player_may_confirm_and_dispute(match: Match) -> None:
    player = _player(match)
    for decision in ConfirmationDecision:
        MatchConfirmation.objects.create(match=match, player=player, decision=decision)

    assert match.confirmations.count() == 2


def test_a_bad_decision_is_refused(match: Match) -> None:
    with pytest.raises(IntegrityError) as error, transaction.atomic():
        MatchConfirmation.objects.create(
            match=match, player=_player(match), decision="maybe"
        )

    assert "matches_matchconfirmation_decision_valid" in str(error.value)


def test_a_confirmation_is_never_changed_or_deleted(match: Match) -> None:
    row = MatchConfirmation.objects.create(
        match=match, player=_player(match), decision=ConfirmationDecision.CONFIRM
    )

    with pytest.raises(MatchImmutable):
        row.save()
    with pytest.raises(MatchImmutable):
        row.delete()
    with pytest.raises(MatchImmutable):
        MatchConfirmation.objects.all().update(decision=ConfirmationDecision.DISPUTE)
    with pytest.raises(MatchImmutable):
        MatchConfirmation.objects.all().delete()


def test_a_match_corrects_at_most_one_match(match: Match) -> None:
    correction: Match = MatchFactory.create(corrects=match)

    assert match.corrected_by == correction
    with pytest.raises(IntegrityError), transaction.atomic():
        MatchFactory.create(corrects=match)


def test_the_moderate_permission_exists() -> None:
    assert Permission.objects.filter(
        content_type__app_label="matches", codename="moderate_match"
    ).exists()


def test_a_voided_match_frees_its_external_id(match: Match) -> None:
    earlier: Match = MatchFactory.create(by_application=True)
    raw_update(earlier, status=MatchStatus.VOIDED)

    later: Match = MatchFactory.create(
        submitted_by_user=None,
        submitted_by_application=earlier.submitted_by_application,
        external_id=earlier.external_id,
    )

    assert later.external_id == earlier.external_id


def test_two_live_matches_cannot_share_an_external_id() -> None:
    earlier: Match = MatchFactory.create(by_application=True)

    with pytest.raises(IntegrityError) as error, transaction.atomic():
        MatchFactory.create(
            submitted_by_user=None,
            submitted_by_application=earlier.submitted_by_application,
            external_id=earlier.external_id,
        )

    assert "matches_match_external_id_unique_per_application" in str(error.value)


def test_a_repeated_external_id_names_the_live_match_first() -> None:
    voided: Match = MatchFactory.create(by_application=True)
    raw_update(voided, status=MatchStatus.VOIDED)
    application = voided.submitted_by_application
    assert application is not None
    approve(application)
    live: Match = MatchFactory.create(
        submitted_by_user=None,
        submitted_by_application=application,
        external_id=voided.external_id,
    )
    event = EventFactory.create(organizer=verified_organizer())
    OrganizerApplicationGrantFactory.create(
        organizer=event.organizer, application=application
    )
    players = [PlayerFactory.create(), PlayerFactory.create()]

    with pytest.raises(DuplicateMatchError) as error:
        record_match(
            format=MatchFormat.SINGLES,
            team_a=players[:1],
            team_b=players[1:],
            games=[(11, 3)],
            played_on=date(2026, 6, 1),
            submitted_by_application=application,
            external_id=voided.external_id,
            event=event,
        )

    assert error.value.kind == DuplicateKind.DUPLICATE_EXTERNAL_ID
    assert error.value.existing_code == live.public_code
