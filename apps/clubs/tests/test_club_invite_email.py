"""The Club admin invite email (#230): OPEN's templates, one action link
into the web app, and no inviter or Club name. Made-up data only.
"""

from datetime import UTC, datetime
from pathlib import Path

from django.conf import settings

from apps.clubs.emails import render_club_invite
from apps.emails.tests.hygiene import (
    assert_clean_message,
    assert_clean_template,
    assert_links_only_to_the_web_app,
    hrefs,
)

TEMPLATES = Path(settings.BASE_DIR) / "templates" / "clubs" / "email"
KEY = "MadeUpInviteKey_0123456789abcdefghijklmnopq"
EXPIRES = datetime(2026, 12, 3, 9, 30, 12, tzinfo=UTC)


def test_the_templates_are_clean() -> None:
    files = sorted(TEMPLATES.glob("club_invite_*"))

    assert [f.name for f in files] == [
        "club_invite_message.html",
        "club_invite_message.txt",
        "club_invite_subject.txt",
    ]
    for path in files:
        assert_clean_template(path)


def test_the_message_links_only_to_the_invite_page() -> None:
    message = render_club_invite(
        "invitee@example.com",
        club_code="C-MADE-UP",
        key=KEY,
        expires_at=EXPIRES,
    )
    url = f"{settings.FRONTEND_URL}/clubs/invites/{KEY}"

    assert message.to == ["invitee@example.com"]
    assert message.subject == "You are invited to be a club admin on OPEN"
    assert url in str(message.body)
    assert "C-MADE-UP" in str(message.body)
    assert "2026-12-03 09:30 UTC" in str(message.body)
    assert "invitee@example.com" not in str(message.body)
    (html, mimetype) = message.alternatives[0]
    assert mimetype == "text/html"
    assert set(hrefs(str(html))) == {url}
    assert "C-MADE-UP" in str(html)
    assert "invitee@example.com" not in str(html)
    assert message.attachments == []
    assert_clean_message(message)
    assert_links_only_to_the_web_app(str(html))
