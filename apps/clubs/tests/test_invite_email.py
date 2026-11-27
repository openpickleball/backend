"""The Organizer invite email (#229): OPEN's templates, one action link
into the web app, and no inviter or Organizer name. Made-up data only.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from django.conf import settings

from apps.clubs.emails import render_organizer_invite
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
    files = sorted(TEMPLATES.glob("organizer_invite_*"))

    assert [f.name for f in files] == [
        "organizer_invite_message.html",
        "organizer_invite_message.txt",
        "organizer_invite_subject.txt",
    ]
    for path in files:
        assert_clean_template(path)


@pytest.mark.parametrize(
    ("role", "label"), [("admin", "admin"), ("match_manager", "match manager")]
)
def test_the_message_links_only_to_the_invite_page(role: str, label: str) -> None:
    message = render_organizer_invite(
        "invitee@example.com",
        organizer_code="ORG-MADE-UP",
        role=role,
        key=KEY,
        expires_at=EXPIRES,
    )
    url = f"{settings.FRONTEND_URL}/organizer/invites/{KEY}"

    assert message.to == ["invitee@example.com"]
    assert message.subject == "You are invited to act for an organizer on OPEN"
    assert url in str(message.body)
    assert f"as {label}." in str(message.body)
    assert "ORG-MADE-UP" in str(message.body)
    assert "2026-12-03 09:30 UTC" in str(message.body)
    assert "invitee@example.com" not in str(message.body)
    (html, mimetype) = message.alternatives[0]
    assert mimetype == "text/html"
    assert set(hrefs(str(html))) == {url}
    assert "invitee@example.com" not in str(html)
    assert message.attachments == []
    assert_clean_message(message)
    assert_links_only_to_the_web_app(str(html))
