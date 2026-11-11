from typing import ClassVar

from django import forms
from django.contrib.auth import forms as auth_forms

from apps.accounts.models import User


class UserCreationForm(auth_forms.AdminUserCreationForm):
    """Admin add form for `User`, keyed on email instead of username."""

    class Meta(auth_forms.AdminUserCreationForm.Meta):
        model = User
        fields = ("email",)
        field_classes: ClassVar[dict[str, type[forms.Field]]] = {}


class UserChangeForm(auth_forms.UserChangeForm):
    """Admin change form for `User`, keyed on email instead of username."""

    class Meta(auth_forms.UserChangeForm.Meta):
        model = User
        fields = "__all__"
        field_classes: ClassVar[dict[str, type[forms.Field]]] = {}
