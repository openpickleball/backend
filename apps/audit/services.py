"""Record audit events.

``record_event`` is the only supported way to write an AuditEvent. Call it
inside the same ``transaction.atomic()`` block as the change it records, so
the change and its audit event commit together or not at all.
"""

import re
from collections.abc import Iterable, Mapping
from typing import Any

from django.contrib.auth.base_user import AbstractBaseUser
from django.contrib.auth.models import AnonymousUser
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import FieldDoesNotExist
from django.db import models

from apps.audit.models import AuditAction, AuditEvent

REDACTED = "[redacted]"

# A key whose lowercased name contains any of these has its value replaced.
_SENSITIVE_KEY = re.compile(
    r"password|passwd|secret|token|recovery|otp|api_key|private_key|session_key"
)


def _redact(value: Any) -> Any:
    """Return a copy of ``value`` with every sensitive key's value redacted.

    Dicts recurse, and lists and tuples recurse element by element, so a
    secret at any depth is caught. The input is never changed.
    """
    if isinstance(value, Mapping):
        return {
            key: REDACTED if _SENSITIVE_KEY.search(str(key).lower()) else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [_redact(item) for item in value]
    return value


def snapshot(instance: models.Model, fields: Iterable[str]) -> dict[str, Any]:
    """Return the named fields' values, for ``before`` or ``after``.

    The field list is required, so a field such as a password hash is never
    captured by accident. Every value is JSON-safe for ``record_event``:

    - a foreign key gives its primary key value;
    - a many-to-many field gives the sorted list of related primary keys;
    - a file field gives its stored name (the path in storage), not the file;
    - a binary field raises ``ValueError``: raw bytes do not belong in an
      audit event.

    An unknown name raises ``FieldDoesNotExist``.
    """
    values: dict[str, Any] = {}
    for name in fields:
        field = instance._meta.get_field(name)
        if not isinstance(field, models.Field):
            raise FieldDoesNotExist(f"{name} is not a concrete field.")
        if isinstance(field, models.BinaryField):
            raise ValueError(f"{name} is a binary field and cannot be snapshotted.")
        value = field.value_from_object(instance)
        if isinstance(field, models.ManyToManyField):
            value = sorted(related.pk for related in value)
        elif isinstance(field, models.FileField):
            value = value.name or ""
        values[name] = value
    return values


def _is_saved(instance: models.Model | AbstractBaseUser) -> bool:
    """True when ``instance`` is a row in the database.

    A primary key alone is not enough: a UUID primary key default is set
    before the instance is saved.
    """
    return not instance._state.adding and instance.pk is not None


def record_event(
    *,
    action: AuditAction,
    target: models.Model,
    actor: AbstractBaseUser | AnonymousUser | None = None,
    before: Mapping[str, Any] | None = None,
    after: Mapping[str, Any] | None = None,
    reason: str = "",
) -> AuditEvent:
    """Store one AuditEvent and return it.

    ``action`` is an AuditAction member or its value. ``target`` and an
    Account ``actor`` must be saved: loaded from or written to the database,
    with a primary key. To record a delete, call this before ``delete()``,
    which clears the instance's primary key. An anonymous or missing actor
    is stored as no actor. Sensitive keys in ``before`` and ``after`` are
    redacted (see ``_redact``).
    """
    if not isinstance(action, str) or action not in AuditAction.values:
        raise ValueError("The action is not an AuditAction.")
    if not _is_saved(target):
        raise ValueError("The target must be saved before it is audited.")

    actor_id: Any = None
    if actor is not None and actor.is_authenticated:
        if not _is_saved(actor):
            raise ValueError("The actor must be saved before it is audited.")
        actor_id = actor.pk

    return AuditEvent.objects.create(
        action=AuditAction(action),
        actor_user_id=actor_id,
        target_content_type=ContentType.objects.get_for_model(target),
        target_object_id=str(target.pk),
        before=None if before is None else _redact(before),
        after=None if after is None else _redact(after),
        reason=reason,
    )
