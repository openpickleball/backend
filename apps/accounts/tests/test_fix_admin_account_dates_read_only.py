"""An Account's join and last login times are read-only (issue #101).

The admin's split date-time fields drop microseconds, so a save that
changed nothing rewrote ``date_joined`` and recorded a false
``admin.change``. Made-up data only.
"""

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditAction, AuditEvent

pytestmark = pytest.mark.django_db


def test_the_dates_are_not_form_fields(client: Client) -> None:
    client.force_login(UserFactory.create(is_staff=True, is_superuser=True))
    account = UserFactory.create()

    response = client.get(reverse("admin:accounts_user_change", args=[account.pk]))

    form = response.context["adminform"].form
    assert "date_joined" not in form.fields
    assert "last_login" not in form.fields


def test_a_save_that_changes_nothing_keeps_the_dates_and_records_nothing(
    client: Client,
) -> None:
    client.force_login(UserFactory.create(is_staff=True, is_superuser=True))
    account = UserFactory.create()
    account.date_joined = account.date_joined.replace(microsecond=123456)
    account.last_login = timezone.now().replace(microsecond=654321)
    account.save()

    response = client.post(
        reverse("admin:accounts_user_change", args=[account.pk]),
        {
            "email": account.email,
            "is_active": "on",
            "date_joined_0": "2020-01-01",
            "date_joined_1": "00:00:00",
            "last_login_0": "",
            "last_login_1": "",
        },
    )

    assert response.status_code == 302
    stored = type(account)._default_manager.get(pk=account.pk)
    assert stored.date_joined == account.date_joined
    assert stored.last_login == account.last_login
    assert not AuditEvent.objects.filter(action=AuditAction.ADMIN_CHANGE).exists()
