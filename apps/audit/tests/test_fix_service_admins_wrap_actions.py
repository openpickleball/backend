"""Custom actions are audited on every admin (issue #101).

An admin whose saves and deletes go through an audited service (the Event
admin, the grant admins) skips only the generic add, change, and delete
events. Its custom actions, like every other admin's, record one
``admin.action`` per selected object unless marked
``@audited_by_service``. Made-up data only.
"""

from typing import TYPE_CHECKING, Any

import pytest
from django.contrib import admin
from django.db.models import QuerySet
from django.http import HttpRequest

from apps.accounts.tests.factories import UserFactory
from apps.audit.admin_audit import AdminAuditMixin, audited_admin_class
from apps.audit.models import AuditAction, AuditEvent
from apps.events.admin import EventAdmin
from apps.events.models import Event
from apps.events.tests.factories import EventFactory

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

pytestmark = pytest.mark.django_db


@pytest.fixture
def superuser() -> "_User":
    account: _User = UserFactory.create(is_staff=True, is_superuser=True)
    return account


def test_every_registered_admin_has_the_mixin() -> None:
    for model, model_admin in admin.site._registry.items():
        assert isinstance(model_admin, AdminAuditMixin), model


def test_every_unmarked_custom_action_is_wrapped(rf: Any, superuser: "_User") -> None:
    request = rf.get("/")
    request.user = superuser

    for model, model_admin in admin.site._registry.items():
        for name, entry in model_admin.get_actions(request).items():
            assert entry is not None
            func = entry[0]
            if name == "delete_selected" or getattr(func, "audited_by_service", False):
                continue
            assert getattr(func, "records_admin_action", False), (model, name)


class _ServiceAdminWithAction(EventAdmin):
    actions = ("made_up_action",)

    @admin.action(description="A made-up action")
    def made_up_action(self, request: HttpRequest, queryset: QuerySet[Event]) -> None:
        return None


def test_a_service_audited_admin_still_audits_its_custom_actions(
    rf: Any, superuser: "_User"
) -> None:
    event = EventFactory.create()
    model_admin = audited_admin_class(_ServiceAdminWithAction)(Event, admin.site)
    request = rf.post("/")
    request.user = superuser

    entry = model_admin.get_actions(request)["made_up_action"]
    assert entry is not None
    entry[0](model_admin, request, Event.objects.filter(pk=event.pk))

    [recorded] = AuditEvent.objects.filter(action=AuditAction.ADMIN_ACTION)
    assert recorded.target == event
    assert recorded.after == {"action": "made_up_action"}
    assert recorded.actor_user_id == superuser.pk
