"""The moderator's match edit form (#40).

Players are entered by OPEN Code and games as "11-7" lines. Errors use
fixed text and never repeat what was typed. The edit itself goes through
``apps.matches.workflow.edit_match``.
"""

import re
from typing import Any

from django import forms

from apps.audit.admin_actions import REASON_MAX_LENGTH
from apps.matches.models import Match, Team
from apps.players.models import Player
from apps.public_ids.codes import CodeType, normalize

UNKNOWN_CODES = "Enter the OPEN Codes of existing players."
BAD_GAMES = "Enter each game on its own line as two scores, like 11-7."

_CODE_SEPARATORS = re.compile(r"[\s,]+")
_GAME_LINE = re.compile(r"(\d{1,3})\s*[-:]\s*(\d{1,3})")
_TEAM_HELP = "Player OPEN Codes, separated by commas or spaces."


def _team_codes(match: Match, team: Team) -> str:
    codes = (
        match.participants.filter(team=team)
        .order_by("pk")
        .values_list("player__public_code", flat=True)
    )
    return ", ".join(codes)


def _game_lines(match: Match) -> str:
    games = match.games.order_by("game_number")
    return "\n".join(f"{game.team_a_score}-{game.team_b_score}" for game in games)


def initial_values(match: Match) -> dict[str, Any]:
    """The form's starting values: the match as it is stored."""
    return {
        "played_on": match.played_on,
        "team_a": _team_codes(match, Team.A),
        "team_b": _team_codes(match, Team.B),
        "games": _game_lines(match),
    }


class MatchEditForm(forms.Form):
    """A correction of one match: date, players, and scores, with a reason."""

    reason = forms.CharField(
        max_length=REASON_MAX_LENGTH,
        widget=forms.Textarea(attrs={"rows": 2, "cols": 60}),
        help_text="Why the match is corrected. Stored in the audit log.",
        error_messages={"required": "Give a reason for the correction."},
    )
    played_on = forms.DateField(error_messages={"invalid": "Enter a valid date."})
    team_a = forms.CharField(label="Team A", max_length=200, help_text=_TEAM_HELP)
    team_b = forms.CharField(label="Team B", max_length=200, help_text=_TEAM_HELP)
    games = forms.CharField(
        max_length=200,
        widget=forms.Textarea(attrs={"rows": 5, "cols": 20}),
        help_text="One game per line, team A's score first, like 11-7.",
    )

    def _players(self, field: str) -> list[Player]:
        raw: str = self.cleaned_data[field]
        typed = [code for code in _CODE_SEPARATORS.split(raw) if code]
        canonical = [normalize(code, CodeType.PLAYER) for code in typed]
        codes = [code for code in canonical if code is not None]
        if not codes or len(codes) != len(typed):
            raise forms.ValidationError(UNKNOWN_CODES, code="unknown_codes")
        found = Player.objects.in_bulk(codes, field_name="public_code")
        if any(code not in found for code in codes):
            raise forms.ValidationError(UNKNOWN_CODES, code="unknown_codes")
        return [found[code] for code in codes]

    def clean_team_a(self) -> list[Player]:
        return self._players("team_a")

    def clean_team_b(self) -> list[Player]:
        return self._players("team_b")

    def clean_games(self) -> list[tuple[int, int]]:
        raw: str = self.cleaned_data["games"]
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
        games: list[tuple[int, int]] = []
        for line in lines:
            found = _GAME_LINE.fullmatch(line)
            if found is None:
                raise forms.ValidationError(BAD_GAMES, code="bad_games")
            games.append((int(found.group(1)), int(found.group(2))))
        if not games:
            raise forms.ValidationError(BAD_GAMES, code="bad_games")
        return games
