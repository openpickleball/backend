"""A duplicate voided while ``record_match`` waits for it, and the locks an
edit takes (review finding F, #26). Made-up data only.

(1) When the exact duplicate ``record_match`` found is voided before its
row lock is granted, the submission is recorded as a new match instead of
being refused for a voided one.

(2) ``edit_match`` holds the old match's lineup lock as well as the new
one's, so a submission for either lineup waits for the edit.
"""

from datetime import date

import pytest
from django.db import connection

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.clubs.tests.factories import OrganizerMemberFactory, verified_organizer
from apps.events.tests.factories import EventFactory
from apps.matches import services
from apps.matches.models import Match, MatchFormat, MatchStatus, Team
from apps.matches.services import MatchDraft, _lineup_lock_key, record_match
from apps.matches.tests.factories import DoublesMatchFactory, raw_update
from apps.matches.workflow import edit_match
from apps.players.models import Player

pytestmark = pytest.mark.django_db

PLAYED_ON = date(2026, 6, 1)


def held_advisory_keys() -> set[int]:
    """The signed 64-bit advisory lock keys this session holds."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT classid::bigint, objid::bigint FROM pg_locks "
            "WHERE locktype = 'advisory' AND objsubid = 1 "
            "AND pid = pg_backend_pid()"
        )
        rows = cursor.fetchall()
    keys = set()
    for high, low in rows:
        unsigned = (high << 32) | low
        keys.add(unsigned - (1 << 64) if unsigned >= 1 << 63 else unsigned)
    return keys


def test_a_duplicate_voided_before_its_lock_is_not_cited(
    pending: Match, players: list[Player], monkeypatch: pytest.MonkeyPatch
) -> None:
    real = services.exact_duplicate
    raced: list[int] = []

    def racing(draft: MatchDraft) -> Match | None:
        found = real(draft)
        if found is not None and not raced:
            # Another transaction voids it before this one's row lock.
            raced.append(found.pk)
            raw_update(found, status=MatchStatus.VOIDED)
        return found

    monkeypatch.setattr(services, "exact_duplicate", racing)
    event = EventFactory.create(organizer=verified_organizer())
    account: User = UserFactory.create()
    OrganizerMemberFactory.create(organizer=event.organizer, account=account)
    stored = Match.objects.get(pk=pending.pk)

    recorded = record_match(
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=[(g.team_a_score, g.team_b_score) for g in stored.games.all()],
        played_on=stored.played_on,
        submitted_by_user=account,
        event=event,
    )

    assert raced == [pending.pk]
    assert recorded.created
    assert recorded.match.pk != pending.pk
    assert recorded.match.status == MatchStatus.CONFIRMED


def test_an_edit_holds_the_old_and_the_new_lineup_locks(moderator: User) -> None:
    # Built by the factory, so no earlier call in this test took its locks.
    old: Match = DoublesMatchFactory.create(played_on=PLAYED_ON)
    raw_update(old, status=MatchStatus.CONFIRMED)
    teams = {
        team: [p.player for p in old.participants.filter(team=team)] for team in Team
    }
    keys = {team: [player.pk for player in members] for team, members in teams.items()}
    new_on = date(2026, 5, 20)
    assert held_advisory_keys() == set()

    edit_match(
        old,
        actor=moderator,
        format=MatchFormat.DOUBLES,
        team_a=teams[Team.A],
        team_b=teams[Team.B],
        games=[(11, 5), (11, 8)],
        played_on=new_on,
    )

    held = held_advisory_keys()
    assert _lineup_lock_key(keys[Team.A], keys[Team.B], PLAYED_ON) in held
    assert _lineup_lock_key(keys[Team.A], keys[Team.B], new_on) in held
