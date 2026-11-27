"""Tests for /.well-known/security.txt (RFC 9116, issue #105)."""

from datetime import UTC, datetime, timedelta, timezone

import pytest
from django.test import Client, RequestFactory
from pytest_django.fixtures import Settings

from config.security_txt import security_txt_content

URL = "/.well-known/security.txt"
CONTACT = "mailto:security@example.org"
POLICY = "https://github.com/openpickleball/.github/blob/main/SECURITY.md"


def _fields(body: str) -> dict[str, str]:
    """Parse the file into one value per field, failing on a repeat."""
    fields: dict[str, str] = {}
    for line in body.splitlines():
        name, sep, value = line.partition(": ")
        assert sep, f"not a field line: {line!r}"
        assert name not in fields, f"{name} appears twice"
        fields[name] = value
    return fields


@pytest.fixture
def configured(settings: Settings) -> Settings:
    settings.SECURITY_TXT_CONTACT = CONTACT
    settings.SECURITY_TXT_CANONICAL = ""
    return settings


def test_served_as_plain_text_with_every_field(
    client: Client, configured: Settings
) -> None:
    response = client.get(URL)

    assert response.status_code == 200
    assert response["Content-Type"] == "text/plain; charset=utf-8"
    body = response.content.decode("utf-8")
    assert body.endswith("\n")
    fields = _fields(body)
    assert list(fields) == [
        "Contact",
        "Expires",
        "Policy",
        "Preferred-Languages",
        "Canonical",
    ]
    assert fields["Contact"] == CONTACT
    assert fields["Policy"] == POLICY
    assert fields["Preferred-Languages"] == "en"
    assert fields["Canonical"] == "http://testserver/.well-known/security.txt"


def test_expires_is_in_the_future_and_under_a_year(
    client: Client, configured: Settings
) -> None:
    fields = _fields(client.get(URL).content.decode("utf-8"))

    expires = datetime.strptime(fields["Expires"], "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=UTC
    )
    now = datetime.now(UTC)
    assert now < expires < now + timedelta(days=365)


def test_expires_counts_days_from_the_start_of_the_utc_day(
    rf: RequestFactory, configured: Settings
) -> None:
    configured.SECURITY_TXT_EXPIRES_DAYS = 180
    # 23:30 at UTC-5 on December 31 is 04:30 on January 1 in UTC.
    now = datetime(2026, 12, 31, 23, 30, tzinfo=timezone(timedelta(hours=-5)))

    fields = _fields(security_txt_content(rf.get(URL), now))

    assert fields["Expires"] == "2027-06-30T00:00:00Z"


def test_canonical_setting_wins_over_the_request(
    client: Client, configured: Settings
) -> None:
    configured.SECURITY_TXT_CANONICAL = (
        "https://api.example.org/.well-known/security.txt"
    )

    fields = _fields(client.get(URL).content.decode("utf-8"))

    assert fields["Canonical"] == "https://api.example.org/.well-known/security.txt"


def test_canonical_from_the_request_uses_its_scheme(
    client: Client, configured: Settings
) -> None:
    fields = _fields(client.get(URL, secure=True).content.decode("utf-8"))

    assert fields["Canonical"] == "https://testserver/.well-known/security.txt"


def test_head_is_allowed(client: Client, configured: Settings) -> None:
    response = client.head(URL)

    assert response.status_code == 200
    assert response["Content-Type"] == "text/plain; charset=utf-8"


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_writes_are_refused(client: Client, configured: Settings, method: str) -> None:
    response = getattr(client, method)(URL)

    assert response.status_code == 405


def test_not_in_the_openapi_document(client: Client) -> None:
    response = client.get("/api/v1/openapi.json")

    assert response.status_code == 200
    assert "security.txt" not in response.content.decode("utf-8")


def test_carries_the_security_headers(client: Client, configured: Settings) -> None:
    response = client.get(URL)

    assert response["X-Content-Type-Options"] == "nosniff"
    assert response["X-Frame-Options"] == "DENY"
