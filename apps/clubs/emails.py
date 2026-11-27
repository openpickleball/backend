"""The Organizer invite email (#229, ADR 0054).

``render_organizer_invite`` renders the one email an invite sends, to
the invited address, with OPEN's templates
(``templates/clubs/email/organizer_invite_*``). It names neither the
inviter nor the Organizer's name (ADR 0050: the name goes only to
members): only the Organizer's OPEN Code, the role, the expiry in UTC,
and the single-use link ``{FRONTEND_URL}/organizer/invites/{key}``.
It renders only; ``apps.clubs.self_service.invite_member`` queues it.
"""

from datetime import UTC, datetime
from typing import Final

from django.conf import settings
from django.core.mail import EmailMultiAlternatives

from apps.clubs.models import MemberRole
from apps.emails.rendering import render_email

ORGANIZER_INVITE: Final = "clubs/email/organizer_invite"


def invite_url(key: str) -> str:
    """The web app page that accepts the invite with ``key``."""
    return f"{settings.FRONTEND_URL}/organizer/invites/{key}"


def render_organizer_invite(
    to: str,
    *,
    organizer_code: str,
    role: str,
    key: str,
    expires_at: datetime,
) -> EmailMultiAlternatives:
    """The invite email to ``to``. ``role`` is a ``MemberRole`` value."""
    return render_email(
        ORGANIZER_INVITE,
        to,
        {
            "organizer_code": organizer_code,
            "role_label": str(MemberRole(role).label).lower(),
            "invite_url": invite_url(key),
            "expires_at": expires_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        },
    )
