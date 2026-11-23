"""Admin for Events."""

from typing import Any, ClassVar

from django import forms
from django.contrib import admin

from apps.events.models import CHOICE_MESSAGES, Event


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
