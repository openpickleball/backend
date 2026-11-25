"""Audit Events for rows that grant access or consent (#158, ADR 0033).

Organizer Members, Club Roles, Club Organizer Grants, and Organizer
Application Grants decide who may submit counted matches (CLAUDE.md, hard
rule 8). Their models inherit ``AuditedAccessModel``, which names their
actions and their ``audit_values()``. The admin, and any service that
changes these rows, changes them only through the functions here, passing
the acting Account explicitly:

- ``save_access_row()`` saves one row and records an add or a change in
  the same transaction. A save that changes nothing records nothing, and
  a save that raises records nothing.
- ``delete_access_row()`` records one row's removal, then deletes it.
- ``record_cascaded_removals()`` records a removal for every access row a
  delete of other rows (a Club, an Organizer, a Partner Application, an
  Account) will cascade to. Call it in the delete's transaction, just
  before the delete.

A plain ORM save or delete, a cascade outside these functions, and raw
writes (``bulk_create``, ``QuerySet.update``, ``bulk_update``, raw SQL,
``loaddata``) record nothing. ``before`` and ``after`` hold roles, OPEN
Codes, Account row ids, and client ids, never an email or an Organizer's
name.
"""

from collections.abc import Iterable
from typing import Any, ClassVar

from django.contrib.auth import get_user_model
from django.contrib.auth.base_user import AbstractBaseUser
from django.contrib.auth.models import AnonymousUser
from django.db import models, router, transaction
from django.db.models.deletion import Collector, ProtectedError, RestrictedError

from apps.audit.models import AuditAction

Actor = AbstractBaseUser | AnonymousUser | None


class AuditedAccessModel(models.Model):
    """A row whose adds, changes, and removals are audited (see module)."""

    AUDIT_ADD: ClassVar[AuditAction]
    AUDIT_CHANGE: ClassVar[AuditAction]
    AUDIT_REMOVE: ClassVar[AuditAction]
    # Passed to select_related when the stored row is read.
    AUDIT_RELATED: ClassVar[tuple[str, ...]] = ()
    # Foreign keys whose rows a save locks before the row itself, in the
    # order the model's own save and the parent's delete lock them.
    AUDIT_LOCK_FIRST: ClassVar[tuple[str, ...]] = ()

    class Meta:
        abstract = True

    def audit_values(self) -> dict[str, Any]:
        """The values ``before`` and ``after`` hold. Never an identifying
        personal data field's value (ADR 0009).
        """
        raise NotImplementedError


def save_access_row(row: AuditedAccessModel, *, actor: Actor) -> None:
    """Save ``row`` and record its add or change with ``actor``."""
    with transaction.atomic():
        for name in row.AUDIT_LOCK_FIRST:
            field = row._meta.get_field(name)
            assert isinstance(field, models.ForeignKey)
            parent_pk = getattr(row, field.attname)
            if parent_pk is not None:
                list(
                    field.related_model._base_manager.select_for_update()
                    .filter(pk=parent_pk)
                    .values_list("pk", flat=True)
                )
        stored = _stored(row, lock=True)
        before = None if stored is None else stored.audit_values()
        row.save()
        saved = _stored(row, lock=False)
        if saved is None:
            raise RuntimeError("The saved row could not be read back.")
        after = saved.audit_values()
        if before is None:
            _record(saved, saved.AUDIT_ADD, actor, before=None, after=after)
        elif before != after:
            _record(saved, saved.AUDIT_CHANGE, actor, before=before, after=after)


def delete_access_row(row: AuditedAccessModel, *, actor: Actor) -> None:
    """Record ``row``'s removal with ``actor``, then delete it.

    A row already gone records nothing and deletes nothing.
    """
    with transaction.atomic():
        stored = _stored(row, lock=True)
        if stored is None:
            return
        _record_removal(stored, actor)
        row.delete()


def record_cascaded_removals(
    objs: Iterable[models.Model] | models.QuerySet[Any], *, actor: Actor
) -> None:
    """Record a removal for each access row deleting ``objs`` would remove.

    Call it inside the delete's transaction, just before the delete. It
    collects the delete as Django will run it, so the rows it records are
    the rows that go, ``objs`` themselves included when they are access
    rows. A delete Django would refuse (a protected or restricted link)
    records nothing here; the delete itself then raises.

    When the delete removes ``actor``'s own Account, every event it
    records has no actor, the other selected Accounts' rows included: an
    event naming a deleted Account fails the delete at commit.
    """
    rows = list(objs)
    if not rows:
        return
    model = type(rows[0])
    using = router.db_for_write(model)
    # Lock the deleted rows first, as their own deletes do (Organizer).
    list(
        model._base_manager.using(using)
        .select_for_update()
        .filter(pk__in=[row.pk for row in rows])
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    try:
        collector = _collect(rows, using)
        # Lock every row the delete cascades through, then collect again:
        # a row added under one of them before the lock is found now, and
        # one added after it waits until the delete commits.
        for deleted_model, deleted in sorted(
            collector.data.items(), key=lambda item: item[0]._meta.label
        ):
            list(
                deleted_model._base_manager.using(using)
                .select_for_update()
                .filter(pk__in=[i.pk for i in deleted])
                .order_by("pk")
                .values_list("pk", flat=True)
            )
        collector = _collect(rows, using)
    except (ProtectedError, RestrictedError):
        return
    actor = _actor_unless_deleted(actor, collector)
    for collected_model, instances in sorted(
        _access_rows(collector).items(), key=lambda item: item[0]._meta.label
    ):
        for pk in sorted(instances):
            stored = _stored_by_pk(collected_model, pk, lock=True)
            if stored is not None:
                _record_removal(stored, actor)


def _collect(rows: list[models.Model], using: str) -> Collector:
    collector = Collector(using=using, origin=rows[0])
    collector.collect(rows)
    return collector


def _access_rows(
    collector: Collector,
) -> dict[type[AuditedAccessModel], set[Any]]:
    """The primary keys of the access rows the collected delete removes."""
    found: dict[type[AuditedAccessModel], set[Any]] = {}
    for model, instances in collector.data.items():
        if issubclass(model, AuditedAccessModel):
            found.setdefault(model, set()).update(i.pk for i in instances)
    for queryset in collector.fast_deletes:
        if issubclass(queryset.model, AuditedAccessModel):
            found.setdefault(queryset.model, set()).update(
                queryset.values_list("pk", flat=True)
            )
    return found


def _actor_unless_deleted(actor: Actor, collector: Collector) -> Actor:
    if actor is None or not actor.is_authenticated:
        return actor
    account_model = get_user_model()
    deleted = {i.pk for i in collector.data.get(account_model, ())}
    return None if actor.pk in deleted else actor


def _stored[M: AuditedAccessModel](row: M, *, lock: bool) -> M | None:
    """The stored row for ``row``'s primary key, or None."""
    if row.pk is None:
        return None
    return _stored_by_pk(type(row), row.pk, lock=lock)


def _stored_by_pk[M: AuditedAccessModel](
    model: type[M], pk: Any, *, lock: bool
) -> M | None:
    rows = model._base_manager.select_related(*model.AUDIT_RELATED)
    if lock:
        rows = rows.select_for_update(of=("self",))
    stored: M | None = rows.filter(pk=pk).first()
    return stored


def _record_removal(stored: AuditedAccessModel, actor: Actor) -> None:
    _record(
        stored, stored.AUDIT_REMOVE, actor, before=stored.audit_values(), after=None
    )


def _record(
    target: AuditedAccessModel,
    action: AuditAction,
    actor: Actor,
    *,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> None:
    # Imported here: apps.audit.services imports django-oauth-toolkit's
    # models, which must not load while the models that use this module do.
    from apps.audit.services import record_event

    record_event(action=action, target=target, actor=actor, before=before, after=after)
