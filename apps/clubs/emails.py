"""The Organizer invite email (#229, ADR 0054) and the Club admin invite
email (#230).

``render_organizer_invite`` renders the one email an invite sends, to
the invited address, with OPEN's templates
(``templates/clubs/email/organizer_invite_*``). It names neither the
inviter nor the Organizer's name (ADR 0050: the name goes only to
members): only the Organizer's OPEN Code, the role, the expiry in UTC,
and the single-use link ``{FRONTEND_URL}/organizer/invites/{key}``.
It renders only; ``apps.clubs.self_service.invite_member`` queues it.

``render_club_invite`` renders the Club admin invite
(``templates/clubs/email/club_invite_*``) by the same rules: only the
Club's OPEN Code, the expiry in UTC, and the single-use link
``{FRONTEND_URL}/clubs/invites/{key}``, never the inviter or the Club's
name. ``apps.clubs.club_self_service.invite_admin`` queues it.

``render_security_notice`` renders one security notice (#232, ADR 0057)
to an admin of an Organizer or Club
(``templates/clubs/email/security_notice_*``): the OPEN Code, a fixed
sentence about the change, and what to do if it was not expected. It
never names a person, an email address, an Organizer's or Club's name,
or a client id. ``apps.clubs.notices`` builds the sentences and queues
it.
"""

from datetime import UTC, datetime
from typing import Final

from django.conf import settings
from django.core.mail import EmailMultiAlternatives

from apps.clubs.models import MemberRole
from apps.emails.rendering import render_email

ORGANIZER_INVITE: Final = "clubs/email/organizer_invite"
CLUB_INVITE: Final = "clubs/email/club_invite"
SECURITY_NOTICE: Final = "clubs/email/security_notice"


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


def club_invite_url(key: str) -> str:
    """The web app page that accepts the Club admin invite with ``key``."""
    return f"{settings.FRONTEND_URL}/clubs/invites/{key}"


def render_club_invite(
    to: str, *, club_code: str, key: str, expires_at: datetime
) -> EmailMultiAlternatives:
    """The Club admin invite email to ``to``."""
    return render_email(
        CLUB_INVITE,
        to,
        {
            "club_code": club_code,
            "invite_url": club_invite_url(key),
            "expires_at": expires_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        },
    )


def render_security_notice(
    to: str, *, scope: str, code: str, summary: str, follow_up: str
) -> EmailMultiAlternatives:
    """The security notice to ``to``. ``scope`` is ``organizer`` or
    ``club``; ``summary`` and ``follow_up`` are fixed sentences from
    ``apps.clubs.notices``, holding only OPEN Codes, a role, and a time."""
    return render_email(
        SECURITY_NOTICE,
        to,
        {"scope": scope, "code": code, "summary": summary, "follow_up": follow_up},
    )
