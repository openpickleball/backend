"""Builders for the results file fix tests (#71).

Made-up data only.
"""

import csv
import io
from collections.abc import Sequence

from django.contrib.auth.models import Permission

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.clubs.tests.factories import OrganizerMemberFactory, verified_organizer
from apps.events.models import Event
from apps.events.tests.factories import EventFactory
from apps.matches.csv_format import COLUMNS
from apps.matches.models import GameTarget, ResultsFile
from apps.matches.results_files import create_results_file
from apps.players.models import Player


def organizer_member() -> tuple[Event, User]:
    event: Event = EventFactory.create(organizer=verified_organizer())
    member: User = UserFactory.create()
    OrganizerMemberFactory.create(organizer=event.organizer, account=member)
    return event, member


def moderator() -> User:
    account: User = UserFactory.create()
    account.user_permissions.add(
        Permission.objects.get(
            content_type__app_label="matches", codename="moderate_match"
        )
    )
    return account


def row(a: Player, b: Player, loser: int, *, name_a: str | None = None) -> list[str]:
    return (
        ["S", "SIDEOUT", "", "2026-06-01"]
        + [a.display_name if name_a is None else name_a, a.public_code, "", ""]
        + [b.display_name, b.public_code, "", ""]
        + ["11", str(loser)]
        + [""] * 8
    )


def upload(member: User, event: Event, rows: Sequence[list[str]]) -> ResultsFile:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(COLUMNS)
    writer.writerows(rows)
    return create_results_file(
        account=member,
        event=event,
        content=out.getvalue().encode(),
        game_target=GameTarget.ELEVEN,
        location="",
        players_13_or_older=True,
    )
