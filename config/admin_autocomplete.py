"""Admin autocomplete widgets that survive a tampered id (issue #161).

Django's ``ModelChoiceField`` refuses a value that is not a valid key with
fixed text. When the admin shows the form again with that error,
Django's autocomplete widget filters the related model on the raw posted
values, and an integer key raises ``ValueError``: a 500 instead of the
form. The safe widgets here drop every value the related key field
refuses before that query. A dropped value can never match a row, so the
widget simply shows no selected option for it.

This covers autocomplete fields whose target key is an integer, which
every current one is. The widgets also drop any value with a NUL
character, but that only keeps the widget's own lookup safe. For a text
key (a CharField primary key or ``to_field``), the form field's own
lookup in ``clean()`` runs first and Postgres refuses the NUL there, so
such a field would also need its form field to refuse NUL. None needs it
today.

``OpenAdminSite.register`` (``config/admin.py``) adds
``SafeAutocompleteAdminMixin`` to every registered admin, right after
``AdminAuditMixin``, and the mixin adds itself to every inline that admin
shows. So every autocomplete field uses a safe widget, with no opt-in.
Many-to-many autocomplete fields also get fixed error text, since
Django's own messages for them name the submitted value.
"""

from collections.abc import Sequence
from typing import Any, cast

from django.contrib.admin.options import BaseModelAdmin, InlineModelAdmin
from django.contrib.admin.widgets import (
    AutocompleteSelect,
    AutocompleteSelectMultiple,
)
from django.core.exceptions import ValidationError
from django.db.models import ForeignKey, ManyToManyField
from django.forms import ModelChoiceField, ModelMultipleChoiceField
from django.http import HttpRequest

# Fixed text: Django's many-to-many messages repeat the submitted value.
MULTIPLE_CHOICE_ERRORS = {
    "invalid_choice": (
        "Select a valid choice. That choice is not one of the available choices."
    ),
    "invalid_pk_value": "Select a valid choice.",
}


class _SafeOptgroupsMixin:
    """Drop values the related key cannot hold before Django's lookup."""

    field: Any

    def _usable(self, value: str) -> bool:
        # Keeps the widget's own lookup safe for a text key. The form
        # field's lookup runs earlier and is not covered (module docstring).
        if "\x00" in value:
            return False
        remote = self.field.remote_field
        opts = remote.model._meta
        key = opts.get_field(getattr(remote, "field_name", None) or opts.pk.name)
        try:
            key.to_python(value)
        except ValidationError:
            return False
        return True

    def optgroups(
        self, name: str, value: Sequence[str], attr: dict[str, Any] | None = None
    ) -> list[tuple[str | None, list[dict[str, Any]], int | None]]:
        usable = [v for v in value if self._usable(str(v))]
        groups: list[tuple[str | None, list[dict[str, Any]], int | None]] = (
            super().optgroups(  # type: ignore[misc]
                name, usable, attr
            )
        )
        return groups


class SafeAutocompleteSelect(_SafeOptgroupsMixin, AutocompleteSelect):
    """Django's ``AutocompleteSelect`` that ignores an unusable id."""


class SafeAutocompleteSelectMultiple(_SafeOptgroupsMixin, AutocompleteSelectMultiple):
    """Django's ``AutocompleteSelectMultiple`` that ignores an unusable id."""


class SafeAutocompleteAdminMixin(BaseModelAdmin[Any]):
    """Give every autocomplete field of an admin, and of its inlines, a
    safe widget."""

    def formfield_for_foreignkey(
        self, db_field: ForeignKey[Any, Any], request: HttpRequest, **kwargs: Any
    ) -> ModelChoiceField[Any] | None:
        if "widget" not in kwargs and db_field.name in self.get_autocomplete_fields(
            request
        ):
            kwargs["widget"] = SafeAutocompleteSelect(
                db_field, self.admin_site, using=kwargs.get("using")
            )
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def formfield_for_manytomany(
        self, db_field: ManyToManyField[Any, Any], request: HttpRequest, **kwargs: Any
    ) -> ModelMultipleChoiceField[Any] | None:
        if "widget" not in kwargs and db_field.name in self.get_autocomplete_fields(
            request
        ):
            kwargs["widget"] = SafeAutocompleteSelectMultiple(
                db_field, self.admin_site, using=kwargs.get("using")
            )
            kwargs["error_messages"] = {
                **MULTIPLE_CHOICE_ERRORS,
                **kwargs.get("error_messages", {}),
            }
        return super().formfield_for_manytomany(db_field, request, **kwargs)

    def get_inlines(
        self, request: HttpRequest, obj: Any
    ) -> list[type[InlineModelAdmin[Any, Any]]]:
        inlines = super().get_inlines(request, obj)
        return [safe_autocomplete_class(inline) for inline in inlines]


# One subclass per wrapped class, so an inline class keeps one identity
# across requests.
_SAFE_CLASSES: dict[type[Any], type[Any]] = {}


def safe_autocomplete_class[T: BaseModelAdmin[Any]](cls: type[T]) -> type[T]:
    """``cls`` with ``SafeAutocompleteAdminMixin`` first, unless it already
    has it. Each class gets one subclass, made on first use."""
    if issubclass(cls, SafeAutocompleteAdminMixin):
        return cls
    safe = _SAFE_CLASSES.get(cls)
    if safe is None:
        made = type(
            cls.__name__,
            (SafeAutocompleteAdminMixin, cls),
            {"__module__": cls.__module__, "__doc__": cls.__doc__},
        )
        safe = _SAFE_CLASSES.setdefault(cls, made)
    return cast("type[T]", safe)
