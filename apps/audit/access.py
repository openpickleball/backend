"""Audit Events for rows that grant access or consent (#158).

Organizer Members, Club Roles, Club Organizer Grants, and Organizer
Application Grants decide who may submit counted matches (CLAUDE.md, hard
rule 8). A model that inherits ``AuditedAccessModel`` and is decorated with
``audited_access`` records one Audit Event for each add, change, and
removal, in the same transaction as the change:

- ``save()`` locks and reads the stored row, saves, reads it again, and
  records an add (no stored row) or a change (the values differ). A save
  that changes nothing records nothing. A save that raises records
  nothing, since the event is written in the same atomic block.
- A delete records the removal from the stored row before the row goes.
  Django sends ``pre_delete`` inside the delete's transaction for every
  ORM delete: ``delete()``, ``QuerySet.delete()``, and a cascade from a
  deleted parent.

The actor is ``apps.audit.actor.current_actor()``: the request's Account
in a request, or None. ``before`` and ``after`` come from the model's
``audit_values()``, which holds roles, OPEN Codes, Account row ids, and
client ids, never an email or an Organizer's name.

Raw writes skip all of this: ``bulk_create``, ``QuerySet.update``,
``bulk_update``, raw SQL, and ``loaddata``.
"""

from typing import Any, ClassVar

from django.db import models, transaction
from django.db.models.signals import pre_delete

from apps.audit.actor import current_actor
from apps.audit.models import AuditAction


class AuditedAccessModel(models.Model):
    """A row whose adds, changes, and removals each record an Audit Event."""

    AUDIT_ADD: ClassVar[AuditAction]
    AUDIT_CHANGE: ClassVar[AuditAction]
    AUDIT_REMOVE: ClassVar[AuditAction]
    # Passed to select_related when the stored row is read.
    AUDIT_RELATED: ClassVar[tuple[str, ...]] = ()

    class Meta:
        abstract = True

    def save(self, *args: Any, **kwargs: Any) -> None:
        with transaction.atomic():
            stored = _stored(self, lock=True)
            before = None if stored is None else stored.audit_values()
            super().save(*args, **kwargs)
            saved = _stored(self, lock=False)
            assert saved is not None
            after = saved.audit_values()
            if before is None:
                _record(self, self.AUDIT_ADD, before=None, after=after)
            elif before != after:
                _record(self, self.AUDIT_CHANGE, before=before, after=after)

    def audit_values(self) -> dict[str, Any]:
        """The values ``before`` and ``after`` hold. Never an identifying
        personal data field's value (ADR 0009).
        """
        raise NotImplementedError


def _stored[M: AuditedAccessModel](instance: M, *, lock: bool) -> M | None:
    """The stored row for ``instance``'s primary key, or None."""
    if instance.pk is None:
        return None
    rows = type(instance)._base_manager.select_related(*instance.AUDIT_RELATED)
    if lock:
        rows = rows.select_for_update(of=("self",))
    stored: M | None = rows.filter(pk=instance.pk).first()
    return stored


def _record(
    target: AuditedAccessModel,
    action: AuditAction,
    *,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> None:
    # Imported here: apps.audit.services imports django-oauth-toolkit's
    # models, which must not load while the models that use this module do.
    from apps.audit.services import record_event

    record_event(
        action=action,
        target=target,
        actor=current_actor(),
        before=before,
        after=after,
    )


def _record_removal(
    sender: type[AuditedAccessModel], instance: AuditedAccessModel, **kwargs: Any
) -> None:
    """Record a removal before the row is deleted.

    Runs inside the delete's transaction, so the event and the delete
    commit together. A row already gone records nothing.
    """
    stored = _stored(instance, lock=True)
    if stored is None:
        return
    _record(stored, sender.AUDIT_REMOVE, before=stored.audit_values(), after=None)


def audited_access[M: AuditedAccessModel](model: type[M]) -> type[M]:
    """Class decorator: record the removal of ``model``'s rows.

    Connected per model, never for every sender: a receiver for every
    model would stop Django's fast delete for all of them.
    """
    pre_delete.connect(
        _record_removal,
        sender=model,
        weak=False,
        dispatch_uid=f"audit-access-removal-{model._meta.label_lower}",
    )
    return model
