"""Django on the API host and the PWA's host (issue #183, ADR 0063).

App Platform routes api.openpb.org, and app.openpb.org's /api/,
/_allauth/, and /oauth/, to the web component (.do/production.yaml).
These tests take the hosts and trusted origins from the spec, with the
production cookie names, and send https requests as the proxy forwards
them. The PWA's calls are same-origin, so they need no CORS and no
credentials. Made-up accounts only.
"""

from collections.abc import Iterator
from typing import Any

import pytest
from allauth.account.models import EmailAddress
from django.conf import settings
from django.test import Client, override_settings

from apps.accounts.tests.factories import UserFactory
from apps.accounts.tests.headless import BROWSER, PASSWORD, post
from config.tests.test_app_spec import API_HOST, APP_HOST, _envs, _spec

pytestmark = pytest.mark.django_db

READY = "/api/v1/health/ready"
CONFIG = f"{BROWSER}/config"
EMAIL = "pat.pwa@example.org"
APP_ORIGIN = f"https://{APP_HOST}"
# A made-up default App Platform host, which Django does not answer on.
DEFAULT_HOST = "open-backend-production-ab1cd.ondigitalocean.app"
HEALTH_PROBE = "config.health_probe.HealthProbeMiddleware"
SESSION_COOKIE = "__Host-sessionid"
CSRF_COOKIE = "__Host-csrftoken"


@pytest.fixture(autouse=True)
def spec_hosts() -> Iterator[None]:
    """The spec's hosts, no trusted origin, and the DEBUG-off cookie names."""
    envs = _envs(_spec())
    with override_settings(
        ALLOWED_HOSTS=envs["DJANGO_ALLOWED_HOSTS"]["value"].split(","),
        # The spec leaves it unset, so it is empty (ADR 0063).
        CSRF_TRUSTED_ORIGINS=[],
        SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO", "https"),
        SECURE_SSL_REDIRECT=True,
        SESSION_COOKIE_NAME=SESSION_COOKIE,
        CSRF_COOKIE_NAME=CSRF_COOKIE,
        SESSION_COOKIE_SECURE=True,
        CSRF_COOKIE_SECURE=True,
    ):
        yield


def _client(host: str) -> Client:
    """A browser on ``host``, over https as the proxy forwards it."""
    return Client(
        enforce_csrf_checks=True,
        HTTP_HOST=host,
        HTTP_X_FORWARDED_PROTO="https",
    )


@pytest.fixture
def account() -> Any:
    account = UserFactory.create(email=EMAIL, password=PASSWORD)
    EmailAddress.objects.create(user=account, email=EMAIL, primary=True, verified=True)
    return account


@pytest.mark.parametrize("host", [API_HOST, APP_HOST])
def test_readiness_answers_on_both_hosts(host: str) -> None:
    # Without HealthProbeMiddleware, which answers a refused host itself,
    # so the host must be in ALLOWED_HOSTS.
    middleware = [m for m in settings.MIDDLEWARE if m != HEALTH_PROBE]
    assert len(middleware) == len(settings.MIDDLEWARE) - 1
    with override_settings(MIDDLEWARE=middleware):
        response = _client(host).get(READY)

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


@pytest.mark.parametrize("host", [API_HOST, APP_HOST])
def test_the_browser_config_answers_same_origin_on_both_hosts(host: str) -> None:
    # Not a health path, so HealthProbeMiddleware does not answer it:
    # the host must be in ALLOWED_HOSTS.
    response = _client(host).get(CONFIG, headers={"Origin": f"https://{host}"})

    assert response.status_code == 200
    # Same-origin: no CORS header is needed or sent.
    assert "Access-Control-Allow-Origin" not in response.headers


def test_a_pwa_login_sets_a_host_only_session_cookie(account: Any) -> None:
    client = _client(APP_HOST)
    assert client.get(CONFIG).status_code == 200
    token = client.cookies[CSRF_COOKIE].value

    response = post(
        client,
        f"{BROWSER}/auth/login",
        {"email": EMAIL, "password": PASSWORD},
        Origin=APP_ORIGIN,
        **{"X-CSRFToken": token},
    )

    assert response.status_code == 200, response.content
    cookie = response.cookies[SESSION_COOKIE]
    assert cookie["domain"] == ""
    assert cookie["path"] == "/"
    assert cookie["secure"] is True
    assert cookie["httponly"] is True
    assert cookie["samesite"] == "Lax"


@pytest.mark.parametrize(
    ("host", "origin"),
    [
        (APP_HOST, "https://partner.openpb.org"),
        # Each host refuses the other's origin, so a flaw in the PWA's
        # scripts cannot pass the origin check on the admin's host.
        (APP_HOST, f"https://{API_HOST}"),
        (API_HOST, APP_ORIGIN),
    ],
)
def test_a_login_from_another_origin_is_refused(
    account: Any, host: str, origin: str
) -> None:
    client = _client(host)
    assert client.get(CONFIG).status_code == 200
    token = client.cookies[CSRF_COOKIE].value

    response = post(
        client,
        f"{BROWSER}/auth/login",
        {"email": EMAIL, "password": PASSWORD},
        Origin=origin,
        **{"X-CSRFToken": token},
    )

    assert response.status_code == 403
    assert SESSION_COOKIE not in response.cookies


def test_django_refuses_the_default_app_platform_host() -> None:
    # No ingress rule names this host, so App Platform should not send it
    # to Django at all; if it ever does, Django refuses it.
    response = _client(DEFAULT_HOST).get(CONFIG)

    assert response.status_code == 400


def test_probes_still_answer_on_the_default_host() -> None:
    # HealthProbeMiddleware answers App Platform's probes whatever the
    # host (ADR 0042), so leaving the default host out keeps deploys up.
    response = _client(DEFAULT_HOST).get(READY)

    assert response.status_code == 200
