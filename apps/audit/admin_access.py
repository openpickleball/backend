"""Admin hooks that audit access rows (#158, ADR 0033).

``AccessAuditAdminMixin`` goes first in the bases of every admin that
saves or deletes an Organizer Member, a Club Role, or a grant, or deletes
a row that cascades to one (a Club, an Organizer, a Partner Application,
an Account). It routes those changes through ``apps.audit.access`` with
``request.user`` as the actor, in the admin's transaction.
"""

from typing import Any

from django.contrib import admin
from django.db import transaction
from django.db.models import Model, QuerySet
from django.forms import BaseInlineFormSet, ModelForm
from django.http import HttpRequest

from apps.audit.access import (
    AuditedAccessModel,
    delete_access_row,
    record_cascaded_removals,
    save_access_row,
)


class AccessAuditAdminMixin(admin.ModelAdmin[Any]):
    """Record access row changes made through this admin."""

    def save_model(
        self, request: HttpRequest, obj: Any, form: ModelForm[Any], change: bool
    ) -> None:
        if isinstance(obj, AuditedAccessModel):
            save_access_row(obj, actor=request.user)
        else:
            super().save_model(request, obj, form, change)

    def delete_model(self, request: HttpRequest, obj: Any) -> None:
        with transaction.atomic():
            record_cascaded_removals([obj], actor=request.user)
            super().delete_model(request, obj)

    def delete_queryset(self, request: HttpRequest, queryset: QuerySet[Any]) -> None:
        with transaction.atomic():
            record_cascaded_removals(queryset, actor=request.user)
            super().delete_queryset(request, queryset)

    def save_formset(
        self,
        request: HttpRequest,
        form: ModelForm[Any],
        formset: BaseInlineFormSet[Any, Model, Any],
        change: bool,
    ) -> None:
        model = formset.model
        if model is None or not issubclass(model, AuditedAccessModel):
            super().save_formset(request, form, formset, change)
            return
        with transaction.atomic():
            # commit=False leaves the deleted rows for us to delete.
            rows = formset.save(commit=False)
            for row in formset.deleted_objects:
                delete_access_row(row, actor=request.user)
            for row in rows:
                save_access_row(row, actor=request.user)
            formset.save_m2m()
