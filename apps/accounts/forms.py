from typing import TYPE_CHECKING, ClassVar

from django import forms
from django.contrib.auth import forms as auth_forms

from apps.accounts.models import User
from apps.players.models import EARLIEST_BIRTH_YEAR, GenderDivision

if TYPE_CHECKING:
    from django.contrib.auth.base_user import AbstractBaseUser
    from django.http import HttpRequest


class UserCreationForm(auth_forms.AdminUserCreationForm[User]):
    """Admin add form for `User`, keyed on email instead of username."""

    class Meta(auth_forms.AdminUserCreationForm.Meta):
        model = User
        fields = ("email",)
        field_classes: ClassVar[dict[str, type[forms.Field]]] = {}


class UserChangeForm(auth_forms.UserChangeForm[User]):
    """Admin change form for `User`, keyed on email instead of username."""

    class Meta(auth_forms.UserChangeForm.Meta):
        model = User
        fields = "__all__"
        field_classes: ClassVar[dict[str, type[forms.Field]]] = {}


# Never the submitted value: allauth returns these messages to the client.
_WHOLE_NUMBER = "Enter a whole number."
_BIRTH_YEAR_MESSAGES = {
    "required": "Enter your birth year.",
    "invalid": _WHOLE_NUMBER,
    "min_value": f"Enter a birth year of {EARLIEST_BIRTH_YEAR} or later.",
}
_BIRTH_MONTH_MESSAGES = {
    "required": "Enter your birth month.",
    "invalid": _WHOLE_NUMBER,
    "min_value": "Enter a month from 1 to 12.",
    "max_value": "Enter a month from 1 to 12.",
}
_GENDER_DIVISION_MESSAGES = {
    "required": "Choose a gender division.",
    "invalid_choice": "Choose one of the listed options.",
}


class SignupAgeForm(forms.Form):
    """The signup details on allauth's signup (issues #114 and #215).

    ``ACCOUNT_SIGNUP_FORM_CLASS`` names this form, so allauth's signup
    input for both headless clients carries the fields and its OpenAPI
    document lists them as required. A birth year and month only, never a
    day, and a gender division (``mens`` or ``womens``), never a raw gender
    (ADR 0009, ADR 0017). OPEN's signup view
    (``apps/accounts/headless_views.py``) checks them first and refuses
    anyone under 13.

    allauth imports this class while it builds its own signup form, so this
    module must never import allauth's forms.
    """

    birth_year = forms.IntegerField(
        min_value=EARLIEST_BIRTH_YEAR, error_messages=_BIRTH_YEAR_MESSAGES
    )
    birth_month = forms.IntegerField(
        min_value=1, max_value=12, error_messages=_BIRTH_MONTH_MESSAGES
    )
    gender_division = forms.ChoiceField(
        choices=GenderDivision.choices, error_messages=_GENDER_DIVISION_MESSAGES
    )

    def signup(self, request: "HttpRequest", user: "AbstractBaseUser") -> None:
        """allauth calls this after saving a new Account. Nothing to store:
        the signup view holds the details in the session."""
