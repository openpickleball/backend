"""Unusual submissions from trusted submitters are flagged, never held (#206).

A match with an Event, recorded by its verified Organizer's member or an
approved application, counts at once. ``record_match()`` gives it one
``unusual_submission`` flag per rule that fires: among the first
``UNUSUAL_SUBMISSION_FIRST_MATCHES`` of its Organizer since its current
verification or of its application since approval (``first_matches``),
in a Submission Batch expecting more than ``UNUSUAL_SUBMISSION_BATCH_SIZE``
matches (``large_batch``), or past ``UNUSUAL_SUBMISSION_DAILY_MATCHES`` of
its submitter on one UTC day (``daily_volume``). A flag is a signal only:
it changes no count, rating, status, or hold, and sends nothing. Made-up
data only.
"""

from datetime import UTC, date, datetime, timedelta
from itertools import count
from typing import Any
from unittest import mock

import pytest
from django.contrib.auth.models import Permission
from django.utils import timezone
from pytest_django.fixtures import Settings

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.clubs.models import Organizer
from apps.clubs.services import unverify_organizer
from apps.clubs.tests.factories import (
    OrganizerMemberFactory,
    verified_organizer,
    verify,
)
from apps.emails.models import QueuedEmail
from apps.events.models import Event
from apps.events.tests.factories import EventFactory
from apps.matches import services
from apps.matches.models import (
    FlagReason,
    FlagRule,
    Match,
    MatchFlag,
    MatchFormat,
    MatchStatus,
    SubmissionBatch,
)
from apps.matches.permissions import MatchPermissionDenied
from apps.matches.services import RecordedMatch, record_match
from apps.matches.tests.conftest import SignalLog
from apps.matches.workflow import edit_match, void_match
from apps.partners.models import PartnerApplication
from apps.partners.tests.factories import approved_application
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory
from apps.ratings.models import RatingReplayRequest, RatingState

pytestmark = pytest.mark.django_db

GAMES = ((11, 7), (11, 4))
FIRST_DAY = date(2026, 1, 1)


@pytest.fixture(autouse=True)
def high_thresholds(settings: Settings) -> None:
    """The batch and daily rules off unless a test lowers their threshold.

    The first matches rule cannot be off: a new Organizer's first match is
    always among its first.
    """
    settings.UNUSUAL_SUBMISSION_FIRST_MATCHES = 1_000
    settings.UNUSUAL_SUBMISSION_BATCH_SIZE = 1_000
    settings.UNUSUAL_SUBMISSION_DAILY_MATCHES = 1_000
    settings.UNUSUAL_SUBMISSION_PLAYER_DAILY_MATCHES = 1_000


@pytest.fixture
def organizer() -> Organizer:
    return verified_organizer()


@pytest.fixture
def event(organizer: Organizer) -> Event:
    made: Event = EventFactory.create(organizer=organizer)
    return made


@pytest.fixture
def member(organizer: Organizer) -> User:
    account: User = UserFactory.create()
    OrganizerMemberFactory.create(organizer=organizer, account=account)
    return account


_days = count()


def _played_on() -> date:
    """A new past date for each match, so no two are near duplicates."""
    return FIRST_DAY + timedelta(days=next(_days) % 300)


def _record(event: Event | None = None, **overrides: Any) -> RecordedMatch:
    players = [PlayerFactory.create(), PlayerFactory.create()]
    values: dict[str, Any] = {
        "format": MatchFormat.SINGLES,
        "team_a": players[:1],
        "team_b": players[1:],
        "games": GAMES,
        "played_on": _played_on(),
        "event": event,
    }
    values.update(overrides)
    return record_match(**values)


def _rules(match: Match) -> list[str]:
    return sorted(
        match.flags.filter(reason=FlagReason.UNUSUAL_SUBMISSION).values_list(
            "rule", flat=True
        )
    )


def _other_rules(match: Match) -> list[str]:
    """The rules but ``first_matches``, which fires on every new
    Organizer's first matches whatever the thresholds."""
    return [rule for rule in _rules(match) if rule != FlagRule.FIRST_MATCHES]


def _counted(matches: list[Match]) -> bool:
    pks = {match.pk for match in matches}
    return (
        set(Match.objects.counted().filter(pk__in=pks).values_list("pk", flat=True))
        == pks
    )


def _unverify(organizer: Organizer) -> None:
    verifier = UserFactory.create()
    verifier.user_permissions.add(
        Permission.objects.get(
            content_type__app_label="clubs", codename="verify_organizer"
        )
    )
    unverify_organizer(organizer, actor=verifier, reason="Made-up test reason.")


@pytest.fixture
def pinned_now(monkeypatch: pytest.MonkeyPatch) -> list[datetime]:
    """``timezone.now()`` returns ``moment[0]``, which a test moves."""
    moment = [datetime(2026, 11, 20, 12, 0, tzinfo=UTC)]
    monkeypatch.setattr(timezone, "now", lambda: moment[0])
    return moment


# First matches


def test_an_organizers_first_matches_are_flagged(
    settings: Settings, event: Event, member: User, signals: SignalLog
) -> None:
    settings.UNUSUAL_SUBMISSION_FIRST_MATCHES = 3

    matches = [_record(event, submitted_by_user=member).match for _ in range(4)]

    assert [_rules(match) for match in matches] == [
        [FlagRule.FIRST_MATCHES],
        [FlagRule.FIRST_MATCHES],
        [FlagRule.FIRST_MATCHES],
        [],
    ]
    assert all(match.status == MatchStatus.CONFIRMED for match in matches)
    assert _counted(matches)


def test_the_first_matches_count_every_member_and_event_of_the_organizer(
    settings: Settings, organizer: Organizer, event: Event, member: User
) -> None:
    settings.UNUSUAL_SUBMISSION_FIRST_MATCHES = 2
    other_member: User = UserFactory.create()
    OrganizerMemberFactory.create(organizer=organizer, account=other_member)
    other_event: Event = EventFactory.create(organizer=organizer)

    first = _record(event, submitted_by_user=member).match
    second = _record(other_event, submitted_by_user=other_member).match
    third = _record(event, submitted_by_user=other_member).match

    assert _rules(first) == [FlagRule.FIRST_MATCHES]
    assert _rules(second) == [FlagRule.FIRST_MATCHES]
    assert _rules(third) == []


def test_another_organizers_matches_do_not_count(
    settings: Settings, event: Event, member: User
) -> None:
    settings.UNUSUAL_SUBMISSION_FIRST_MATCHES = 1
    other = verified_organizer()
    other_member: User = UserFactory.create()
    OrganizerMemberFactory.create(organizer=other, account=other_member)
    other_event: Event = EventFactory.create(organizer=other)
    _record(other_event, submitted_by_user=other_member)
    _record(other_event, submitted_by_user=other_member)

    match = _record(event, submitted_by_user=member).match

    assert _rules(match) == [FlagRule.FIRST_MATCHES]


def test_a_new_verification_restarts_the_count(
    settings: Settings, organizer: Organizer, event: Event, member: User
) -> None:
    settings.UNUSUAL_SUBMISSION_FIRST_MATCHES = 1
    _record(event, submitted_by_user=member)
    assert _rules(_record(event, submitted_by_user=member).match) == []

    _unverify(organizer)
    verify(organizer)

    assert _rules(_record(event, submitted_by_user=member).match) == [
        FlagRule.FIRST_MATCHES
    ]
    assert _rules(_record(event, submitted_by_user=member).match) == []


def test_voided_matches_count_toward_the_first_matches(
    settings: Settings, event: Event, member: User
) -> None:
    settings.UNUSUAL_SUBMISSION_FIRST_MATCHES = 1
    first = _record(event, submitted_by_user=member).match
    void_match(first, actor=member)

    assert _rules(_record(event, submitted_by_user=member).match) == []


def test_an_applications_first_matches_are_flagged_after_its_organizers(
    settings: Settings, organizer: Organizer, event: Event, member: User
) -> None:
    settings.UNUSUAL_SUBMISSION_FIRST_MATCHES = 2
    for _ in range(3):
        _record(event, submitted_by_user=member)
    application = approved_application(organizer=organizer)

    matches = [
        _record(event, submitted_by_application=application).match for _ in range(3)
    ]

    assert [_rules(match) for match in matches] == [
        [FlagRule.FIRST_MATCHES],
        [FlagRule.FIRST_MATCHES],
        [],
    ]
    assert _counted(matches)


def test_both_first_counts_give_one_flag(
    settings: Settings, organizer: Organizer, event: Event
) -> None:
    settings.UNUSUAL_SUBMISSION_FIRST_MATCHES = 5
    application: PartnerApplication = approved_application(organizer=organizer)

    match = _record(event, submitted_by_application=application).match

    assert _rules(match) == [FlagRule.FIRST_MATCHES]


# Daily volume


def test_matches_past_the_daily_number_are_flagged(
    settings: Settings, event: Event, member: User, pinned_now: list[datetime]
) -> None:
    settings.UNUSUAL_SUBMISSION_DAILY_MATCHES = 2

    matches = [_record(event, submitted_by_user=member).match for _ in range(4)]

    assert [_other_rules(match) for match in matches] == [
        [],
        [],
        [FlagRule.DAILY_VOLUME],
        [FlagRule.DAILY_VOLUME],
    ]
    assert all(match.status == MatchStatus.CONFIRMED for match in matches)
    assert _counted(matches)


def test_voided_matches_of_the_day_count(
    settings: Settings, event: Event, member: User, pinned_now: list[datetime]
) -> None:
    settings.UNUSUAL_SUBMISSION_DAILY_MATCHES = 1
    first = _record(event, submitted_by_user=member).match
    void_match(first, actor=member)

    assert _other_rules(_record(event, submitted_by_user=member).match) == [
        FlagRule.DAILY_VOLUME
    ]


def test_the_day_is_the_utc_day_of_created_at(
    settings: Settings, event: Event, member: User, pinned_now: list[datetime]
) -> None:
    settings.UNUSUAL_SUBMISSION_DAILY_MATCHES = 1
    pinned_now[0] = datetime(2026, 11, 20, 23, 59, 59, tzinfo=UTC)
    _record(event, submitted_by_user=member)

    pinned_now[0] = datetime(2026, 11, 21, 0, 0, 0, tzinfo=UTC)
    after_midnight = _record(event, submitted_by_user=member).match
    next_one = _record(event, submitted_by_user=member).match

    assert _other_rules(after_midnight) == []
    assert _other_rules(next_one) == [FlagRule.DAILY_VOLUME]


def test_the_daily_number_is_per_submitter(
    settings: Settings,
    organizer: Organizer,
    event: Event,
    member: User,
    pinned_now: list[datetime],
) -> None:
    settings.UNUSUAL_SUBMISSION_DAILY_MATCHES = 1
    other_member: User = UserFactory.create()
    OrganizerMemberFactory.create(organizer=organizer, account=other_member)
    application = approved_application(organizer=organizer)
    _record(event, submitted_by_user=member)

    assert _other_rules(_record(event, submitted_by_user=other_member).match) == []
    assert (
        _other_rules(_record(event, submitted_by_application=application).match) == []
    )
    assert _other_rules(_record(event, submitted_by_application=application).match) == [
        FlagRule.DAILY_VOLUME
    ]


# Large batches


def _batch(member: User, event: Event, size: int | None) -> SubmissionBatch:
    made: SubmissionBatch = SubmissionBatch.objects.create(
        submitted_by_user=member, event=event, expected_size=size
    )
    return made


def test_every_match_of_a_large_batch_is_flagged(
    settings: Settings, event: Event, member: User
) -> None:
    settings.UNUSUAL_SUBMISSION_BATCH_SIZE = 2
    batch = _batch(member, event, 3)

    matches = [
        _record(event, submitted_by_user=member, submission_batch=batch).match
        for _ in range(2)
    ]

    assert [_other_rules(match) for match in matches] == [
        [FlagRule.LARGE_BATCH],
        [FlagRule.LARGE_BATCH],
    ]
    assert _counted(matches)


@pytest.mark.parametrize("size", [2, None])
def test_a_batch_at_the_threshold_or_with_no_size_is_not_flagged(
    settings: Settings, event: Event, member: User, size: int | None
) -> None:
    settings.UNUSUAL_SUBMISSION_BATCH_SIZE = 2
    batch = _batch(member, event, size)

    match = _record(event, submitted_by_user=member, submission_batch=batch).match

    assert _other_rules(match) == []


def test_the_batch_size_is_read_from_the_stored_row(
    settings: Settings, event: Event, member: User
) -> None:
    settings.UNUSUAL_SUBMISSION_BATCH_SIZE = 2
    batch = _batch(member, event, None)
    batch.expected_size = 50  # In memory only.

    match = _record(event, submitted_by_user=member, submission_batch=batch).match

    assert _other_rules(match) == []


def test_a_match_can_carry_every_rule(
    settings: Settings, event: Event, member: User, pinned_now: list[datetime]
) -> None:
    settings.UNUSUAL_SUBMISSION_FIRST_MATCHES = 5
    settings.UNUSUAL_SUBMISSION_BATCH_SIZE = 1
    settings.UNUSUAL_SUBMISSION_DAILY_MATCHES = 1
    settings.UNUSUAL_SUBMISSION_PLAYER_DAILY_MATCHES = 1
    batch = _batch(member, event, 2)
    first = _record(event, submitted_by_user=member, submission_batch=batch).match
    player = first.participants.filter(team="A").get().player
    played_on = first.played_on

    match = _record(
        event,
        submitted_by_user=member,
        submission_batch=batch,
        team_a=[player],
        played_on=played_on,
    ).match

    assert _rules(match) == sorted(FlagRule.values)
    assert match.flags.count() == 4
    assert _counted([match])


# Signals only


def _lowest(settings: Settings) -> None:
    settings.UNUSUAL_SUBMISSION_FIRST_MATCHES = 1_000
    settings.UNUSUAL_SUBMISSION_BATCH_SIZE = 1
    settings.UNUSUAL_SUBMISSION_DAILY_MATCHES = 1


def test_a_flag_sends_no_signal_and_holds_nothing(
    settings: Settings,
    event: Event,
    member: User,
    signals: SignalLog,
) -> None:
    _lowest(settings)

    match = _record(event, submitted_by_user=member).match

    assert _rules(match) == [FlagRule.FIRST_MATCHES]
    assert signals.confirmed == [match.pk]
    assert signals.changed == []
    assert match.flags.filter(reason=FlagReason.POSSIBLE_DUPLICATE).count() == 0
    assert _counted([match])


def test_a_flagged_match_is_rated_live_with_no_replay(
    settings: Settings,
    event: Event,
    member: User,
    django_capture_on_commit_callbacks: Any,
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        match = _record(event, submitted_by_user=member).match

    assert _rules(match) == [FlagRule.FIRST_MATCHES]
    player_ids = list(match.participants.values_list("player_id", flat=True))
    assert RatingState.objects.filter(player_id__in=player_ids).count() == 2
    assert not RatingReplayRequest.objects.exists()


def test_a_flag_sends_no_email_of_its_own(
    settings: Settings,
    event: Event,
    member: User,
    django_capture_on_commit_callbacks: Any,
) -> None:
    settings.UNUSUAL_SUBMISSION_FIRST_MATCHES = 1
    with django_capture_on_commit_callbacks(execute=True):
        _record(event, submitted_by_user=member)
    flagged_mail = QueuedEmail.objects.count()
    with django_capture_on_commit_callbacks(execute=True):
        unflagged = _record(event, submitted_by_user=member).match

    assert _rules(unflagged) == []
    # The flagged match queued only what an unflagged one does: the
    # recorded match notices to its players (#208).
    assert QueuedEmail.objects.count() == 2 * flagged_mail


def test_the_match_api_shows_no_flag(
    settings: Settings, event: Event, member: User, client: Any
) -> None:
    match = _record(event, submitted_by_user=member).match
    assert _rules(match) == [FlagRule.FIRST_MATCHES]
    player = match.participants.select_related("player__account").first()
    assert player is not None
    client.force_login(player.player.account)

    response = client.get(f"/api/v1/matches/{match.public_code}")

    assert response.status_code == 200
    assert "flag" not in response.content.decode().lower()
    assert "unusual" not in response.content.decode().lower()


# Never flagged


def test_a_self_submission_is_never_flagged(
    settings: Settings, pinned_now: list[datetime]
) -> None:
    settings.UNUSUAL_SUBMISSION_FIRST_MATCHES = 1_000
    settings.UNUSUAL_SUBMISSION_DAILY_MATCHES = 1
    submitter = PlayerFactory.create()
    account = submitter.account

    matches = [
        _record(team_a=[submitter], submitted_by_user=account).match for _ in range(3)
    ]

    assert all(match.status == MatchStatus.PENDING for match in matches)
    assert not MatchFlag.objects.filter(match__in=matches).exists()


def test_a_correction_is_not_flagged(
    settings: Settings, event: Event, member: User
) -> None:
    settings.UNUSUAL_SUBMISSION_FIRST_MATCHES = 1_000
    original = _record(event, submitted_by_user=member).match
    players: list[Player] = [
        participant.player for participant in original.participants.order_by("team")
    ]

    correction = edit_match(
        original,
        actor=member,
        format=MatchFormat.SINGLES,
        team_a=players[:1],
        team_b=players[1:],
        games=((11, 9), (11, 5)),
        played_on=original.played_on,
    )

    assert _rules(original) == [FlagRule.FIRST_MATCHES]
    assert _rules(correction) == []


def test_a_confirming_duplicate_is_not_flagged_again(
    settings: Settings, event: Event, member: User, pinned_now: list[datetime]
) -> None:
    settings.UNUSUAL_SUBMISSION_DAILY_MATCHES = 1
    submitter = PlayerFactory.create()
    opponent = PlayerFactory.create()
    played_on = _played_on()
    pending = record_match(
        format=MatchFormat.SINGLES,
        team_a=[submitter],
        team_b=[opponent],
        games=GAMES,
        played_on=played_on,
        submitted_by_user=submitter.account,
    ).match
    _record(event, submitted_by_user=member)

    recorded = record_match(
        format=MatchFormat.SINGLES,
        team_a=[submitter],
        team_b=[opponent],
        games=GAMES,
        played_on=played_on,
        submitted_by_user=member,
        event=event,
    )

    assert recorded.created is False
    assert recorded.match.pk == pending.pk
    assert not MatchFlag.objects.filter(match=pending).exists()


def test_a_refused_submitter_reaches_no_rule(settings: Settings, event: Event) -> None:
    outsider: User = UserFactory.create()

    with (
        mock.patch.object(services, "flag_unusual_submission") as flag,
        pytest.raises(MatchPermissionDenied),
    ):
        _record(event, submitted_by_user=outsider)

    flag.assert_not_called()
    assert not MatchFlag.objects.exists()


def test_the_rules_run_last_under_the_submitter_lock(
    event: Event, member: User
) -> None:
    order: list[str] = []
    real_lock = services._advisory_lock
    real_flag = services.flag_unusual_submission

    def lock(key: int) -> None:
        order.append(f"lock:{key}")
        real_lock(key)

    def flag(match: Match) -> list[MatchFlag]:
        order.append("flag")
        return real_flag(match)

    with (
        mock.patch.object(services, "_advisory_lock", side_effect=lock),
        mock.patch.object(services, "flag_unusual_submission", side_effect=flag),
    ):
        _record(event, submitted_by_user=member)

    submitter_lock = f"lock:{services.submitter_lock_key(member)}"
    assert order[-2:] == [submitter_lock, "flag"]
    assert order.count(submitter_lock) == 1
