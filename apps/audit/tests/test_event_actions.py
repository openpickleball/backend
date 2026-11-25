"""The Audit Event actions an Event's create, change, and delete record (#163)."""

from apps.audit.models import AuditAction


def test_event_actions_exist() -> None:
    assert AuditAction["EVENT_CREATE"].value == "event.create"
    assert AuditAction["EVENT_CHANGE"].value == "event.change"
    assert AuditAction["EVENT_DELETE"].value == "event.delete"
