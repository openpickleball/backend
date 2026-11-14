"""Record audit events.

``record_event`` is the only supported way to write an AuditEvent. Call it
inside the same ``transaction.atomic()`` block as the change it records, so
the change and its audit event commit together or not at all.
"""

import copy
import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

from django.contrib.auth.base_user import AbstractBaseUser
from django.contrib.auth.models import AnonymousUser
from django.contrib.contenttypes.models import ContentType
from django.db import models

from apps.audit.models import AuditAction, AuditEvent

REDACTED = "[redacted]"

# A key is lowercased and stripped of every non-alphanumeric character
# (so apiKey, api-key, and api_key all become apikey). If the result contains
# any of these, its value is replaced. "seed" is left out on purpose: Seed is
# an OPEN rating term (CONTEXT.md).
_SENSITIVE_KEY = re.compile(
    r"password|passwd|secret|token|recovery|otp|apikey|privatekey|sessionkey"
    r"|sessiondata"
)
_NON_ALPHANUMERIC = re.compile(r"[^a-z0-9]")


def _is_sensitive(key: object) -> bool:
    return bool(_SENSITIVE_KEY.search(_NON_ALPHANUMERIC.sub("", str(key).lower())))


def _redact(value: Any) -> Any:
    """Return a copy of ``value`` with every sensitive key's value redacted.

    Dicts recurse, and lists and tuples recurse element by element, so a
    secret at any depth is caught. The input is never changed.
    """
    if isinstance(value, Mapping):
        return {
            key: REDACTED if _is_sensitive(key) else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [_redact(item) for item in value]
    return value


def snapshot(instance: models.Model, fields: Iterable[str]) -> dict[str, Any]:
    """Return the named fields' values, for ``before`` or ``after``.

    The field list is required, so a field such as a password hash is never
    captured by accident. Only field kinds with a JSON-safe value for
    ``record_event`` are accepted:

    - a many-to-many field gives the sorted list of related primary keys;
    - a foreign key (or one-to-one) gives the value it stores: the related
      primary key, or the ``to_field`` value when ``to_field`` is set;
    - a file field gives its stored name (the path in storage), not the file;
    - any other concrete, non-relational field gives a deep copy of its
      value, so changing the instance afterwards cannot change the snapshot.

    A binary field (raw bytes do not belong in an audit event) and every
    other kind (reverse relations, generic relations and generic foreign
    keys, other foreign objects) raise ``ValueError`` naming the field. So
    does an accepted field whose value the AuditEvent JSON encoder cannot
    encode (a range, an array of bytes). An unknown name raises
    ``FieldDoesNotExist``.
    """
    values: dict[str, Any] = {}
    for name in fields:
        field = instance._meta.get_field(name)
        if isinstance(field, models.ManyToManyField):
            related = field.value_from_object(instance)
            value: Any = sorted(item.pk for item in related)
        elif isinstance(field, models.ForeignKey):
            value = field.value_from_object(instance)
        elif (
            isinstance(field, models.Field)
            and field.concrete
            and not field.is_relation
            and not isinstance(field, models.BinaryField)
        ):
            value = field.value_from_object(instance)
            if isinstance(field, models.FileField):
                value = value.name or ""
        else:
            raise ValueError(
                f"{name} cannot be snapshotted: its kind is not supported."
            )
        # Check the live value first: a value that cannot be encoded may not
        # be copyable either, and a copy error can carry the value's state.
        _require_json_encodable(name, value)
        # A copy: a JSONField or ArrayField value is the instance's own dict
        # or list, and an in-place change after the snapshot would otherwise
        # change the snapshot too.
        values[name] = copy.deepcopy(value)
    return values


def _audit_json_encoder() -> type[json.JSONEncoder] | None:
    """The encoder AuditEvent's before and after fields store with.

    Read from the model, so this check and the stored row cannot drift.
    """
    field = AuditEvent._meta.get_field("before")
    assert isinstance(field, models.JSONField)
    return field.encoder


def _require_json_encodable(name: str, value: Any) -> None:
    """Refuse a value the AuditEvent JSON fields could not store.

    Raised ``from None``: the encoder's own message can include the value.
    """
    try:
        json.dumps(value, cls=_audit_json_encoder())
    except (TypeError, ValueError):
        raise ValueError(
            f"{name} cannot be snapshotted: its value cannot be stored as JSON."
        ) from None


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

    This is the only supported way to write an AuditEvent. Call it inside
    the same ``transaction.atomic()`` block as the change it records, so the
    change and its audit event commit together or not at all.

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
