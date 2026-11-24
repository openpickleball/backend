"""``record_match`` checks every submitter it is given (review finding 3).

An unpermitted Account paired with an unsaved application must not skip
the check: the model sees one saved submitter and accepts it, so the call
would reach the duplicate lookup and confirm that a match exists. Made-up
data only.
"""

from datetime import date
from typing import Any

import pytest

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.clubs.tests.factories import OrganizerMemberFactory, verified_organizer
from apps.events.models import Event
from apps.events.tests.factories import EventFactory
from apps.matches import services
from apps.matches.models import Game, Match, MatchFlag, MatchFormat, MatchParticipant
from apps.matches.permissions import MatchPermissionDenied
from apps.matches.services import MatchValidationError, record_match
from apps.partners.models import PartnerApplication
from apps.partners.tests.factories import (
    OrganizerApplicationGrantFactory,
    approved_application,
)
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db

PLAYED_ON = date(2026, 6, 1)
GAMES = ((11, 7),)


def _counts() -> tuple[int, int, int, int]:
    return (
        Match.objects.count(),
        MatchParticipant.objects.count(),
        Game.objects.count(),
        MatchFlag.objects.count(),
    )


def _record(team_a: list[Player], team_b: list[Player], **overrides: Any) -> Match:
    values: dict[str, Any] = {
        "format": MatchFormat.SINGLES,
        "team_a": team_a,
        "team_b": team_b,
        "games": GAMES,
        "played_on": PLAYED_ON,
    }
    values.update(overrides)
    return record_match(**values).match


def test_unpermitted_account_with_unsaved_application_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    team_a, team_b = [PlayerFactory.create()], [PlayerFactory.create()]
    _record(team_a, team_b, submitted_by_user=team_a[0].account)
    before = _counts()

    def fail(*args: object, **kwargs: object) -> None:
        raise AssertionError("reached past the permission check")

    monkeypatch.setattr(services, "_advisory_lock", fail)
    monkeypatch.setattr(services, "_exact_match", fail)

    with pytest.raises(MatchPermissionDenied):
        _record(
            team_a,
            team_b,
            submitted_by_user=UserFactory.create(),
            submitted_by_application=PartnerApplication(),
        )

    assert _counts() == before


def test_two_permitted_submitters_fail_the_one_submitter_rule() -> None:
    event: Event = EventFactory.create(organizer=verified_organizer())
    account: User = UserFactory.create()
    OrganizerMemberFactory.create(organizer=event.organizer, account=account)
    application = approved_application()
    OrganizerApplicationGrantFactory.create(
        organizer=event.organizer, application=application
    )
    before = _counts()

    with pytest.raises(MatchValidationError) as error:
        _record(
            [PlayerFactory.create()],
            [PlayerFactory.create()],
            submitted_by_user=account,
            submitted_by_application=application,
            event=event,
        )

    fields = {issue.field for issue in error.value.issues}
    assert fields == {"submitted_by_user", "submitted_by_application"}
    assert _counts() == before
