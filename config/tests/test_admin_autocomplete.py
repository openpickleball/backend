"""Every admin autocomplete field uses a widget that survives a tampered id.

Issue #161. The frozen reproduction is
``test_fix_admin_autocomplete_non_numeric_id.py``; these tests cover the
rest: an unknown numeric id, a valid save, every registered admin and
inline, and many-to-many fields.
"""

from typing import TYPE_CHECKING, Any

import pytest
from django import forms
from django.contrib import admin
from django.contrib.admin.widgets import RelatedFieldWidgetWrapper
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import Client, RequestFactory
from django.urls import reverse

from apps.accounts.tests.factories import UserFactory
from apps.audit.admin_audit import AdminAuditMixin
from apps.clubs.tests.factories import OrganizerFactory
from apps.events.models import Event
from config.admin_autocomplete import (
    MULTIPLE_CHOICE_ERRORS,
    SafeAutocompleteAdminMixin,
    SafeAutocompleteSelect,
    SafeAutocompleteSelectMultiple,
    safe_autocomplete_class,
)

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

pytestmark = pytest.mark.django_db

MARKER = "zqxjmarker"
SAFE_WIDGETS = (SafeAutocompleteSelect, SafeAutocompleteSelectMultiple)


def _superuser() -> "_User":
    account: _User = UserFactory.create(is_staff=True, is_superuser=True)
    return account


def _event_data(organizer: str, club: str = "") -> dict[str, str]:
    return {
        "name": "Made-up Fall Open",
        "kind": "rec_verified",
        "organizer": organizer,
        "club": club,
        "start_date": "2026-09-05",
        "end_date": "2026-09-06",
    }


def _widget(field: forms.Field | None) -> forms.Widget:
    assert field is not None
    widget: forms.Widget = field.widget
    if isinstance(widget, RelatedFieldWidgetWrapper):
        inner: forms.Widget = widget.widget
        return inner
    return widget


def test_unknown_numeric_id_gets_form_error(client: Client) -> None:
    client.force_login(_superuser())

    response = client.post(
        reverse("admin:events_event_add"), _event_data(organizer="987654321")
    )

    assert response.status_code == 200
    assert "organizer" in response.context["adminform"].form.errors
    assert not Event.objects.exists()


def test_valid_id_still_saves(client: Client) -> None:
    client.force_login(_superuser())
    organizer = OrganizerFactory.create()

    response = client.post(
        reverse("admin:events_event_add"), _event_data(organizer=str(organizer.pk))
    )

    assert response.status_code == 302
    assert Event.objects.get().organizer == organizer


def test_valid_selection_still_renders() -> None:
    organizer = OrganizerFactory.create()
    field = Event._meta.get_field("organizer")
    widget = SafeAutocompleteSelect(field, admin.site)
    widget.choices = forms.ModelChoiceField(
        queryset=type(organizer).objects.all()
    ).choices

    html = widget.render("organizer", str(organizer.pk))

    assert f'value="{organizer.pk}" selected' in html


def test_every_registered_admin_and_inline_uses_safe_widgets() -> None:
    request = RequestFactory().get("/")
    request.user = _superuser()
    seen: set[str] = set()

    def check(model_admin: Any) -> None:
        assert isinstance(model_admin, SafeAutocompleteAdminMixin)
        for name in model_admin.get_autocomplete_fields(request):
            db_field = model_admin.model._meta.get_field(name)
            field = model_admin.formfield_for_dbfield(db_field, request)
            assert isinstance(_widget(field), SAFE_WIDGETS), (model_admin, name)
            seen.add(f"{model_admin.model._meta.label_lower}.{name}")

    for model, model_admin in admin.site._registry.items():
        mro = type(model_admin).__mro__
        assert mro.index(AdminAuditMixin) < mro.index(SafeAutocompleteAdminMixin), model
        check(model_admin)
        for inline in model_admin.get_inline_instances(request):
            check(inline)

    assert {
        "clubs.organizermember.account",
        "clubs.clubrole.account",
        "events.event.organizer",
        "events.event.club",
    } <= seen


def test_safe_autocomplete_class_is_cached_and_idempotent() -> None:
    base: type[admin.ModelAdmin[Any]] = admin.ModelAdmin
    wrapped = safe_autocomplete_class(base)

    assert safe_autocomplete_class(base) is wrapped
    assert safe_autocomplete_class(wrapped) is wrapped


def test_many_to_many_autocomplete_refuses_without_echo() -> None:
    request = RequestFactory().get("/")
    request.user = _superuser()
    groups_admin: type[admin.ModelAdmin[Any]] = type(
        "GroupsAdmin", (admin.ModelAdmin,), {"autocomplete_fields": ("groups",)}
    )
    account_admin_class = safe_autocomplete_class(groups_admin)
    model_admin = account_admin_class(get_user_model(), admin.site)
    db_field = get_user_model()._meta.get_field("groups")
    field = model_admin.formfield_for_dbfield(db_field, request)
    assert field is not None
    widget = _widget(field)
    assert isinstance(widget, SafeAutocompleteSelectMultiple)

    for value in ([MARKER], [str(Group.objects.create(name="made-up").pk + 1)]):
        with pytest.raises(forms.ValidationError) as excinfo:
            field.clean(value)
        assert MARKER not in " ".join(excinfo.value.messages)
        assert set(excinfo.value.messages) <= set(MULTIPLE_CHOICE_ERRORS.values())

    html = widget.render("groups", [MARKER, "zqxj\x00marker"])
    assert MARKER not in html


# Issue #162: every admin choice and many-to-many field names no value.

CHOICE_KEYS = ("invalid_choice",)
MULTIPLE_KEYS = ("invalid_choice", "invalid_pk_value")


def test_every_admin_choice_and_many_to_many_field_has_fixed_messages() -> None:
    request = RequestFactory().get("/")
    request.user = _superuser()
    seen: set[str] = set()

    def check(model_admin: Any) -> None:
        for db_field in model_admin.model._meta.get_fields():
            is_m2m = getattr(db_field, "many_to_many", False) and not getattr(
                db_field, "auto_created", False
            )
            has_choices = bool(getattr(db_field, "choices", None))
            if not (is_m2m or has_choices) or not getattr(db_field, "concrete", True):
                continue
            field = model_admin.formfield_for_dbfield(db_field, request)
            if field is None:
                continue
            keys = MULTIPLE_KEYS if is_m2m else CHOICE_KEYS
            for key in keys:
                message = str(field.error_messages[key])
                assert "%(" not in message, (model_admin, db_field.name, key)
            seen.add(f"{model_admin.model._meta.label_lower}.{db_field.name}")

    for model_admin in admin.site._registry.values():
        check(model_admin)
        for inline in model_admin.get_inline_instances(request):
            check(inline)

    assert {
        "clubs.organizermember.role",
        "clubs.clubrole.role",
        "accounts.user.groups",
        "accounts.user.user_permissions",
        "auth.group.permissions",
    } <= seen


def test_caller_choice_messages_win() -> None:
    request = RequestFactory().get("/")
    model_admin = safe_autocomplete_class(admin.ModelAdmin)(Event, admin.site)
    db_field = Event._meta.get_field("kind")

    field = model_admin.formfield_for_choice_field(
        db_field, request, error_messages={"invalid_choice": "Made-up text."}
    )

    assert field is not None
    assert field.error_messages["invalid_choice"] == "Made-up text."


def test_caller_many_to_many_messages_win() -> None:
    request = RequestFactory().get("/")
    model_admin = safe_autocomplete_class(admin.ModelAdmin)(
        get_user_model(), admin.site
    )
    db_field = get_user_model()._meta.get_field("groups")

    field = model_admin.formfield_for_manytomany(
        db_field, request, error_messages={"invalid_pk_value": "Made-up text."}
    )

    assert field is not None
    assert field.error_messages["invalid_pk_value"] == "Made-up text."
    assert (
        field.error_messages["invalid_choice"]
        == MULTIPLE_CHOICE_ERRORS["invalid_choice"]
    )
