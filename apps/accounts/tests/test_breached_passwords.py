"""The breached password check (#100, ADR 0047).

Every path that sets a password refuses one the range API lists with a
count above zero: signup, the signup password check (ADR 0039), password
change, and password reset from a key. Only the first five hex characters
of the SHA-1 leave OPEN. When the service fails, the password is accepted
and one warning is logged with the error class only. The service is the
``breach_service`` stand-in from the root conftest; no test reaches the
network. Made-up data only.
"""

import logging
from io import BytesIO
from typing import Any
from urllib.error import HTTPError, URLError

import pytest
from allauth.account.models import EmailAddress
from django.conf import settings
from django.contrib.auth.password_validation import (
    MinimumLengthValidator,
    get_default_password_validators,
)
from django.core.exceptions import ValidationError

from apps.accounts import breached
from apps.accounts.breached import BreachedPasswordValidator, is_breached
from apps.accounts.tests.breach import PADDING_LINE, FakeBreachService, sha1_hex
from apps.accounts.tests.factories import UserFactory
from apps.accounts.tests.headless import (
    APP,
    BROWSER,
    PASSWORD,
    RESET_PREFIX,
    HeadlessSession,
    key_from_link,
    link_in_last_mail,
)

pytestmark = pytest.mark.django_db

BREACHED = "made-up-breached-Passw0rd"
EMAIL = "breach.player@example.org"


def _codes(response: Any) -> list[str]:
    return [error.get("code") for error in response.json().get("errors", [])]


@pytest.fixture
def account() -> Any:
    account = UserFactory.create(email=EMAIL, password=PASSWORD)
    EmailAddress.objects.create(user=account, email=EMAIL, primary=True, verified=True)
    return account


def test_a_listed_password_is_breached(breach_service: FakeBreachService) -> None:
    breach_service.add(BREACHED)

    assert is_breached(BREACHED)
    assert breach_service.prefixes == [sha1_hex(BREACHED)[:5]]


def test_an_unlisted_password_is_not_breached() -> None:
    assert not is_breached(PASSWORD)


def test_a_padding_entry_never_refuses(breach_service: FakeBreachService) -> None:
    suffix = sha1_hex(PASSWORD)[5:]
    breach_service.body = f"{PADDING_LINE}\r\n{suffix}:0"

    assert not is_breached(PASSWORD)


def test_the_validator_refuses_with_a_clear_message(
    breach_service: FakeBreachService,
) -> None:
    breach_service.add(BREACHED)

    with pytest.raises(ValidationError) as caught:
        BreachedPasswordValidator().validate(BREACHED)

    assert caught.value.code == "password_breached"
    assert "data breach" in caught.value.messages[0]
    assert BREACHED not in caught.value.messages[0]


def test_the_validators_are_djangos_four_then_the_breach_check() -> None:
    validators = get_default_password_validators()

    # After the text check that keeps an unencodable password from the hasher.
    assert [type(v).__name__ for v in validators] == [
        "EncodablePasswordValidator",
        "UserAttributeSimilarityValidator",
        "MinimumLengthValidator",
        "CommonPasswordValidator",
        "NumericPasswordValidator",
        "BreachedPasswordValidator",
    ]
    minimum = validators[2]
    assert isinstance(minimum, MinimumLengthValidator)
    assert minimum.min_length == settings.PASSWORD_MIN_LENGTH >= 8


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("timed out"),
        URLError("unreachable"),
        ConnectionRefusedError("refused"),
        HTTPError("https://example.invalid", 503, "down", None, None),  # type: ignore[arg-type]
        UnicodeDecodeError("ascii", b"\xff", 0, 1, "bad"),
    ],
)
def test_an_outage_accepts_and_logs_only_the_error_class(
    breach_service: FakeBreachService,
    caplog: pytest.LogCaptureFixture,
    error: Exception,
) -> None:
    breach_service.add(BREACHED)
    breach_service.error = error

    with caplog.at_level(logging.WARNING, logger="apps.accounts.breached"):
        assert not is_breached(BREACHED)

    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.levelno == logging.WARNING
    text = record.getMessage()
    assert type(error).__name__ in text
    digest = sha1_hex(BREACHED)
    for secret in (BREACHED, digest, digest[:5], digest[5:], EMAIL, "unreachable"):
        assert secret not in text


@pytest.mark.parametrize(
    "body",
    ["not a range answer", "ABC", f"{'1' * 35}:many", f"{'1' * 35}:-1", "\x00"],
)
def test_a_malformed_answer_accepts_and_logs(
    breach_service: FakeBreachService,
    caplog: pytest.LogCaptureFixture,
    body: str,
) -> None:
    breach_service.body = body

    with caplog.at_level(logging.WARNING, logger="apps.accounts.breached"):
        assert not is_breached(BREACHED)

    assert len(caplog.records) == 1
    assert BREACHED not in caplog.records[0].getMessage()


class _Answer(BytesIO):
    status = 200
    # What http.client leaves when the whole Content-Length was read.
    length: int | None = 0


def test_the_request_sends_only_the_prefix_with_padding_and_the_timeout(
    breach_service: FakeBreachService, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: dict[str, Any] = {}

    def urlopen(request: Any, timeout: float) -> _Answer:
        sent["url"] = request.full_url
        sent["headers"] = {k.lower(): v for k, v in request.header_items()}
        sent["timeout"] = timeout
        sent["data"] = request.data
        return _Answer(f"{PADDING_LINE}\r\n".encode())

    monkeypatch.setattr(breached, "urlopen", urlopen)

    body = breach_service.original("ABCDE")

    assert body.startswith(PADDING_LINE)
    assert sent["url"] == f"{settings.PASSWORD_BREACH_API_URL}ABCDE"
    assert sent["headers"]["add-padding"] == "true"
    assert sent["headers"]["user-agent"] == breached.USER_AGENT
    assert "open" in breached.USER_AGENT.lower()
    assert sent["timeout"] == settings.PASSWORD_BREACH_TIMEOUT_SECONDS == 3
    assert sent["data"] is None


def test_a_non_200_answer_is_an_outage(
    breach_service: FakeBreachService, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Odd(_Answer):
        status = 204

    monkeypatch.setattr(breached, "urlopen", lambda request, timeout: Odd(b""))

    with pytest.raises(breached.BreachServiceError):
        breach_service.original("ABCDE")


def test_only_the_five_character_prefix_is_ever_sent(
    breach_service: FakeBreachService,
) -> None:
    is_breached(BREACHED)

    assert breach_service.prefixes == [sha1_hex(BREACHED)[:5]]
    assert len(breach_service.prefixes[0]) == 5


@pytest.mark.parametrize("base", [APP, BROWSER])
def test_signup_refuses_a_breached_password(
    base: str, breach_service: FakeBreachService
) -> None:
    breach_service.add(BREACHED)
    session = HeadlessSession(base)

    response = session.signup(email="new.breach@example.org", password=BREACHED)

    assert response.status_code == 400
    assert "password_breached" in _codes(response)


@pytest.mark.parametrize("base", [APP, BROWSER])
def test_the_signup_password_check_refuses_a_breached_password(
    base: str, breach_service: FakeBreachService
) -> None:
    breach_service.add(BREACHED)

    response = HeadlessSession(base).password_check(password=BREACHED)

    assert response.status_code == 400
    assert "password_breached" in _codes(response)


@pytest.mark.parametrize("base", [APP, BROWSER])
def test_password_change_refuses_a_breached_password(
    base: str, breach_service: FakeBreachService, account: Any
) -> None:
    breach_service.add(BREACHED)
    session = HeadlessSession(base)
    assert session.login(EMAIL).status_code == 200

    response = session.post(
        "account/password/change",
        {"current_password": PASSWORD, "new_password": BREACHED},
    )

    assert response.status_code == 400
    assert "password_breached" in _codes(response)
    account.refresh_from_db()
    assert account.check_password(PASSWORD)


@pytest.mark.parametrize("base", [APP, BROWSER])
def test_password_reset_refuses_a_breached_password(
    base: str, breach_service: FakeBreachService, account: Any
) -> None:
    breach_service.add(BREACHED)
    session = HeadlessSession(base)
    assert session.post("auth/password/request", {"email": EMAIL}).status_code == 200
    key = key_from_link(link_in_last_mail(RESET_PREFIX), RESET_PREFIX)

    response = session.post("auth/password/reset", {"key": key, "password": BREACHED})

    assert response.status_code == 400
    assert "password_breached" in _codes(response)
    account.refresh_from_db()
    assert account.check_password(PASSWORD)


@pytest.mark.parametrize("base", [APP, BROWSER])
def test_an_outage_still_lets_signup_through(
    base: str, breach_service: FakeBreachService
) -> None:
    breach_service.add(BREACHED)
    breach_service.error = TimeoutError("timed out")

    response = HeadlessSession(base).signup(
        email="outage.player@example.org", password=BREACHED
    )

    # Signup goes on to the email verification step.
    assert response.status_code == 401


def test_a_password_that_is_not_text_is_never_sent(
    breach_service: FakeBreachService,
) -> None:
    # EncodablePasswordValidator refuses it; the range API is not asked.
    assert not is_breached("\ud800abcdefgh-XYZ-123")
    assert breach_service.prefixes == []
