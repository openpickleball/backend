"""Admin for Events.

Saves and deletes go through ``apps.events.services``, which record one
Audit Event each with ``request.user`` as actor (#163).
"""

from typing import Any, ClassVar

from django import forms
from django.contrib import admin
from django.db import transaction
from django.db.models import QuerySet
from django.http import HttpRequest

from apps.events.models import CHOICE_MESSAGES, Event
from apps.events.services import delete_event, save_event


class EventAdminForm(forms.ModelForm[Event]):
    """The Event form, with messages that never name the submitted value.

    A model field's ``error_messages`` do not reach its form field, and
    the form's own ``invalid_choice`` message names the value.
    """

    class Meta:
        model = Event
        fields = (
            "name",
            "kind",
            "organizer",
            "club",
            "start_date",
            "end_date",
        )
        error_messages: ClassVar[dict[str, dict[str, str]]] = {"kind": CHOICE_MESSAGES}

    def clean(self) -> dict[str, Any] | None:
        """Clear each date that failed on the instance before it is checked.

        The form leaves a field that failed at its stored value, and the
        model's ``clean()`` still runs, so its order check would compare
        the other new date with the stored one and report a false error.
        """
        cleaned_data = super().clean()
        for name in ("start_date", "end_date"):
            if self.has_error(name):
                setattr(self.instance, name, None)
        return cleaned_data


@admin.register(Event)
class EventAdmin(admin.ModelAdmin[Event]):
    # save_event and delete_event record the Audit Events (#101).
    audited_by_service = True
    form = EventAdminForm
    list_display = (
        "public_code",
        "name",
        "kind",
        "organizer",
        "club",
        "start_date",
        "end_date",
    )
    readonly_fields = ("public_code",)
    search_fields = ("public_code", "name")
    list_filter = ("kind",)
    autocomplete_fields = ("organizer", "club")

    def save_model(
        self,
        request: HttpRequest,
        obj: Event,
        form: forms.ModelForm[Event],
        change: bool,
    ) -> None:
        save_event(obj, actor=request.user)

    def delete_model(self, request: HttpRequest, obj: Event) -> None:
        delete_event(obj, actor=request.user)

    def delete_queryset(self, request: HttpRequest, queryset: QuerySet[Event]) -> None:
        # One transaction, one Audit Event per Event.
        with transaction.atomic():
            for event in queryset.order_by("pk"):
                delete_event(event, actor=request.user)
