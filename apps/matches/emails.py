"""The match emails: the confirmation request (issue #36) and the dispute
and hold notices (#272).

They render only. #37 decides when to send the confirmation request;
``apps.matches.notices`` queues the dispute and hold notices with
``apps.emails.services.queue_email``.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING

from django.conf import settings
from django.core.mail import EmailMultiAlternatives

from apps.emails.rendering import render_email
from apps.matches.models import Match, MatchStatus, Team
from apps.players.models import Player

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

TEMPLATE_PREFIX = "matches/email/confirmation_request"
DISPUTE_NOTICE = "matches/email/dispute_notice"
HOLD_NOTICE = "matches/email/hold_notice"


@dataclass(frozen=True)
class PendingMatchLine:
    """One Match as the email lists it. No player is named."""

    code: str
    played_on: date
    scores: str
    url: str


def match_url(match: Match) -> str:
    """The web app's page for a Match, where a player confirms or disputes it.

    The ``/matches/{code}`` route is a new web app route; #37 and the web
    app adopt it.
    """
    return f"{settings.FRONTEND_URL}/matches/{match.public_code}"


def _line(player: Player, match: Match) -> PendingMatchLine:
    team = (
        match.participants.filter(player=player).values_list("team", flat=True).first()
    )
    if team is None:
        raise ValueError("The player did not play in every match.")
    if match.status != MatchStatus.PENDING:
        raise ValueError("A confirmation request is only for pending matches.")
    scores = _scores(match, team)
    if not scores:
        raise ValueError("A confirmation request needs a match with games.")
    return PendingMatchLine(
        code=str(match.public_code),
        played_on=match.played_on,
        scores=scores,
        url=match_url(match),
    )


def _scores(match: Match, team: str | None) -> str:
    """The game scores in game order, ``team``'s score first.

    With no team (an Organizer member, who plays on no side), team A's
    score comes first. Empty for a match with no games.
    """
    scores = []
    for game in match.games.order_by("game_number"):
        mine, theirs = game.team_a_score, game.team_b_score
        if team == Team.B:
            mine, theirs = theirs, mine
        scores.append(f"{mine}-{theirs}")
    return ", ".join(scores)


def render_confirmation_request(
    player: Player, matches: Sequence[Match]
) -> EmailMultiAlternatives:
    """Ask a Player to confirm one or more Matches they played in.

    Each Match is listed in the order given, with its OPEN Code, the date
    it was played, the game scores from the Player's side (their team
    first), and a link to it in the web app (``match_url``). The email
    names no player, location, or submitter, so no other player's details
    reach the mailbox or the provider's logs.

    Refuses, with fixed text that names no address or code: a Player
    whose Account is inactive, an empty list, the same Match more than
    once, a Match the Player did not play in, a Match that is not
    pending, and a Match with no games.
    """
    if not player.account.is_active:
        raise ValueError("A confirmation request needs an active account.")
    if not matches:
        raise ValueError("A confirmation request needs at least one match.")
    if len({match.pk for match in matches}) != len(matches):
        raise ValueError("A confirmation request lists each match once.")
    lines = [_line(player, match) for match in matches]
    return render_email(TEMPLATE_PREFIX, player.account.email, {"matches": lines})


def _notice(
    prefix: str,
    account: "_User",
    match: Match,
    *,
    team: str | None,
    ends_on: date,
) -> EmailMultiAlternatives:
    scores = _scores(match, team)
    if not scores:
        name = "dispute" if prefix == DISPUTE_NOTICE else "hold"
        raise ValueError(f"A {name} notice needs a match with games.")
    context: dict[str, object] = {
        "code": str(match.public_code),
        "played_on": match.played_on,
        "scores": scores,
        "url": match_url(match),
        "ends_on": ends_on,
        "as_organizer": team is None,
    }
    if prefix == DISPUTE_NOTICE:
        # Read from the stored match, so the email offers only what
        # someone can do: the Event's verified Organizer settles a
        # dispute, and the players correct a self-reported match. Only
        # the dispute notice uses them.
        context["organizer_level"] = Match.objects.filter(
            pk=match.pk, event__organizer__verified_at__isnull=False
        ).exists()
        context["self_reported"] = Match.objects.filter(
            pk=match.pk, event__isnull=True, club__isnull=True
        ).exists()
    return render_email(prefix, account.email, context)


def render_dispute_notice(
    account: "_User", match: Match, *, team: str | None, ends_on: date
) -> EmailMultiAlternatives:
    """Tell ``account`` that ``match`` was disputed (#272).

    ``team`` is the recipient's team in the match, whose scores come
    first, or None for a member of the Event's Organizer, who sees team A
    first and the Organizer's wording. ``ends_on`` is the date the dispute
    ends on its own (the match is voided then). The email lists the
    match's OPEN Code, date, scores, and a link to it (``match_url``). It
    names no player, location, submitter, Event, Club, or Organizer, and
    quotes no text another person wrote.

    Refuses, with fixed text that names no address or code: an inactive
    Account (``is_active`` is read from the instance given, so pass a
    freshly fetched row), a match that is not disputed, and a match with
    no games.
    """
    if not account.is_active:
        raise ValueError("A dispute notice needs an active account.")
    if match.status != MatchStatus.DISPUTED:
        raise ValueError("A dispute notice is only for disputed matches.")
    return _notice(DISPUTE_NOTICE, account, match, team=team, ends_on=ends_on)


def render_hold_notice(
    account: "_User", match: Match, *, team: str | None, ends_on: date
) -> EmailMultiAlternatives:
    """Tell ``account`` that a possible duplicate flag holds ``match`` (#272).

    As ``render_dispute_notice``, with ``ends_on`` the date from which the
    hold ends on its own when the earlier of the two matches is confirmed
    (ADR 0034). The other match is never named. Refuses an inactive
    Account, a voided match, and a match with no games, with fixed text.
    """
    if not account.is_active:
        raise ValueError("A hold notice needs an active account.")
    if match.status == MatchStatus.VOIDED:
        raise ValueError("A hold notice is not for a voided match.")
    return _notice(HOLD_NOTICE, account, match, team=team, ends_on=ends_on)
