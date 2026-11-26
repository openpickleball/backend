"""Audit Events for every change made through the admin (issue #101).

``OpenAdminSite.register`` (``config/admin.py``) puts ``AdminAuditMixin``
first in the bases of every ModelAdmin it registers, OPEN's own and
third-party ones alike. Each add, change, and delete then records one
Audit Event through ``record_event``, in the change's transaction, with
``request.user`` as actor and the object as target:

- ``admin.add``: ``after`` is ``{"values": ...}``.
- ``admin.change``, only when a field of the object changed: ``before``
  is ``{"values": ...}`` and ``after`` is ``{"fields": [...], "values":
  ...}``, where ``fields`` names every changed field (sorted).
- ``admin.delete``, recorded before the row goes: ``before`` is
  ``{"values": ...}``.

``values`` holds only the fields ``AUDIT_VALUE_FIELDS`` lists for the
model, built with ``snapshot()``. A model it does not list records no
values (``before`` or ``after`` is None, or ``after`` holds only
``fields``). So an identifying personal data value (ADR 0009), a
password hash, a secret, or a token never reaches an event; a changed
field of that kind shows only by name.

What a change compares is read from the database: the stored row when
the admin loads it for a POST (``get_object``: the change form, the
Account's set-password view) or before ``save_model`` (the list_editable
changelist), and again when Django logs the change, after the form and
its many-to-many fields are saved. Fields Django sets on every save
(``auto_now``) are not compared.

Custom actions record one ``admin.action`` per selected object
(``after={"action": name}``), before the action runs and in one
transaction with it. ``delete_selected`` is not wrapped (its deletes
record ``admin.delete``), and neither is an action marked
``@audited_by_service``, whose service records its own events per
object: the bulk voids among them must never run inside an outer
transaction.

An admin that already saves and deletes through an audited service sets
``audited_by_service = True`` on its class (the Event admin, the grant
admins) and is registered without this mixin.

When a delete removes the acting Account itself, its events have no
actor, as ``apps.audit.access`` does: an event naming a deleted Account
fails the delete at commit.
"""

from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.db import models, transaction
from django.db.models import QuerySet
from django.forms import ModelForm
from django.http import HttpRequest, HttpResponseBase

from apps.audit.models import AuditAction
from apps.audit.services import record_event, snapshot

if TYPE_CHECKING:
    from apps.audit.access import Actor

# The only fields whose values an admin event holds, by model label.
AUDIT_VALUE_FIELDS: dict[str, tuple[str, ...]] = {
    "accounts.user": (
        "is_active",
        "is_staff",
        "is_superuser",
        "groups",
        "user_permissions",
    ),
    "auth.group": ("name", "permissions"),
}

# Set on the request: the stored state of each object the admin is
# changing, keyed by (model label, primary key).
_BEFORE_ATTRIBUTE = "_open_admin_audit_before"


def audited_by_service[F: Callable[..., Any]](action: F) -> F:
    """Mark an admin action whose service records its own Audit Events
    (one per object), so it is not wrapped in ``admin.action`` events."""
    action.audited_by_service = True  # type: ignore[attr-defined]
    return action


def _label(model: type[models.Model]) -> str:
    return model._meta.label_lower


def _value_fields(model: type[models.Model]) -> tuple[str, ...]:
    return AUDIT_VALUE_FIELDS.get(_label(model), ())


def _values(obj: models.Model) -> dict[str, Any] | None:
    """``{"values": ...}`` for the listed fields, or None when none are."""
    fields = _value_fields(type(obj))
    return {"values": snapshot(obj, fields)} if fields else None


def _state(obj: models.Model) -> dict[str, Any]:
    """Every compared field's stored value, kept in memory only."""
    state: dict[str, Any] = {}
    for field in obj._meta.concrete_fields:
        if getattr(field, "auto_now", False):
            continue
        state[field.name] = field.value_from_object(obj)
    for field in obj._meta.many_to_many:
        related = getattr(obj, field.name).all()
        state[field.name] = sorted(related.values_list("pk", flat=True))
    return state


def _stored(model: type[models.Model], pk: Any) -> models.Model | None:
    stored: models.Model | None = model._base_manager.filter(pk=pk).first()
    return stored


def _actor(request: HttpRequest) -> "Actor":
    return getattr(request.user, "_wrapped", request.user)


def _actor_unless_deleted(request: HttpRequest, objs: Iterable[Any]) -> "Actor":
    actor = _actor(request)
    if actor is None or not actor.is_authenticated:
        return actor
    account_model = get_user_model()
    deleted = {obj.pk for obj in objs if isinstance(obj, account_model)}
    return None if actor.pk in deleted else actor


class AdminAuditMixin(admin.ModelAdmin[Any]):
    """Record each add, change, delete, and custom action (see module)."""

    # Captures

    def _capture(self, request: HttpRequest, pk: Any) -> None:
        captured: dict[tuple[str, str], tuple[dict[str, Any], Any]] = getattr(
            request, _BEFORE_ATTRIBUTE, {}
        )
        key = (_label(self.model), str(pk))
        if key in captured:
            return
        stored = _stored(self.model, pk)
        if stored is None:
            return
        captured[key] = (_state(stored), _values(stored))
        setattr(request, _BEFORE_ATTRIBUTE, captured)

    def _captured(
        self, request: HttpRequest, obj: models.Model
    ) -> tuple[dict[str, Any], Any] | None:
        captured: dict[tuple[str, str], tuple[dict[str, Any], Any]] = getattr(
            request, _BEFORE_ATTRIBUTE, {}
        )
        return captured.pop((_label(type(obj)), str(obj.pk)), None)

    def get_object(
        self, request: HttpRequest, object_id: str, from_field: str | None = None
    ) -> Any:
        obj = super().get_object(request, object_id, from_field)
        if obj is not None and request.method == "POST":
            self._capture(request, obj.pk)
        return obj

    def save_model(
        self, request: HttpRequest, obj: Any, form: ModelForm[Any], change: bool
    ) -> None:
        if change and obj.pk is not None:
            self._capture(request, obj.pk)
        super().save_model(request, obj, form, change)

    # Adds and changes

    def log_addition(self, request: HttpRequest, obj: Any, message: Any) -> Any:
        logged = super().log_addition(request, obj, message)
        stored = _stored(type(obj), obj.pk)
        record_event(
            action=AuditAction.ADMIN_ADD,
            actor=_actor(request),
            target=obj,
            after=None if stored is None else _values(stored),
        )
        return logged

    def log_change(self, request: HttpRequest, obj: Any, message: Any) -> Any:
        logged = super().log_change(request, obj, message)
        stored = _stored(type(obj), obj.pk)
        if stored is None:
            return logged
        after_state = _state(stored)
        captured = self._captured(request, obj)
        if captured is None:
            # Never seen before the save: every compared field counts.
            before_state: dict[str, Any] = {}
            before_values = None
        else:
            before_state, before_values = captured
        fields = sorted(
            name
            for name, value in after_state.items()
            if name not in before_state or before_state[name] != value
        )
        if not fields:
            return logged
        after: dict[str, Any] = {"fields": fields}
        after_values = _values(stored)
        if after_values is not None:
            after.update(after_values)
        record_event(
            action=AuditAction.ADMIN_CHANGE,
            actor=_actor(request),
            target=stored,
            before=before_values,
            after=after,
        )
        return logged

    # Deletes

    def delete_model(self, request: HttpRequest, obj: Any) -> None:
        with transaction.atomic():
            self._record_deletes(request, [obj])
            super().delete_model(request, obj)

    def delete_queryset(self, request: HttpRequest, queryset: QuerySet[Any]) -> None:
        with transaction.atomic():
            self._record_deletes(request, list(queryset.order_by("pk")))
            super().delete_queryset(request, queryset)

    def _record_deletes(self, request: HttpRequest, objs: list[Any]) -> None:
        actor = _actor_unless_deleted(request, objs)
        for obj in objs:
            record_event(
                action=AuditAction.ADMIN_DELETE,
                actor=actor,
                target=obj,
                before=_values(obj),
            )

    # Actions

    def get_actions(self, request: HttpRequest, *args: Any, **kwargs: Any) -> Any:
        actions: dict[str, Any] = dict(super().get_actions(request, *args, **kwargs))
        for key, entry in actions.items():
            if entry is None:
                continue
            func, name, description = entry
            if name == "delete_selected" or getattr(func, "audited_by_service", False):
                continue
            actions[key] = (_audited_action(func, name), name, description)
        return actions


def _audited_action(func: Callable[..., Any], name: str) -> Callable[..., Any]:
    """``func``, recording one ``admin.action`` per selected object first,
    in one transaction with the action."""

    def audited(
        modeladmin: admin.ModelAdmin[Any],
        request: HttpRequest,
        queryset: QuerySet[Any],
    ) -> HttpResponseBase | None:
        with transaction.atomic():
            for obj in queryset.order_by("pk"):
                record_event(
                    action=AuditAction.ADMIN_ACTION,
                    actor=_actor(request),
                    target=obj,
                    after={"action": name},
                )
            response: HttpResponseBase | None = func(modeladmin, request, queryset)
            return response

    audited.__name__ = getattr(func, "__name__", name)
    audited.__doc__ = func.__doc__
    return audited


def audited_admin_class(
    admin_class: type[admin.ModelAdmin[Any]],
) -> type[admin.ModelAdmin[Any]]:
    """``admin_class`` with ``AdminAuditMixin`` first, unless it already
    has it or is audited by its own services."""
    if issubclass(admin_class, AdminAuditMixin) or getattr(
        admin_class, "audited_by_service", False
    ):
        return admin_class
    return type(
        admin_class.__name__,
        (AdminAuditMixin, admin_class),
        {"__module__": admin_class.__module__, "__doc__": admin_class.__doc__},
    )
