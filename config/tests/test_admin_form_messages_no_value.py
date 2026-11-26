"""No admin form field has an error message that names the submitted value.

Issue #162. The shared admin mixin fixes the fields the admin builds from
model fields, but a form can declare its own fields: Django's
``AdminUserCreationForm`` and ``AdminPasswordChangeForm`` declare
``usable_password``, and django_celery_beat's ``PeriodicTaskForm``
declares ``regtask``, each with Django's ``invalid_choice`` message that
repeats the value. This walks every registered admin's form, each
inline's form, and the Account set-password form.
"""

from typing import Any

import pytest
from django import forms
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.test import Client, RequestFactory
from django.urls import reverse

from apps.accounts.admin import UserAdmin
from apps.accounts.tests.factories import UserFactory

pytestmark = pytest.mark.django_db

PLACEHOLDERS = ("%(value)s", "%(pk)s")


def _offending(form: forms.BaseForm, owner: object) -> list[str]:
    found = []
    for name, field in form.fields.items():
        for key, message in field.error_messages.items():
            if any(p in str(message) for p in PLACEHOLDERS):
                found.append(f"{owner}: {name}.{key}")
    return found


def test_no_admin_form_field_message_names_the_value() -> None:
    request = RequestFactory().get("/")
    request.user = UserFactory.create(is_staff=True, is_superuser=True)
    account = UserFactory.create()
    found: list[str] = []

    for model_admin in admin.site._registry.values():
        form_class: Any = model_admin.get_form(request, obj=None)
        found += _offending(form_class(), model_admin)
        for inline in model_admin.get_inline_instances(request):
            formset: Any = inline.get_formset(request, None)
            found += _offending(formset.form(), inline)

    user_admin = admin.site._registry[type(account)]
    assert isinstance(user_admin, UserAdmin)
    change_form: Any = user_admin.get_form(request, obj=account)
    found += _offending(change_form(instance=account), "UserAdmin change form")
    found += _offending(
        user_admin.change_password_form(account), "UserAdmin.change_password_form"
    )

    assert found == []


MARKER = "zqxjmarker"


def test_account_add_refuses_tampered_usable_password_without_echo(
    client: Client,
) -> None:
    client.force_login(UserFactory.create(is_staff=True, is_superuser=True))

    response = client.post(
        reverse("admin:accounts_user_add"),
        {"email": "made-up-162@example.test", "usable_password": MARKER},
    )

    assert response.status_code == 200
    errors = response.context["adminform"].form.errors
    assert "usable_password" in errors
    assert MARKER not in " ".join(errors["usable_password"])
    assert MARKER not in response.content.decode()
    assert not get_user_model().objects.filter(email="made-up-162@example.test")


def test_set_password_refuses_tampered_usable_password_without_echo(
    client: Client,
) -> None:
    client.force_login(UserFactory.create(is_staff=True, is_superuser=True))
    target = UserFactory.create()
    before = target.password

    response = client.post(
        reverse("admin:auth_user_password_change", args=[target.pk]),
        {"usable_password": MARKER, "password1": "", "password2": ""},
    )

    assert response.status_code == 200
    errors = response.context["form"].errors
    assert "usable_password" in errors
    assert MARKER not in " ".join(errors["usable_password"])
    assert MARKER not in response.content.decode()
    target.refresh_from_db()
    assert target.password == before
