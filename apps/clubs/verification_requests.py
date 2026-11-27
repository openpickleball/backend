"""Closing Organizer Verification Requests (#229, ADR 0054).

An admin member asks moderators to verify an Organizer through
``apps.clubs.self_service.request_verification``. A request changes
nothing about verification: only ``verify_organizer`` verifies. A request
is closed in one of two ways, each with one
``organizer_verification_request.close`` Audit Event in the same
transaction:

- ``close_open_verification_requests()``, which only ``verify_organizer``
  calls, under its Organizer lock, with rule ``verified``.
- ``close_verification_request()``, by a moderator who does not verify:
  an Account ``apps.clubs.reports.may_close`` allows (active, with
  ``clubs.verify_organizer``, not a member of the Organizer), with a
  required reason, and rule ``moderator``. It locks only the request
  row, never the Organizer, like ``close_report``.

``blank_statements()`` clears the statements of an Organizer whose only
member deletes their Account (ADR 0048).
"""

from typing import TYPE_CHECKING

from django.db import transaction
from django.utils import timezone

from apps.audit.models import AuditAction
from apps.audit.services import record_event, snapshot
from apps.clubs.models import Organizer, OrganizerVerificationRequest
from apps.clubs.reports import may_close

if TYPE_CHECKING:
    from django.contrib.auth.models import AnonymousUser, _User

RULE_VERIFIED = "verified"
RULE_MODERATOR = "moderator"

# Fixed texts, never a submitted value.
_NOT_ALLOWED = "Only a moderator who is not a member of the organizer may close it."
_REASON_REQUIRED = "Give a reason to close the request."
_CLOSED_FIELDS = ["closed_at"]


class NotAllowedToCloseRequest(PermissionError):
    """The actor may not close this verification request."""

    def __init__(self) -> None:
        super().__init__(_NOT_ALLOWED)


def close_verification_request(
    request: OrganizerVerificationRequest,
    *,
    actor: "_User | AnonymousUser",
    reason: str,
) -> bool:
    """Close ``request`` as a moderator, without verifying, with a reason.

    Raises ``NotAllowedToCloseRequest`` for an actor ``may_close`` refuses
    and ``ValueError`` for a blank reason. Returns False and records
    nothing when the request is already closed.
    """
    if not may_close(actor, request.organizer_id):
        raise NotAllowedToCloseRequest
    if not reason.strip():
        raise ValueError(_REASON_REQUIRED)
    with transaction.atomic():
        locked = OrganizerVerificationRequest.objects.select_for_update().get(
            pk=request.pk
        )
        if locked.closed_at is not None:
            return False
        _close(locked, actor=actor, reason=reason, rule=RULE_MODERATOR)
    request.closed_at = locked.closed_at
    return True


def close_open_verification_requests(
    organizer: Organizer, *, actor: "_User", reason: str
) -> int:
    """Close the open request on ``organizer`` as verified; return how many.

    For ``apps.clubs.services``: call inside the transaction that holds
    the Organizer row's lock, after it verified the Organizer.
    """
    requests = OrganizerVerificationRequest.objects.select_for_update().filter(
        organizer_id=organizer.pk, closed_at__isnull=True
    )
    count = 0
    for request in requests.order_by("pk"):
        _close(request, actor=actor, reason=reason, rule=RULE_VERIFIED)
        count += 1
    return count


def blank_statements(organizer_id: int) -> None:
    """Clear every statement on the Organizer (Account deletion, under its
    Organizer lock). The statement may describe the person."""
    OrganizerVerificationRequest.objects.filter(organizer_id=organizer_id).exclude(
        statement=""
    ).update(statement="")


def _close(
    locked: OrganizerVerificationRequest,
    *,
    actor: "_User | AnonymousUser | None",
    reason: str,
    rule: str,
) -> None:
    before = snapshot(locked, _CLOSED_FIELDS)
    locked.closed_at = timezone.now()
    locked.save(update_fields=_CLOSED_FIELDS)
    record_event(
        action=AuditAction.ORGANIZER_VERIFICATION_REQUEST_CLOSE,
        target=locked,
        actor=actor,
        before=before,
        after={**snapshot(locked, _CLOSED_FIELDS), "rule": rule},
        reason=reason,
    )
