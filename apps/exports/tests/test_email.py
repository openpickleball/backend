"""The data export email (issue #39, ADR 0051): OPEN's templates, one
action link into the web app, the 24-hour expiry, and no attachment.
Made-up data only.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from django.conf import settings
from django.core.mail import EmailMultiAlternatives

from apps.accounts.tests.factories import UserFactory
from apps.emails.tests.hygiene import (
    assert_clean_message,
    assert_clean_template,
    assert_links_only_to_the_web_app,
    hrefs,
)
from apps.exports.services import render_export_ready

pytestmark = pytest.mark.django_db

TEMPLATES = Path(settings.BASE_DIR) / "templates" / "exports" / "email"
KEY = "MadeUpDownloadKey_0123456789abcdefghijklmno"
EXPIRES = datetime(2026, 11, 27, 14, 5, 59, tzinfo=UTC)


def _message() -> EmailMultiAlternatives:
    return render_export_ready(UserFactory.create(), key=KEY, expires_at=EXPIRES)


def test_the_templates_are_clean() -> None:
    files = sorted(TEMPLATES.glob("data_export_ready_*"))

    assert [f.name for f in files] == [
        "data_export_ready_message.html",
        "data_export_ready_message.txt",
        "data_export_ready_subject.txt",
    ]
    for path in files:
        assert_clean_template(path)


def test_the_message_links_only_to_the_export_page() -> None:
    message = _message()
    url = f"{settings.FRONTEND_URL}/account/export/{KEY}"

    assert message.subject == "Your OPEN data export is ready"
    assert url in str(message.body)
    (html, mimetype) = message.alternatives[0]
    assert mimetype == "text/html"
    assert set(hrefs(str(html))) == {url}
    assert_clean_message(message)
    assert_links_only_to_the_web_app(str(html))


def test_the_message_states_the_expiry_and_attaches_nothing() -> None:
    message = _message()

    assert "2026-11-27 14:05 UTC" in str(message.body)
    assert "24 hours" in str(message.body)
    assert message.attachments == []
    assert len(message.alternatives) == 1


def test_the_message_never_names_the_recipient() -> None:
    account = UserFactory.create()
    message = render_export_ready(account, key=KEY, expires_at=EXPIRES)

    assert message.to == [account.email]
    for part in (
        str(message.subject),
        str(message.body),
        str(message.alternatives[0][0]),
    ):
        assert account.email not in part
