"""The moderator's bulk void actions in the match admin (#205).

Two actions, for one selected match: void every match of its upload
batch, or every match its submitter recorded since it. The first POST
shows a confirmation page with the counts and changes nothing; the
confirm POST runs the service. No page or message shows a player's name
or any Account's email. Made-up data only.
"""

from datetime import UTC, date, datetime, timedelta

import pytest
from django.contrib.auth.models import Group
from django.contrib.messages import get_messages
from django.contrib.messages.storage.base import Message
from django.http import HttpResponse
from django.test import Client
from django.urls import reverse

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditAction, AuditEvent
from apps.clubs.tests.factories import OrganizerMemberFactory, verified_organizer
from apps.events.models import Event
from apps.events.tests.factories import EventFactory
from apps.matches.admin import (
    BLANK_REASON,
    BULK_SELECT_ONE,
    NO_BATCH,
)
from apps.matches.models import Match, MatchFormat, MatchStatus, SubmissionBatch
from apps.matches.permissions import MODERATOR_GROUP
from apps.matches.services import record_match
from apps.matches.tests.factories import raw_update
from apps.matches.workflow import void_match
from apps.partners.models import PartnerApplication
from apps.partners.tests.factories import approved_application
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db

CHANGELIST = "admin:matches_match_changelist"
CONFIRMATION = "admin/matches/match/bulk_void_confirmation.html"
BATCH_ACTION = "void_batch_selected"
SUBMITTER_ACTION = "void_submitter_selected"
REASON = "Made-up moderator reason."
FIRST_DAY = date(2026, 3, 1)
GAMES = ((11, 7), (11, 9))


@pytest.fixture
def moderator() -> User:
    account: User = UserFactory.create(is_staff=True)
    account.groups.add(Group.objects.get(name=MODERATOR_GROUP))
    return account


@pytest.fixture
def moderator_client(client: Client, moderator: User) -> Client:
    client.force_login(moderator)
    return client


@pytest.fixture
def players() -> list[Player]:
    return [PlayerFactory.create(display_name=f"Zqadult{n} Made-up") for n in range(2)]


@pytest.fixture
def event() -> Event:
    made: Event = EventFactory.create(organizer=verified_organizer())
    return made


@pytest.fixture
def member(event: Event) -> User:
    account: User = UserFactory.create()
    OrganizerMemberFactory.create(organizer=event.organizer, account=account)
    return account


def _record(
    players: list[Player],
    day: int,
    event: Event,
    *,
    user: User | None = None,
    application: PartnerApplication | None = None,
    batch: SubmissionBatch | None = None,
) -> Match:
    return record_match(
        format=MatchFormat.SINGLES,
        team_a=players[:1],
        team_b=players[1:2],
        games=GAMES,
        played_on=FIRST_DAY + timedelta(days=day),
        event=event,
        submitted_by_user=user,
        submitted_by_application=application,
        submission_batch=batch,
    ).match


def _batch(
    players: list[Player], event: Event, member: User, count: int = 3
) -> list[Match]:
    batch = SubmissionBatch.objects.create(submitted_by_user=member, event=event)
    return [
        _record(players, day, event, user=member, batch=batch) for day in range(count)
    ]


def _post(
    client: Client,
    action: str,
    matches: list[Match],
    reason: str | None = REASON,
    *,
    confirm: bool = False,
) -> tuple[HttpResponse, list[Message]]:
    data: dict[str, object] = {
        "action": action,
        "_selected_action": [str(m.pk) for m in matches],
    }
    if reason is not None:
        data["reason"] = reason
    if confirm:
        data["post"] = "yes"
    response = client.post(reverse(CHANGELIST) + "?review=all", data)
    return response, list(get_messages(response.wsgi_request))


def _statuses(matches: list[Match]) -> set[str]:
    return set(
        Match.objects.filter(pk__in=[m.pk for m in matches]).values_list(
            "status", flat=True
        )
    )


def _templates(response: HttpResponse) -> list[str]:
    return [t.name for t in getattr(response, "templates", []) if t.name]


# Who sees the actions


def test_a_moderator_sees_both_actions(
    moderator_client: Client, players: list[Player], event: Event, member: User
) -> None:
    # Django shows the action bar only on a list with matches.
    _batch(players, event, member, count=1)

    page = moderator_client.get(reverse(CHANGELIST) + "?review=all").content.decode()

    assert BATCH_ACTION in page
    assert SUBMITTER_ACTION in page


def test_staff_without_the_group_sees_neither_and_cannot_post(
    client: Client, players: list[Player], event: Event, member: User
) -> None:
    matches = _batch(players, event, member)
    client.force_login(UserFactory.create(is_staff=True))
    before = AuditEvent.objects.count()

    page = client.get(reverse(CHANGELIST) + "?review=all").content.decode()
    for action in (BATCH_ACTION, SUBMITTER_ACTION):
        for confirm in (False, True):
            _post(client, action, matches[:1], confirm=confirm)

    assert BATCH_ACTION not in page
    assert SUBMITTER_ACTION not in page
    assert _statuses(matches) == {MatchStatus.CONFIRMED}
    assert AuditEvent.objects.count() == before


# Refusals before the page


@pytest.mark.parametrize("action", [BATCH_ACTION, SUBMITTER_ACTION])
@pytest.mark.parametrize("confirm", [False, True])
def test_a_blank_reason_warns_and_shows_no_page(
    moderator_client: Client,
    players: list[Player],
    event: Event,
    member: User,
    action: str,
    confirm: bool,
) -> None:
    matches = _batch(players, event, member)
    before = AuditEvent.objects.count()

    response, messages = _post(
        moderator_client, action, matches[:1], reason="  ", confirm=confirm
    )

    assert CONFIRMATION not in _templates(response)
    assert [str(m) for m in messages] == [BLANK_REASON]
    assert _statuses(matches) == {MatchStatus.CONFIRMED}
    assert AuditEvent.objects.count() == before


@pytest.mark.parametrize("action", [BATCH_ACTION, SUBMITTER_ACTION])
@pytest.mark.parametrize("confirm", [False, True])
def test_two_selected_matches_warn(
    moderator_client: Client,
    players: list[Player],
    event: Event,
    member: User,
    action: str,
    confirm: bool,
) -> None:
    matches = _batch(players, event, member)
    before = AuditEvent.objects.count()

    response, messages = _post(moderator_client, action, matches[:2], confirm=confirm)

    assert CONFIRMATION not in _templates(response)
    assert [str(m) for m in messages] == [BULK_SELECT_ONE]
    assert _statuses(matches) == {MatchStatus.CONFIRMED}
    assert AuditEvent.objects.count() == before


@pytest.mark.parametrize("confirm", [False, True])
def test_a_match_with_no_batch_warns(
    moderator_client: Client,
    players: list[Player],
    event: Event,
    member: User,
    confirm: bool,
) -> None:
    single = _record(players, 10, event, user=member)
    before = AuditEvent.objects.count()

    response, messages = _post(
        moderator_client, BATCH_ACTION, [single], confirm=confirm
    )

    assert CONFIRMATION not in _templates(response)
    assert [str(m) for m in messages] == [NO_BATCH]
    assert _statuses([single]) == {MatchStatus.CONFIRMED}
    assert AuditEvent.objects.count() == before


# The batch action


def test_the_batch_confirmation_page_shows_counts_and_changes_nothing(
    moderator_client: Client,
    players: list[Player],
    event: Event,
    member: User,
    moderator: User,
) -> None:
    matches = _batch(players, event, member, count=4)
    void_match(matches[3], actor=moderator, reason=REASON)
    outside = _record(players, 10, event, user=member)
    before = AuditEvent.objects.count()

    response, messages = _post(moderator_client, BATCH_ACTION, matches[:1])

    assert response.status_code == 200
    assert CONFIRMATION in _templates(response)
    assert messages == []
    context = response.context
    assert context["to_void"] == 3
    assert context["already_voided"] == 1
    page = response.content.decode()
    assert matches[0].public_code in page
    assert REASON in page
    assert 'name="post" value="yes"' in page
    assert f'name="_selected_action" value="{matches[0].pk}"' in page
    assert f'name="action" value="{BATCH_ACTION}"' in page
    assert _statuses([*matches[:3], outside]) == {MatchStatus.CONFIRMED}
    assert AuditEvent.objects.count() == before


def test_the_batch_confirm_post_voids_the_batch_and_reports_counts(
    moderator_client: Client,
    players: list[Player],
    event: Event,
    member: User,
) -> None:
    matches = _batch(players, event, member)
    outside = _record(players, 10, event, user=member)

    response, messages = _post(
        moderator_client, BATCH_ACTION, matches[1:2], confirm=True
    )

    assert response.status_code == 302
    assert [str(m) for m in messages] == [
        "3 match(es) voided, 0 already voided, 0 refused."
    ]
    assert _statuses(matches) == {MatchStatus.VOIDED}
    assert _statuses([outside]) == {MatchStatus.CONFIRMED}
    assert AuditEvent.objects.filter(action=AuditAction.MATCH_VOID).count() == 3
    [bulk] = AuditEvent.objects.filter(action=AuditAction.MATCH_BULK_VOID)
    assert bulk.reason == REASON


# The submitter action


def test_the_submitter_confirmation_page_shows_the_scope(
    moderator_client: Client,
    players: list[Player],
    event: Event,
    member: User,
) -> None:
    earlier = _record(players, 0, event, user=member)
    selected = _record(players, 1, event, user=member)
    later = _record(players, 2, event, user=member)
    raw_update(earlier, created_at=datetime(2026, 11, 19, 23, 0, tzinfo=UTC))
    raw_update(selected, created_at=datetime(2026, 11, 20, 9, 0, tzinfo=UTC))
    raw_update(later, created_at=datetime(2026, 11, 21, 9, 0, tzinfo=UTC))
    before = AuditEvent.objects.count()

    response, _messages = _post(moderator_client, SUBMITTER_ACTION, [selected])

    assert CONFIRMATION in _templates(response)
    assert response.context["to_void"] == 2
    assert response.context["already_voided"] == 0
    page = response.content.decode()
    assert "Account" in page
    assert "2026-11-20" in page
    assert selected.public_code in page
    assert _statuses([earlier, selected, later]) == {MatchStatus.CONFIRMED}
    assert AuditEvent.objects.count() == before


def test_the_submitter_confirm_post_voids_since_the_selected_match(
    moderator_client: Client,
    players: list[Player],
    event: Event,
    member: User,
) -> None:
    earlier = _record(players, 0, event, user=member)
    selected = _record(players, 1, event, user=member)
    later = _record(players, 2, event, user=member)
    raw_update(earlier, created_at=datetime(2026, 11, 19, 23, 0, tzinfo=UTC))
    raw_update(selected, created_at=datetime(2026, 11, 20, 9, 0, tzinfo=UTC))
    raw_update(later, created_at=datetime(2026, 11, 21, 9, 0, tzinfo=UTC))

    _response, messages = _post(
        moderator_client, SUBMITTER_ACTION, [selected], confirm=True
    )

    assert [str(m) for m in messages] == [
        "2 match(es) voided, 0 already voided, 0 refused."
    ]
    assert _statuses([earlier]) == {MatchStatus.CONFIRMED}
    assert _statuses([selected, later]) == {MatchStatus.VOIDED}
    [bulk] = AuditEvent.objects.filter(action=AuditAction.MATCH_BULK_VOID)
    assert bulk.target_object_id == str(member.pk)
    assert bulk.after is not None
    assert bulk.after["recorded_since"] == "2026-11-20"


def test_an_application_submitter_is_shown_by_client_id(
    moderator_client: Client, players: list[Player], event: Event
) -> None:
    application = approved_application(organizer=event.organizer)
    selected = _record(players, 0, event, application=application)

    response, _messages = _post(moderator_client, SUBMITTER_ACTION, [selected])

    assert CONFIRMATION in _templates(response)
    page = response.content.decode()
    assert application.client_id in page
    assert application.name not in page


# Nothing identifying


def test_no_names_or_emails_on_pages_or_messages(
    moderator_client: Client,
    players: list[Player],
    event: Event,
    member: User,
) -> None:
    matches = _batch(players, event, member)
    secret = [*(p.display_name for p in players), *(p.account.email for p in players)]
    secret.append(member.email)

    texts: list[str] = []
    for action in (BATCH_ACTION, SUBMITTER_ACTION):
        response, messages = _post(moderator_client, action, matches[:1])
        texts.append(response.content.decode())
        texts.extend(str(m) for m in messages)
    for action in (BATCH_ACTION, SUBMITTER_ACTION):
        _response, messages = _post(moderator_client, action, matches[:1], confirm=True)
        texts.extend(str(m) for m in messages)
    change = moderator_client.get(
        reverse("admin:matches_match_change", args=[matches[0].pk])
    )
    texts.append(change.content.decode())

    for text in texts:
        for value in secret:
            assert value not in text


def test_the_change_page_shows_the_batch(
    moderator_client: Client,
    players: list[Player],
    event: Event,
    member: User,
) -> None:
    matches = _batch(players, event, member, count=1)
    batch = SubmissionBatch.objects.get()

    page = moderator_client.get(
        reverse("admin:matches_match_change", args=[matches[0].pk])
    ).content.decode()

    assert f"Submission batch {batch.pk}" in page
