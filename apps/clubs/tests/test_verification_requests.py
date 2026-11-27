"""Closing Organizer Verification Requests (#229, ADR 0054).

A verify closes the open request (rule ``verified``); a moderator who
does not verify closes it with a reason (rule ``moderator``). Neither
the request nor its close changes verification. Made-up data only.
"""

import pytest
from django.contrib.auth.models import Permission

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditAction, AuditEvent
from apps.clubs.models import MemberRole
from apps.clubs.services import unverify_organizer, verify_organizer
from apps.clubs.tests.factories import (
    OrganizerFactory,
    OrganizerMemberFactory,
    OrganizerVerificationRequestFactory,
)
from apps.clubs.verification_requests import (
    NotAllowedToCloseRequest,
    blank_statements,
    close_verification_request,
)

pytestmark = pytest.mark.django_db


def _moderator() -> User:
    account: User = UserFactory.create()
    account.user_permissions.add(Permission.objects.get(codename="verify_organizer"))
    return User.objects.get(pk=account.pk)


def _closes() -> list[AuditEvent]:
    return list(
        AuditEvent.objects.filter(
            action=AuditAction.ORGANIZER_VERIFICATION_REQUEST_CLOSE
        )
    )


def test_verify_closes_the_open_request() -> None:
    request = OrganizerVerificationRequestFactory.create()
    moderator = _moderator()

    verify_organizer(request.organizer, actor=moderator, reason="Checked it.")

    request.refresh_from_db()
    assert request.closed_at is not None
    [event] = _closes()
    assert event.after is not None
    assert event.after["rule"] == "verified"
    assert event.actor_user_id == moderator.pk
    assert event.reason == "Checked it."


def test_unverify_and_a_repeat_verify_close_nothing_more() -> None:
    request = OrganizerVerificationRequestFactory.create()
    moderator = _moderator()
    verify_organizer(request.organizer, actor=moderator)
    verify_organizer(request.organizer, actor=moderator)
    unverify_organizer(request.organizer, actor=moderator)

    assert len(_closes()) == 1


def test_a_moderator_closes_a_request_without_verifying() -> None:
    request = OrganizerVerificationRequestFactory.create()

    assert close_verification_request(
        request, actor=_moderator(), reason="Not enough detail."
    )

    request.refresh_from_db()
    request.organizer.refresh_from_db()
    assert request.closed_at is not None
    assert request.organizer.verified_at is None
    [event] = _closes()
    assert event.after is not None
    assert event.after["rule"] == "moderator"
    assert event.reason == "Not enough detail."


def test_closing_a_closed_request_records_nothing() -> None:
    request = OrganizerVerificationRequestFactory.create()
    moderator = _moderator()
    close_verification_request(request, actor=moderator, reason="Done.")

    assert not close_verification_request(request, actor=moderator, reason="Again.")
    assert len(_closes()) == 1


def test_a_blank_reason_is_refused() -> None:
    request = OrganizerVerificationRequestFactory.create()

    with pytest.raises(ValueError, match="Give a reason"):
        close_verification_request(request, actor=_moderator(), reason="  ")


def test_a_member_moderator_or_a_non_moderator_cannot_close() -> None:
    request = OrganizerVerificationRequestFactory.create()
    member = _moderator()
    OrganizerMemberFactory.create(
        organizer=request.organizer, account=member, role=MemberRole.ADMIN
    )

    for actor in (member, UserFactory.create()):
        with pytest.raises(NotAllowedToCloseRequest):
            close_verification_request(request, actor=actor, reason="No.")
    assert _closes() == []


def test_blank_statements_clears_only_that_organizers() -> None:
    request = OrganizerVerificationRequestFactory.create(statement="Made-up text.")
    other = OrganizerVerificationRequestFactory.create(
        organizer=OrganizerFactory.create(), statement="Other made-up text."
    )

    blank_statements(request.organizer_id)

    request.refresh_from_db()
    other.refresh_from_db()
    assert request.statement == ""
    assert other.statement == "Other made-up text."
