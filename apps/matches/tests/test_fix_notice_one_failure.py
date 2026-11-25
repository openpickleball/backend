"""One recipient's failed notice does not drop the others (#272, F5).

A failure rendering or queuing one email is logged with the match's row
id and the error class only, and the other recipients and matches still
get theirs. Made-up data only.
"""

import logging
from collections.abc import Callable
from typing import Any

import pytest
from django.core import mail
from django.core.mail import EmailMultiAlternatives

from apps.matches import emails, notices
from apps.matches.models import Match, MatchFormat
from apps.matches.services import record_match
from apps.matches.tests.conftest import WORKFLOW_PLAYED_ON
from apps.matches.workflow import dispute_match
from apps.players.models import Player

pytestmark = pytest.mark.django_db

SECRET = "made-up secret text"
Capture = Callable[..., Any]


def _failing_for(
    real: Callable[..., EmailMultiAlternatives], account_pk: int
) -> Callable[..., EmailMultiAlternatives]:
    def render(account: Any, *args: Any, **kwargs: Any) -> EmailMultiAlternatives:
        if account.pk == account_pk:
            raise RuntimeError(SECRET)
        return real(account, *args, **kwargs)

    return render


def _notice_logs(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == notices.__name__]


def test_one_failed_dispute_notice_does_not_drop_the_other(
    players: list[Player],
    confirmed: Match,
    django_capture_on_commit_callbacks: Capture,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(
        notices,
        "render_dispute_notice",
        _failing_for(emails.render_dispute_notice, players[2].account.pk),
    )

    with (
        caplog.at_level(logging.WARNING, logger=notices.__name__),
        django_capture_on_commit_callbacks(execute=True),
    ):
        dispute_match(confirmed, actor=players[0].account)

    assert [m.to for m in mail.outbox] == [[players[3].account.email]]
    assert _notice_logs(caplog) == [
        f"Could not queue notices for match {confirmed.pk} (RuntimeError)."
    ]
    assert SECRET not in caplog.text


def test_one_failed_hold_notice_does_not_drop_the_others(
    players: list[Player],
    confirmed: Match,
    django_capture_on_commit_callbacks: Capture,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(
        notices,
        "render_hold_notice",
        _failing_for(emails.render_hold_notice, players[0].account.pk),
    )

    with (
        caplog.at_level(logging.WARNING, logger=notices.__name__),
        django_capture_on_commit_callbacks(execute=True),
    ):
        newer = record_match(
            format=MatchFormat.DOUBLES,
            team_a=players[:2],
            team_b=players[2:],
            games=((11, 2), (11, 3)),
            played_on=WORKFLOW_PLAYED_ON,
            submitted_by_user=players[1].account,
        ).match

    # Three other players, for each of the two held matches.
    assert len(mail.outbox) == 6
    assert players[0].account.email not in {m.to[0] for m in mail.outbox}
    for held in (newer, confirmed):
        assert sum(str(held.public_code) in str(m.body) for m in mail.outbox) == 3
    assert sorted(_notice_logs(caplog)) == sorted(
        f"Could not queue notices for match {held.pk} (RuntimeError)."
        for held in (newer, confirmed)
    )
    assert SECRET not in caplog.text
