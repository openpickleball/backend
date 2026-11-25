"""The current actor for Audit Events that model hooks record (#158)."""

import pytest
from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.http import HttpRequest, HttpResponse

from apps.accounts.tests.factories import UserFactory
from apps.audit.actor import acting_as, current_actor
from apps.audit.middleware import AuditActorMiddleware

MIDDLEWARE = "apps.audit.middleware.AuditActorMiddleware"


def test_no_actor_by_default() -> None:
    assert current_actor() is None


def test_an_anonymous_user_is_no_actor() -> None:
    with acting_as(AnonymousUser()):
        assert current_actor() is None


@pytest.mark.django_db
def test_acting_as_sets_and_restores_the_actor() -> None:
    outer = UserFactory.create()
    inner = UserFactory.create()

    with acting_as(outer):
        with acting_as(inner):
            assert current_actor() == inner
        assert current_actor() == outer
    assert current_actor() is None


@pytest.mark.django_db
def test_the_middleware_holds_the_request_user_only_during_the_request() -> None:
    account = UserFactory.create()
    seen = []

    def view(request: HttpRequest) -> HttpResponse:
        seen.append(current_actor())
        return HttpResponse()

    request = HttpRequest()
    request.user = account
    AuditActorMiddleware(view)(request)

    assert seen == [account]
    assert current_actor() is None


def test_the_middleware_restores_the_actor_when_the_view_raises() -> None:
    def view(request: HttpRequest) -> HttpResponse:
        raise RuntimeError("Made-up failure.")

    with pytest.raises(RuntimeError):
        AuditActorMiddleware(view)(HttpRequest())

    assert current_actor() is None


def test_the_middleware_runs_after_authentication() -> None:
    middleware = list(settings.MIDDLEWARE)

    assert middleware.index(MIDDLEWARE) > middleware.index(
        "django.contrib.auth.middleware.AuthenticationMiddleware"
    )
