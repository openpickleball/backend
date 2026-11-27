from typing import TYPE_CHECKING, Any, ClassVar

from django import forms
from django.contrib.auth import forms as auth_forms

from apps.accounts.models import User
from apps.players.models import (
    CONTROL_CHARACTER_MESSAGE,
    EARLIEST_BIRTH_YEAR,
    GenderDivision,
    Player,
    has_refused_character,
    has_visible_character,
)
from config.admin_autocomplete import CHOICE_ERRORS

if TYPE_CHECKING:
    from django.contrib.auth.base_user import AbstractBaseUser
    from django.http import HttpRequest


class UserCreationForm(auth_forms.AdminUserCreationForm[User]):
    """Admin add form for `User`, keyed on email instead of username.

    ``usable_password`` is Django's declared field, whose message repeats a
    tampered value (issue #162), so it gets fixed text.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["usable_password"].error_messages.update(CHOICE_ERRORS)

    class Meta(auth_forms.AdminUserCreationForm.Meta):
        model = User
        fields = ("email",)
        field_classes: ClassVar[dict[str, type[forms.Field]]] = {}


class AdminPasswordChangeForm(auth_forms.AdminPasswordChangeForm):  # type: ignore[type-arg]
    """Django's admin set-password form, with fixed text for a tampered
    ``usable_password`` (issue #162). Django adds that field in
    ``__init__``, only for an Account with a usable password."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        if "usable_password" in self.fields:
            self.fields["usable_password"].error_messages.update(CHOICE_ERRORS)


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
_ACCEPT_TERMS = "Accept the Terms of Service."
_ACCEPT_PRIVACY = "Accept the Privacy Policy."
_GENDER_DIVISION_MESSAGES = {
    "required": "Choose a gender division.",
    "invalid_choice": "Choose one of the listed options.",
}


# "First Last" is an adult's starting display name, so together they fit it.
SIGNUP_NAME_MAX_LENGTH = Player._meta.get_field("display_name").max_length or 0
_LOCATION_MAX_LENGTH = Player._meta.get_field("city").max_length or 0
_NOT_TEXT = "Enter text."
_FIRST_NAME_MESSAGES = {"required": "Enter your first name."}
_LAST_NAME_MESSAGES = {"required": "Enter your last name."}
_LOCATION_MESSAGES = {
    "max_length": f"Enter at most {_LOCATION_MAX_LENGTH} characters.",
}
# A player aged 13 to 17 starts with "First L." (ADR 0022), so the first
# name leaves room for a space, an initial, and a period.
SIGNUP_FIRST_NAME_MAX_LENGTH = SIGNUP_NAME_MAX_LENGTH - 3
_FIRST_NAME_TOO_LONG = (
    f"Your first name can be at most {SIGNUP_FIRST_NAME_MAX_LENGTH} characters."
)
_NAME_TOO_LONG = (
    f"Your first and last name together can be at most "
    f"{SIGNUP_NAME_MAX_LENGTH - 1} characters."
)


class SignupTextField(forms.CharField):
    """Text from the signup body, with the Player text rules.

    Only a JSON string (or nothing) is text: Django's own conversion would
    turn a number or a list into one. Stripped, as the Player model strips
    it, and refused when it holds a control character or a lone surrogate.
    Messages never repeat the value.
    """

    def to_python(self, value: object) -> str:
        if value not in self.empty_values and not isinstance(value, str):
            raise forms.ValidationError(_NOT_TEXT, code="invalid")
        # Empty input is "", the field's empty value.
        return super().to_python(value) or ""

    def validate(self, value: str) -> None:
        super().validate(value)
        if has_refused_character(value):
            raise forms.ValidationError(CONTROL_CHARACTER_MESSAGE, code="invalid")


class SignupNameField(SignupTextField):
    """A first or last name: required, with a character that renders."""

    def validate(self, value: str) -> None:
        super().validate(value)
        if not has_visible_character(value):
            raise forms.ValidationError(
                self.error_messages["required"], code="required"
            )


class SignupBirthForm(forms.Form):
    """A signup's birth year and month, never a day (ADR 0009, ADR 0017).

    The signup age check (``SignupAgeCheckView``, ADR 0020) takes this form
    alone, and ``SignupAgeForm`` adds the other signup details to it, so both
    answer a missing or malformed age with the same field errors.
    """

    birth_year = forms.IntegerField(
        min_value=EARLIEST_BIRTH_YEAR, error_messages=_BIRTH_YEAR_MESSAGES
    )
    birth_month = forms.IntegerField(
        min_value=1, max_value=12, error_messages=_BIRTH_MONTH_MESSAGES
    )


def signup_full_name(first_name: str, last_name: str) -> str:
    """The legal name (and an adult's starting display name) a signup's
    names give."""
    return f"{first_name} {last_name}"


def _check_name_lengths(form: forms.Form, cleaned: dict[str, Any]) -> None:
    """Add the length errors for a first and last name to ``form``.

    A first name leaves room for "First L.", and "First Last" fits the
    display name (ADR 0022). Fixed messages, never the value.
    """
    first, last = cleaned.get("first_name"), cleaned.get("last_name")
    if first and len(first) > SIGNUP_FIRST_NAME_MAX_LENGTH:
        form.add_error(
            "first_name",
            forms.ValidationError(_FIRST_NAME_TOO_LONG, code="max_length"),
        )
    elif (
        first and last and len(signup_full_name(first, last)) > (SIGNUP_NAME_MAX_LENGTH)
    ):
        form.add_error(
            "last_name", forms.ValidationError(_NAME_TOO_LONG, code="max_length")
        )


class PlayerNamesForm(forms.Form):
    """A first and last name with signup's checks (issue #354, ADR 0062).

    The Player create endpoint's body path, used when the session holds no
    signup details, checks its names with this form, so they follow the
    same rules as at signup.
    """

    first_name = SignupNameField(error_messages=_FIRST_NAME_MESSAGES)
    last_name = SignupNameField(error_messages=_LAST_NAME_MESSAGES)

    def clean(self) -> dict[str, Any]:
        cleaned: dict[str, Any] = super().clean() or {}
        _check_name_lengths(self, cleaned)
        return cleaned


class SignupAgeForm(SignupBirthForm):
    """The signup details on allauth's signup (issues #114, #215, #221, #222).

    ``ACCOUNT_SIGNUP_FORM_CLASS`` names this form, so allauth's signup
    input for both headless clients carries the fields and its OpenAPI
    document lists them, all required but the city and region. A birth year
    and month only, never a day, a gender division (``mens`` or
    ``womens``), never a raw gender (ADR 0009, ADR 0017), the first and
    last name, the chosen city and region, never a street address
    (ADR 0022), and the acceptance of the legal agreements (ADR 0026).
    OPEN's signup view
    (``apps/accounts/headless_views.py``) checks them first and refuses
    anyone under 13.

    allauth imports this class while it builds its own signup form, so this
    module must never import allauth's forms.
    """

    gender_division = forms.ChoiceField(
        choices=GenderDivision.choices, error_messages=_GENDER_DIVISION_MESSAGES
    )
    # The first and last name, required, and the city and region, optional
    # (issue #221, ADR 0022). "First Last" becomes the Player's legal name
    # and an adult's starting display name.
    first_name = SignupNameField(error_messages=_FIRST_NAME_MESSAGES)
    last_name = SignupNameField(error_messages=_LAST_NAME_MESSAGES)
    city = SignupTextField(
        required=False,
        max_length=_LOCATION_MAX_LENGTH,
        error_messages=_LOCATION_MESSAGES,
    )
    region = SignupTextField(
        required=False,
        max_length=_LOCATION_MAX_LENGTH,
        error_messages=_LOCATION_MESSAGES,
    )

    # The legal agreements (issue #222, ADR 0026): both required, and only
    # JSON true accepts. A plain BooleanField, so allauth's OpenAPI document
    # lists a boolean; ``_accepted`` refuses the strings and numbers its
    # conversion would read as true.
    accepted_terms = forms.BooleanField(error_messages={"required": _ACCEPT_TERMS})
    accepted_privacy = forms.BooleanField(error_messages={"required": _ACCEPT_PRIVACY})

    def _accepted(self, name: str) -> bool:
        if self.data.get(name) is not True:
            raise forms.ValidationError(
                self.fields[name].error_messages["required"], code="required"
            )
        return True

    def clean_accepted_terms(self) -> bool:
        return self._accepted("accepted_terms")

    def clean_accepted_privacy(self) -> bool:
        return self._accepted("accepted_privacy")

    def clean(self) -> dict[str, Any]:
        """allauth's signup form calls this through its own ``clean()``."""
        cleaned: dict[str, Any] = super().clean() or {}
        _check_name_lengths(self, cleaned)
        return cleaned

    def signup(self, request: "HttpRequest", user: "AbstractBaseUser") -> None:
        """allauth calls this after saving a new Account. Nothing to store:
        the signup view holds the details in the session."""
