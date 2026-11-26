"""Recorded match notices (#208, ADR 0041).

A match a trusted submitter (the Event's verified Organizer, or an
approved application acting for it) records counts at once, without the
players' confirmation. So each participating Player whose Account is
active gets an email listing the match, queued after the change commits.
A CSV upload sends each player one email for the whole upload, once its
processing ends. Made-up data only.
"""

import logging
from collections.abc import Callable, Iterator
from datetime import timedelta
from typing import Any
from unittest import mock

import pytest
from celery.exceptions import SoftTimeLimitExceeded
from django.core import mail
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.clubs.tests.factories import OrganizerMemberFactory
from apps.emails.services import queue_email
from apps.emails.tests.hygiene import assert_clean_message
from apps.events.models import Event
from apps.matches import notices, results_files, tasks
from apps.matches.emails import RECORDED_NOTICE, render_recorded_notice
from apps.matches.models import (
    Match,
    MatchFormat,
    MatchStatus,
    ResultsFile,
    ResultsFileStatus,
    Team,
)
from apps.matches.results_files import (
    fail_for_void,
    process_results_file,
    sweep_results_files,
)
from apps.matches.services import record_match
from apps.matches.tests.conftest import WORKFLOW_GAMES, WORKFLOW_PLAYED_ON
from apps.matches.tests.factories import MatchFactory, raw_update
from apps.matches.tests.results_file_builders import moderator as results_file_moderator
from apps.matches.tests.results_file_builders import row, upload
from apps.matches.workflow import (
    dispute_match,
    edit_match,
    void_submission_batch,
    void_submitter_matches,
)
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db

Capture = Callable[..., Any]
REASON = "Made-up reason for a test."
ONE = "A match you played in was recorded on OPEN"
SEVERAL = "Matches you played in were recorded on OPEN"


@pytest.fixture(autouse=True)
def no_queue() -> Iterator[Any]:
    # The CSV tests call processing directly.
    with mock.patch.object(tasks.process_results_file, "delay") as patched:
        yield patched


@pytest.fixture
def member(event: Event) -> User:
    account: User = UserFactory.create()
    OrganizerMemberFactory.create(organizer=event.organizer, account=account)
    return account


def _event_match(
    players: list[Player],
    event: Event,
    submitter: User,
    games: tuple[tuple[int, int], ...] = WORKFLOW_GAMES,
) -> Match:
    return record_match(
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=games,
        played_on=WORKFLOW_PLAYED_ON,
        submitted_by_user=submitter,
        event=event,
    ).match


def _sent() -> dict[str, list[str]]:
    sent: dict[str, list[str]] = {}
    for message in mail.outbox:
        [to] = message.to
        sent.setdefault(to, []).append(str(message.subject))
    return sent


def _html(message: Any) -> str:
    [(html, mimetype)] = message.alternatives
    assert mimetype == "text/html"
    return str(html)


def _deactivate(account: User) -> None:
    account.is_active = False
    account.save(update_fields=["is_active"])


# A single submission


def test_an_event_match_emails_each_player_once(
    players: list[Player],
    event: Event,
    member: User,
    django_capture_on_commit_callbacks: Capture,
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        match = _event_match(players, event, member)

    assert _sent() == {player.account.email: [ONE] for player in players}
    code = str(match.public_code)
    link = f"/matches/{code}"
    by_address = {message.to[0]: message for message in mail.outbox}
    team_a = by_address[players[0].account.email]
    team_b = by_address[players[2].account.email]
    for body in (team_a.body, _html(team_a)):
        assert code in body
        assert link in body
        assert "11-7, 9-11, 11-4" in body
        assert "Scores show your team first." in body
        assert "dispute it from its page" in body
        assert "counts toward ratings without your confirmation" in body
    assert "7-11, 11-9, 4-11" in team_b.body
    assert "June 1, 2026" in team_b.body


def test_nothing_is_sent_before_commit_or_after_a_rollback(
    players: list[Player],
    event: Event,
    member: User,
    django_capture_on_commit_callbacks: Capture,
) -> None:
    class Rollback(Exception):
        pass

    def record_then_fail() -> None:
        with transaction.atomic():
            _event_match(players, event, member)
            raise Rollback

    with django_capture_on_commit_callbacks(execute=True), pytest.raises(Rollback):
        record_then_fail()

    assert mail.outbox == []
    assert not Match.objects.exists()


def test_an_inactive_account_gets_no_email(
    players: list[Player],
    event: Event,
    member: User,
    django_capture_on_commit_callbacks: Capture,
) -> None:
    _deactivate(players[3].account)

    with django_capture_on_commit_callbacks(execute=True):
        _event_match(players, event, member)

    assert set(_sent()) == {player.account.email for player in players[:3]}


def test_the_email_names_no_player(
    event: Event, member: User, django_capture_on_commit_callbacks: Capture
) -> None:
    players = [
        PlayerFactory.create(
            display_name=f"Quillon Made-up {letter}.",
            legal_name=f"Quillon Made-up {letter}one",
        )
        for letter in "WXYZ"
    ]

    with django_capture_on_commit_callbacks(execute=True):
        _event_match(players, event, member)

    assert len(mail.outbox) == 4
    everyone = [*players, *Player.objects.all()]
    for message in mail.outbox:
        for part in (str(message.subject), str(message.body), _html(message)):
            for player in everyone:
                assert player.display_name not in part
                assert player.legal_name not in part
                assert player.account.email not in part
            assert member.email not in part
            assert event.name not in part
            assert event.organizer.name not in part


def test_a_self_submission_sends_no_recorded_notice(
    players: list[Player], django_capture_on_commit_callbacks: Capture
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        record_match(
            format=MatchFormat.DOUBLES,
            team_a=players[:2],
            team_b=players[2:],
            games=WORKFLOW_GAMES,
            played_on=WORKFLOW_PLAYED_ON,
            submitted_by_user=players[0].account,
        )

    assert mail.outbox == []


def test_an_organizer_confirming_a_pending_duplicate_emails_the_players(
    players: list[Player],
    pending: Match,
    event: Event,
    member: User,
    django_capture_on_commit_callbacks: Capture,
) -> None:
    # A self-submission's exact duplicate from the Organizer confirms it
    # (ADR 0013), so it counts although the other team never confirmed.
    with django_capture_on_commit_callbacks(execute=True):
        recorded = record_match(
            format=MatchFormat.DOUBLES,
            team_a=players[:2],
            team_b=players[2:],
            games=WORKFLOW_GAMES,
            played_on=WORKFLOW_PLAYED_ON,
            submitted_by_user=member,
            event=event,
        )

    assert recorded.created is False
    assert recorded.match.pk == pending.pk
    assert _sent() == {player.account.email: [ONE] for player in players}

    # A retry of the same submission confirms nothing and sends nothing.
    mail.outbox.clear()
    with django_capture_on_commit_callbacks(execute=True):
        record_match(
            format=MatchFormat.DOUBLES,
            team_a=players[:2],
            team_b=players[2:],
            games=WORKFLOW_GAMES,
            played_on=WORKFLOW_PLAYED_ON,
            submitted_by_user=member,
            event=event,
        )
    assert mail.outbox == []


def test_an_organizer_correction_emails_the_players(
    players: list[Player],
    event: Event,
    member: User,
    django_capture_on_commit_callbacks: Capture,
) -> None:
    match = _event_match(players, event, member)

    with django_capture_on_commit_callbacks(execute=True):
        corrected = edit_match(
            match,
            actor=member,
            format=MatchFormat.DOUBLES,
            team_a=players[:2],
            team_b=players[2:],
            games=((11, 5), (11, 6)),
            played_on=WORKFLOW_PLAYED_ON,
        )

    assert corrected.status == MatchStatus.CONFIRMED
    assert _sent() == {player.account.email: [ONE] for player in players}
    for message in mail.outbox:
        assert str(corrected.public_code) in message.body
        assert str(match.public_code) not in message.body


def test_a_player_correction_sends_no_recorded_notice(
    players: list[Player],
    pending: Match,
    django_capture_on_commit_callbacks: Capture,
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        edit_match(
            pending,
            actor=players[0].account,
            format=MatchFormat.DOUBLES,
            team_a=players[:2],
            team_b=players[2:],
            games=((11, 5), (11, 6)),
            played_on=WORKFLOW_PLAYED_ON,
            as_player=True,
        )

    assert mail.outbox == []


def test_a_match_voided_before_the_callback_sends_nothing(
    players: list[Player], event: Event, member: User
) -> None:
    match = _event_match(players, event, member)
    raw_update(match, status=MatchStatus.VOIDED)

    assert notices.send_recorded_notice(match.pk) == 0


def test_disputing_from_the_notice_stops_the_match_counting(
    players: list[Player],
    event: Event,
    member: User,
    django_capture_on_commit_callbacks: Capture,
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        match = _event_match(players, event, member)
    assert Match.objects.counted().filter(pk=match.pk).exists()
    assert f"/matches/{match.public_code}" in mail.outbox[0].body

    dispute_match(match, actor=players[2].account)

    assert not Match.objects.counted().filter(pk=match.pk).exists()


# A CSV upload


def test_a_player_in_three_matches_of_an_upload_gets_one_email(
    event: Event, member: User, django_capture_on_commit_callbacks: Capture
) -> None:
    me, *others = [PlayerFactory.create() for _ in range(4)]
    results_file = upload(member, event, [row(me, other, 5) for other in others])

    with django_capture_on_commit_callbacks(execute=True):
        process_results_file(results_file.pk)

    matches = list(Match.objects.order_by("sequence"))
    assert len(matches) == 3
    assert _sent() == {
        me.account.email: [SEVERAL],
        **{other.account.email: [ONE] for other in others},
    }
    [mine] = [m for m in mail.outbox if m.to == [me.account.email]]
    for body in (mine.body, _html(mine)):
        positions = [body.index(str(match.public_code)) for match in matches]
        assert positions == sorted(positions)
    assert mine.body.count("11-5") == 3


def test_an_upload_sends_nothing_per_row(
    event: Event, member: User, django_capture_on_commit_callbacks: Capture
) -> None:
    a, b = PlayerFactory.create(), PlayerFactory.create()
    results_file = upload(member, event, [row(a, b, 5)])

    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        process_results_file(results_file.pk)

    # One callback for the whole file, not one per row.
    names = [getattr(c, "func", c).__name__ for c in callbacks]
    assert names.count("_enqueue_batch_notices") == 1
    assert "send_recorded_notice" not in names


def test_a_bulk_void_of_the_upload_sends_nothing(
    event: Event, member: User, django_capture_on_commit_callbacks: Capture
) -> None:
    a, b = PlayerFactory.create(), PlayerFactory.create()
    results_file = upload(member, event, [row(a, b, 5)])

    with django_capture_on_commit_callbacks(execute=True):
        fail_for_void(results_file.submission_batch_id)

    results_file.refresh_from_db()
    assert results_file.status == ResultsFileStatus.FAILED
    assert mail.outbox == []


def test_an_expired_upload_tells_the_players_of_its_recorded_rows(
    event: Event, member: User, django_capture_on_commit_callbacks: Capture
) -> None:
    a, b = PlayerFactory.create(), PlayerFactory.create()
    results_file = upload(member, event, [row(a, b, 5), row(a, b, 7)])
    # The first row is recorded, then processing stops before the second.
    with (
        mock.patch.object(
            results_files, "_finish", side_effect=RuntimeError("stopped")
        ),
        pytest.raises(RuntimeError),
    ):
        process_results_file(results_file.pk)
    long_ago = timezone.now() - timedelta(days=4)
    ResultsFile.objects.filter(pk=results_file.pk).update(
        created_at=long_ago, queued_at=long_ago
    )

    with django_capture_on_commit_callbacks(execute=True):
        sweep_results_files()

    results_file.refresh_from_db()
    assert results_file.status == ResultsFileStatus.FAILED
    assert results_file.error_code == "expired"
    assert _sent() == {a.account.email: [SEVERAL], b.account.email: [SEVERAL]}


def test_an_upload_skips_voided_matches_and_inactive_accounts(
    event: Event, member: User, django_capture_on_commit_callbacks: Capture
) -> None:
    a, b, c = (PlayerFactory.create() for _ in range(3))
    results_file = upload(member, event, [row(a, b, 5), row(a, c, 5)])
    process_results_file(results_file.pk)
    first, second = Match.objects.order_by("sequence")
    raw_update(first, status=MatchStatus.VOIDED)
    _deactivate(c.account)

    with django_capture_on_commit_callbacks(execute=True):
        sent = notices.send_batch_notices(results_file.submission_batch_id)

    # b played only in the voided match, and c's Account is inactive.
    assert sent == 1
    assert _sent() == {a.account.email: [ONE]}
    [message] = mail.outbox
    assert str(second.public_code) in message.body
    assert str(first.public_code) not in message.body


def test_a_bulk_void_tells_the_players_of_the_matches_it_left_counting(
    event: Event, member: User, django_capture_on_commit_callbacks: Capture
) -> None:
    moderator = results_file_moderator()
    own = PlayerFactory.create(account=moderator)
    a, b = PlayerFactory.create(), PlayerFactory.create()
    results_file = upload(member, event, [row(a, b, 5), row(own, b, 5)])
    with (
        mock.patch.object(
            results_files, "_finish", side_effect=RuntimeError("stopped")
        ),
        pytest.raises(RuntimeError),
    ):
        process_results_file(results_file.pk)
    first, second = Match.objects.order_by("sequence")

    with django_capture_on_commit_callbacks(execute=True):
        result = void_submission_batch(
            results_file.submission_batch, actor=moderator, reason=REASON
        )

    # The moderator's own match is refused and keeps counting.
    assert (result.voided, result.refused) == (1, 1)
    assert _sent() == {own.account.email: [ONE], b.account.email: [ONE]}
    for message in mail.outbox:
        assert str(second.public_code) in message.body
        assert str(first.public_code) not in message.body


def test_a_submitter_void_tells_the_players_of_rows_before_its_date(
    event: Event, member: User, django_capture_on_commit_callbacks: Capture
) -> None:
    a, b = PlayerFactory.create(), PlayerFactory.create()
    results_file = upload(member, event, [row(a, b, 5), row(a, b, 7)])
    with (
        mock.patch.object(
            results_files, "_finish", side_effect=RuntimeError("stopped")
        ),
        pytest.raises(RuntimeError),
    ):
        process_results_file(results_file.pk)

    with django_capture_on_commit_callbacks(execute=True):
        result = void_submitter_matches(
            member,
            recorded_since=timezone.now().date() + timedelta(days=1),
            actor=results_file_moderator(),
            reason=REASON,
        )

    results_file.refresh_from_db()
    assert results_file.error_code == "voided"
    assert result.voided == 0
    assert _sent() == {a.account.email: [SEVERAL], b.account.email: [SEVERAL]}


def test_a_bulk_void_of_every_match_sends_nothing(
    event: Event, member: User, django_capture_on_commit_callbacks: Capture
) -> None:
    a, b = PlayerFactory.create(), PlayerFactory.create()
    results_file = upload(member, event, [row(a, b, 5)])
    with (
        mock.patch.object(
            results_files, "_finish", side_effect=RuntimeError("stopped")
        ),
        pytest.raises(RuntimeError),
    ):
        process_results_file(results_file.pk)

    with django_capture_on_commit_callbacks(execute=True):
        void_submission_batch(
            results_file.submission_batch,
            actor=results_file_moderator(),
            reason=REASON,
        )

    assert mail.outbox == []


def test_the_batch_notices_run_in_their_own_task(
    event: Event, member: User, django_capture_on_commit_callbacks: Capture
) -> None:
    a, b = PlayerFactory.create(), PlayerFactory.create()
    results_file = upload(member, event, [row(a, b, 5)])

    with (
        mock.patch.object(tasks.send_batch_notices, "delay") as delay,
        django_capture_on_commit_callbacks(execute=True),
    ):
        process_results_file(results_file.pk)

    delay.assert_called_once_with(results_file.submission_batch_id)
    assert mail.outbox == []
    assert (
        tasks.send_batch_notices.apply(args=(results_file.submission_batch_id,)).get()
        == 2
    )


def test_a_broker_error_is_logged_by_id_only(
    event: Event,
    member: User,
    django_capture_on_commit_callbacks: Capture,
    caplog: pytest.LogCaptureFixture,
) -> None:
    a, b = PlayerFactory.create(), PlayerFactory.create()
    results_file = upload(member, event, [row(a, b, 5)])

    with (
        mock.patch.object(
            tasks.send_batch_notices, "delay", side_effect=OSError("refused")
        ),
        caplog.at_level(logging.ERROR, logger=notices.__name__),
        django_capture_on_commit_callbacks(execute=True),
    ):
        process_results_file(results_file.pk)

    assert [r.getMessage() for r in caplog.records if r.name == notices.__name__] == [
        f"Could not queue notices for submission batch "
        f"{results_file.submission_batch_id} (OSError)."
    ]


def test_the_soft_time_limit_stops_the_batch_notices(
    event: Event, member: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = PlayerFactory.create(), PlayerFactory.create()
    results_file = upload(member, event, [row(a, b, 5)])
    process_results_file(results_file.pk)

    def out_of_time(player: Player, matches: list[Match]) -> None:
        raise SoftTimeLimitExceeded

    monkeypatch.setattr(notices, "render_recorded_notice", out_of_time)

    with pytest.raises(SoftTimeLimitExceeded):
        notices.send_batch_notices(results_file.submission_batch_id)


# Failures and the email itself


def test_a_failed_notice_is_logged_without_details_and_others_still_go(
    players: list[Player],
    event: Event,
    member: User,
    django_capture_on_commit_callbacks: Capture,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = players[0].account.email

    def render(player: Player, matches: list[Match]) -> EmailMultiAlternatives:
        if player.pk == players[0].pk:
            raise RuntimeError(f"failed for {secret}")
        return render_recorded_notice(player, matches)

    monkeypatch.setattr(notices, "render_recorded_notice", render)

    with (
        caplog.at_level(logging.WARNING, logger=notices.__name__),
        django_capture_on_commit_callbacks(execute=True),
    ):
        match = _event_match(players, event, member)

    assert set(_sent()) == {player.account.email for player in players[1:]}
    assert [r.getMessage() for r in caplog.records if r.name == notices.__name__] == [
        f"Could not queue notices for match {match.pk} (RuntimeError)."
    ]
    assert secret not in caplog.text


def test_the_message_is_clean_and_queueable(
    players: list[Player], event: Event, member: User
) -> None:
    match = _event_match(players, event, member)

    message = render_recorded_notice(players[0], [match])

    assert_clean_message(message)
    assert queue_email(message, template=RECORDED_NOTICE).html


def test_render_refusals(
    players: list[Player], event: Event, member: User, pending: Match
) -> None:
    match = _event_match(players, event, member, games=((11, 2), (11, 3)))
    stranger = PlayerFactory.create()
    no_games = MatchFactory.create(
        event=event, status=MatchStatus.CONFIRMED, participants=False, games=False
    )
    no_games.participants.create(player=players[0], team=Team.A)

    with pytest.raises(ValueError, match="at least one match"):
        render_recorded_notice(players[0], [])
    with pytest.raises(ValueError, match="each match once"):
        render_recorded_notice(players[0], [match, match])
    with pytest.raises(ValueError, match="did not play"):
        render_recorded_notice(stranger, [match])
    with pytest.raises(ValueError, match="confirmed or disputed"):
        render_recorded_notice(players[0], [pending])
    with pytest.raises(ValueError, match="with games"):
        render_recorded_notice(players[0], [no_games])
    _deactivate(players[0].account)
    with pytest.raises(ValueError, match="active account"):
        render_recorded_notice(players[0], [match])


def test_a_disputed_match_is_still_listed(
    players: list[Player], event: Event, member: User
) -> None:
    match = _event_match(players, event, member)
    dispute_match(match, actor=players[2].account)
    match.refresh_from_db()

    message = render_recorded_notice(players[0], [match])

    assert str(match.public_code) in message.body
