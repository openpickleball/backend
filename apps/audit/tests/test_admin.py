"""The admin shows audit events read-only and refuses every change."""

from typing import TYPE_CHECKING

import pytest
from django.contrib import admin
from django.contrib.auth.models import Permission
from django.test import Client
from django.urls import reverse

from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditAction, AuditEvent
from apps.audit.services import record_event

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

pytestmark = pytest.mark.django_db

CHANGELIST = "admin:audit_auditevent_changelist"
RAW_SECRET = "made-up-raw-secret-in-admin"


@pytest.fixture
def event() -> AuditEvent:
    target = UserFactory.create()
    return record_event(
        action=AuditAction.MFA_CHANGE,
        target=target,
        actor=target,
        after={"device": {"totp_secret": RAW_SECRET}, "label": "made-up phone"},
        reason="Made-up original reason.",
    )


@pytest.fixture
def superuser_client(client: Client) -> Client:
    client.force_login(UserFactory.create(is_staff=True, is_superuser=True))
    return client


@pytest.fixture
def viewer_client(client: Client) -> Client:
    viewer: _User = UserFactory.create(is_staff=True)
    viewer.user_permissions.add(
        Permission.objects.get(
            content_type__app_label="audit", codename="view_auditevent"
        )
    )
    client.force_login(viewer)
    return client


@pytest.fixture
def staff_client(client: Client) -> Client:
    client.force_login(UserFactory.create(is_staff=True))
    return client


@pytest.fixture(params=["superuser_client", "viewer_client"])
def reader_client(request: pytest.FixtureRequest) -> Client:
    client: Client = request.getfixturevalue(request.param)
    return client


def _change_url(event: AuditEvent) -> str:
    return reverse("admin:audit_auditevent_change", args=[event.pk])


def _assert_unchanged(event: AuditEvent) -> None:
    assert AuditEvent.objects.count() == 1
    assert AuditEvent.objects.get(pk=event.pk).reason == "Made-up original reason."


def test_audit_event_is_registered() -> None:
    assert admin.site.is_registered(AuditEvent)


def test_only_a_view_permission_exists() -> None:
    codenames = set(
        Permission.objects.filter(content_type__app_label="audit").values_list(
            "codename", flat=True
        )
    )

    assert codenames == {"view_auditevent"}


def test_changelist_renders_for_a_reader(
    reader_client: Client, event: AuditEvent
) -> None:
    response = reader_client.get(reverse(CHANGELIST))

    assert response.status_code == 200
    assert _change_url(event) in response.content.decode()


def test_changelist_offers_no_actions(reader_client: Client, event: AuditEvent) -> None:
    response = reader_client.get(reverse(CHANGELIST))

    assert response.context["action_form"] is None
    assert "delete_selected" not in response.content.decode()


def test_change_view_is_read_only_for_a_reader(
    reader_client: Client, event: AuditEvent
) -> None:
    response = reader_client.get(_change_url(event))

    assert response.status_code == 200
    content = response.content.decode()
    assert 'name="_save"' not in content
    assert 'name="_continue"' not in content
    assert reverse("admin:audit_auditevent_delete", args=[event.pk]) not in content
    assert "Made-up original reason." in content


def test_change_view_shows_no_redacted_secret(
    superuser_client: Client, event: AuditEvent
) -> None:
    content = superuser_client.get(_change_url(event)).content.decode()

    assert RAW_SECRET not in content
    assert "[redacted]" in content


def test_add_view_is_refused_for_a_superuser(superuser_client: Client) -> None:
    response = superuser_client.get(reverse("admin:audit_auditevent_add"))

    assert response.status_code == 403


def test_add_post_is_refused_for_a_superuser(
    superuser_client: Client, event: AuditEvent
) -> None:
    response = superuser_client.post(
        reverse("admin:audit_auditevent_add"),
        {"action": AuditAction.ADMIN_ADD, "reason": "Forged."},
    )

    assert response.status_code == 403
    _assert_unchanged(event)


def test_delete_view_is_refused_for_a_superuser(
    superuser_client: Client, event: AuditEvent
) -> None:
    url = reverse("admin:audit_auditevent_delete", args=[event.pk])

    assert superuser_client.get(url).status_code == 403
    assert superuser_client.post(url, {"post": "yes"}).status_code == 403
    _assert_unchanged(event)


def test_change_post_is_refused_for_a_superuser(
    superuser_client: Client, event: AuditEvent
) -> None:
    response = superuser_client.post(
        _change_url(event), {"reason": "Changed.", "_save": "Save"}
    )

    assert response.status_code == 403
    _assert_unchanged(event)


def test_bulk_delete_action_is_refused_for_a_superuser(
    superuser_client: Client, event: AuditEvent
) -> None:
    superuser_client.post(
        reverse(CHANGELIST),
        {"action": "delete_selected", "_selected_action": [event.pk], "post": "yes"},
    )

    _assert_unchanged(event)


def test_staff_without_the_view_permission_is_refused(
    staff_client: Client, event: AuditEvent
) -> None:
    assert staff_client.get(reverse(CHANGELIST)).status_code == 403
    assert staff_client.get(_change_url(event)).status_code == 403


def test_anonymous_visitor_is_sent_to_login(client: Client, event: AuditEvent) -> None:
    response = client.get(reverse(CHANGELIST))

    assert response.status_code == 302
    assert reverse("admin:login") in response["Location"]
