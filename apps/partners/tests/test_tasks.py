"""The daily clean-up of expired OAuth tokens (#52). Made-up data only.

``apps.partners.tasks.clear_expired_tokens`` runs django-oauth-toolkit's
``clear_expired()`` (what ``cleartokens`` runs) on the ``default``
queue. Expired access tokens and grants are deleted; unexpired ones stay.
Revoked refresh tokens stay too: ``REFRESH_TOKEN_REUSE_PROTECTION`` keeps
them until they expire, and refresh tokens do not expire on their own
(``REFRESH_TOKEN_EXPIRE_SECONDS`` is unset).
"""

from datetime import timedelta

import pytest
from celery.schedules import crontab
from django.conf import settings
from django.utils import timezone
from oauth2_provider.models import (
    get_access_token_model,
    get_grant_model,
    get_refresh_token_model,
)

from apps.accounts.tests.factories import UserFactory
from apps.partners import tasks
from apps.partners.models import PartnerApplication
from apps.partners.tests.factories import PartnerApplicationFactory
from config.celery import app as celery_app

TASK_NAME = "apps.partners.tasks.clear_expired_tokens"

AccessToken = get_access_token_model()
Grant = get_grant_model()
RefreshToken = get_refresh_token_model()


def _application() -> PartnerApplication:
    application: PartnerApplication = PartnerApplicationFactory.create()
    return application


def _access_token(application: PartnerApplication, *, expires_in: timedelta) -> int:
    token = AccessToken.objects.create(
        user=UserFactory.create(),
        application=application,
        token=f"made-up-access-{expires_in.total_seconds()}",
        expires=timezone.now() + expires_in,
        scope="read",
    )
    return int(token.pk)


def _grant(application: PartnerApplication, *, expires_in: timedelta) -> int:
    grant = Grant.objects.create(
        user=UserFactory.create(),
        application=application,
        code=f"made-up-code-{expires_in.total_seconds()}",
        expires=timezone.now() + expires_in,
        redirect_uri="https://example.org/callback",
        scope="read",
    )
    return int(grant.pk)


def test_the_task_has_its_explicit_name() -> None:
    assert tasks.clear_expired_tokens.name == TASK_NAME


def test_the_task_routes_to_the_default_queue() -> None:
    route = celery_app.amqp.router.route({}, TASK_NAME)
    assert route["queue"].name == "default"


def test_the_task_limits_stay_under_the_visibility_timeout() -> None:
    assert tasks.clear_expired_tokens.soft_time_limit == 540
    assert tasks.clear_expired_tokens.time_limit == 600


def test_the_beat_entry_runs_the_task_daily() -> None:
    entry = settings.CELERY_BEAT_SCHEDULE["clear-expired-tokens"]
    assert entry["task"] == TASK_NAME
    assert entry["schedule"] == crontab(hour=4, minute=20)
    assert entry["options"] == {"expire_seconds": 3600}


@pytest.mark.django_db
def test_expired_access_tokens_and_grants_are_deleted() -> None:
    application = _application()
    expired_token = _access_token(application, expires_in=timedelta(hours=-1))
    live_token = _access_token(application, expires_in=timedelta(hours=1))
    expired_grant = _grant(application, expires_in=timedelta(minutes=-1))
    live_grant = _grant(application, expires_in=timedelta(minutes=5))

    assert tasks.clear_expired_tokens.delay().get() is None

    tokens = set(AccessToken.objects.values_list("pk", flat=True))
    assert expired_token not in tokens
    assert live_token in tokens
    grants = set(Grant.objects.values_list("pk", flat=True))
    assert expired_grant not in grants
    assert live_grant in grants


@pytest.mark.django_db
def test_a_revoked_refresh_token_stays_for_reuse_detection() -> None:
    application = _application()
    account = UserFactory.create()
    refresh = RefreshToken.objects.create(
        user=account,
        application=application,
        token="made-up-refresh",
        revoked=timezone.now() - timedelta(days=30),
    )

    tasks.clear_expired_tokens.delay().get()

    assert RefreshToken.objects.filter(pk=refresh.pk).exists()


class MadeUpError(Exception):
    pass


def test_a_failure_raises_fixed_text_only(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    secret = "made-up secret detail"

    def fail() -> None:
        raise MadeUpError(secret)

    monkeypatch.setattr(tasks, "clear_expired", fail)

    with (
        caplog.at_level("ERROR", logger="apps.partners"),
        pytest.raises(tasks.ClearTokensFailed) as caught,
    ):
        tasks.clear_expired_tokens.run()

    assert secret not in str(caught.value)
    assert "MadeUpError" in str(caught.value)
    assert caught.value.__suppress_context__ is True
    for record in caplog.records:
        assert secret not in record.getMessage()
        assert record.exc_info is None
