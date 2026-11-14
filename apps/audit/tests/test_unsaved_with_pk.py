"""record_event refuses an unsaved target or actor that already has a pk.

Review round 1: the check was ``pk is None``, so an unsaved instance with a
pk (a UUID primary key default sets one before saving) passed as saved.
An unsaved Account given an explicit pk stands in for that case.
"""

import pytest

from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditAction, AuditEvent
from apps.audit.services import record_event

pytestmark = pytest.mark.django_db

UNUSED_PK = 987654


def test_unsaved_target_with_a_pk_is_refused() -> None:
    unsaved = UserFactory.build()
    unsaved.pk = UNUSED_PK

    with pytest.raises(ValueError, match="target"):
        record_event(action=AuditAction.ADMIN_ADD, target=unsaved)

    assert not AuditEvent.objects.exists()


def test_unsaved_actor_with_a_pk_is_refused() -> None:
    target = UserFactory.create()
    unsaved = UserFactory.build()
    unsaved.pk = UNUSED_PK

    with pytest.raises(ValueError, match="actor"):
        record_event(action=AuditAction.ADMIN_ADD, target=target, actor=unsaved)

    assert not AuditEvent.objects.exists()
