"""App Platform's health probes reach the health checks (issue #61).

DigitalOcean App Platform sends its HTTP health probes straight to the
container, over plain http, with a Host header that ALLOWED_HOSTS
refuses (empty, or the instance IP). Django answers such a request with
400 (DisallowedHost), and with SECURE_SSL_REDIRECT on it redirects a
plain http request to https. Either way the readiness check fails and
the deploy never goes live.
"""

import pytest
from django.test import Client, override_settings

READY_URL = "/api/v1/health/ready"

# A made-up instance address, like the ones App Platform probes from.
INSTANCE_HOST = "10.244.0.5"
ALLOWED_HOST = "api.example.org"


@pytest.mark.django_db
def test_readiness_probe_with_the_instance_ip_as_host_gets_200() -> None:
    with override_settings(ALLOWED_HOSTS=[ALLOWED_HOST]):
        response = Client().get(READY_URL, HTTP_HOST=INSTANCE_HOST)

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


@pytest.mark.django_db
def test_readiness_probe_over_plain_http_is_not_redirected() -> None:
    with override_settings(ALLOWED_HOSTS=[ALLOWED_HOST], SECURE_SSL_REDIRECT=True):
        response = Client().get(READY_URL, HTTP_HOST=ALLOWED_HOST)

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
