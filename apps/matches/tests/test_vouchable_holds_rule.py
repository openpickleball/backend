"""The holds list agrees with ``vouch_level`` (#270). Made-up data only.

``vouchable_holds`` decides most of the vouching rule in SQL, so it must
list exactly the held matches that are not voided and that
``vouch_level`` lets the Account vouch for. This builds a ledger with
each case the SQL decides and compares the two for every Account.
"""

from datetime import date

import pytest
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.clubs.models import MemberRole
from apps.clubs.tests.factories import (
    ClubFactory,
    OrganizerFactory,
    OrganizerMemberFactory,
    verified_organizer,
)
from apps.events.tests.factories import EventFactory
from apps.matches.models import (
    FlagReason,
    FlagResolution,
    Match,
    MatchFlag,
    MatchParticipant,
    MatchStatus,
    Team,
)
from apps.matches.permissions import vouch_level
from apps.matches.tests.factories import DoublesMatchFactory
from apps.matches.workflow import vouchable_holds
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db

PLAYED_ON = date(2026, 5, 1)


def _match(
    players: list[Player], status: str = MatchStatus.CONFIRMED, **kwargs: object
) -> Match:
    """A doubles match: the first two players against the last two."""
    match: Match = DoublesMatchFactory.create(
        participants=False, played_on=PLAYED_ON, status=status, **kwargs
    )
    for index, player in enumerate(players):
        MatchParticipant.objects.create(
            match=match, player=player, team=Team.A if index < 2 else Team.B
        )
    return match


def _flag(match: Match, duplicate_of: Match, *, resolved: bool = False) -> None:
    flag = MatchFlag(
        match=match, duplicate_of=duplicate_of, reason=FlagReason.POSSIBLE_DUPLICATE
    )
    if resolved:
        flag.resolution = FlagResolution.APPROVED
        flag.resolved_at = timezone.now()
    flag.save()


def _held() -> list[Match]:
    """Every match an open flag holds whose two matches are not voided."""
    held: set[int] = set()
    for flag in MatchFlag.objects.filter(
        reason=FlagReason.POSSIBLE_DUPLICATE, resolved_at__isnull=True
    ).select_related("match", "duplicate_of"):
        assert flag.duplicate_of is not None
        if MatchStatus.VOIDED not in (flag.match.status, flag.duplicate_of.status):
            held |= {flag.match_id, flag.duplicate_of_id}
    return list(Match.objects.filter(pk__in=held))


def test_the_list_is_exactly_what_vouch_level_allows() -> None:
    organizer = verified_organizer()
    unverified = OrganizerFactory.create()
    event = EventFactory.create(organizer=organizer)
    unverified_event = EventFactory.create(organizer=unverified)
    club = ClubFactory.create()
    p = [PlayerFactory.create() for _ in range(16)]
    inactive_player = p[4].account
    inactive_player.is_active = False
    inactive_player.save(update_fields=["is_active"])
    member_without_player: User = UserFactory.create()
    inactive_member: User = UserFactory.create(is_active=False)
    unverified_member: User = UserFactory.create()
    non_member: User = UserFactory.create()
    for account in (
        member_without_player,
        inactive_member,
        p[1].account,
        p[6].account,
        p[7].account,
    ):
        OrganizerMemberFactory.create(
            organizer=organizer, account=account, role=MemberRole.ADMIN
        )
    OrganizerMemberFactory.create(
        organizer=unverified, account=unverified_member, role=MemberRole.ADMIN
    )

    # Self-reported pair, with an inactive player.
    _flag(_match([p[0], p[1], p[2], p[4]]), _match([p[0], p[1], p[2], p[4]]))
    # A legacy club-only match held with a self-reported one.
    _flag(_match([p[0], p[3], p[8], p[9]]), _match([p[0], p[3], p[8], p[9]], club=club))
    # An Event pair; member p6 plays in one of them.
    _flag(
        _match([p[13], p[10], p[11], p[12]], event=event),
        _match([p[6], p[10], p[11], p[12]], event=event),
    )
    # An Event match with a Club.
    _flag(
        _match([p[13], p[14], p[11], p[12]], event=event),
        _match([p[13], p[14], p[11], p[12]], event=event, club=club),
    )
    # An unverified Organizer's pair.
    _flag(
        _match([p[10], p[11], p[12], p[14]], event=unverified_event),
        _match([p[10], p[11], p[12], p[13]], event=unverified_event),
    )
    # Backward chain: member p7 played only the voided original.
    original = _match(
        [p[7], p[10], p[11], p[12]], event=event, status=MatchStatus.VOIDED
    )
    _flag(
        _match([p[15], p[10], p[11], p[12]], event=event),
        _match([p[15], p[10], p[11], p[12]], event=event, corrects=original),
    )
    # Forward chain: member p6 plays only in the pending correction.
    corrected = _match([p[14], p[10], p[11], p[12]], event=event)
    _match(
        [p[6], p[10], p[11], p[12]],
        event=event,
        corrects=corrected,
        status=MatchStatus.PENDING,
    )
    _flag(_match([p[14], p[10], p[11], p[12]], event=event), corrected)
    # The other match is voided.
    _flag(
        _match([p[0], p[1], p[5], p[8]]),
        _match([p[0], p[1], p[5], p[8]], status=MatchStatus.VOIDED),
    )
    # A resolved flag.
    _flag(
        _match([p[0], p[2], p[5], p[8]]),
        _match([p[0], p[2], p[5], p[8]]),
        resolved=True,
    )
    # Member p1 plays in both an Event match and a self-reported one.
    _flag(
        _match([p[1], p[2], p[3], p[5]]), _match([p[1], p[2], p[3], p[5]], event=event)
    )
    # Pending and disputed held matches.
    _flag(
        _match([p[0], p[5], p[3], p[9]], status=MatchStatus.DISPUTED),
        _match([p[0], p[5], p[3], p[9]], status=MatchStatus.PENDING),
    )

    held = _held()
    viewers = [player.account for player in p] + [
        member_without_player,
        inactive_member,
        unverified_member,
        non_member,
    ]
    listed_any = False
    for account in viewers:
        expected = {m.pk for m in held if vouch_level(account, m) is not None}
        listed = set(vouchable_holds(account).values_list("pk", flat=True))
        assert listed == expected, account.pk
        listed_any = listed_any or bool(listed)
    assert listed_any
    assert set(vouchable_holds(member_without_player))
    assert not set(vouchable_holds(inactive_player))
    assert not set(vouchable_holds(inactive_member))
    assert not set(vouchable_holds(unverified_member))
