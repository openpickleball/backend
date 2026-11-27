"""Players and their privacy settings. See ADR 0002 and ADR 0009.

Create a Player through ``apps.players.services.create_player``, which
also creates its PlayerPrivacy row. When the birth year or month is set
or changed, the player must be 13 or older (``apps.players.age``, issue
#114). Only the player changes these fields and settings, through the
services in that module.
"""

import unicodedata
from collections.abc import Collection
from typing import TYPE_CHECKING, Any, ClassVar

from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.clubs.models import _LinkMessagesModel
from apps.encryption.fields import (
    EncryptedCharField,
    EncryptedPositiveSmallIntegerField,
)
from apps.players.age import is_old_enough
from apps.public_ids.codes import CodeType
from apps.public_ids.models import PublicCodeModel

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

# The under-13 refusal: fixed, with no age in it (issue #114). The Player
# create endpoint matches it to refuse the session (issue #220).
UNDER_MINIMUM_AGE_MESSAGE = "This birth year and month can't be used."
# A future birth year and month. The Player create endpoint refuses the
# session for it too, as signup does (issue #225).
FUTURE_BIRTH_MONTH_MESSAGE = "The birth year and month cannot be in the future."
# Set once, when the Player is created, and never changed by the player
# after that (issue #225). Fixed messages, never the submitted value.
SET_ONCE_MESSAGES = {
    "birth_year": "Your birth year and month can't be changed.",
    "birth_month": "Your birth year and month can't be changed.",
    "gender_division": "Your gender division can't be changed.",
}
SET_ONCE_FIELDS: tuple[str, ...] = tuple(SET_ONCE_MESSAGES)
SET_ONCE_CODE = "set_once"
# Messages never include the submitted value (CLAUDE.md, API). Django's
# defaults for these keys do.
_CHOICE_MESSAGES = {"invalid_choice": "Choose one of the listed options."}
_GENDER_DIVISION_REQUIRED = "Choose a gender division."
_GENDER_DIVISION_MESSAGES = {
    **_CHOICE_MESSAGES,
    "blank": _GENDER_DIVISION_REQUIRED,
    "null": _GENDER_DIVISION_REQUIRED,
}
_WHOLE_NUMBER = "Enter a whole number."
_INTEGER_MESSAGES = {"invalid": _WHOLE_NUMBER}
_BIRTH_REQUIRED = "This field is required."
CONTROL_CHARACTER_MESSAGE = "Remove control or invalid characters."
# Control characters (Postgres cannot store NUL) and lone surrogates
# (not encodable as UTF-8).
_REFUSED_CATEGORIES = frozenset({"Cc", "Cs"})
# Letters and symbols that render blank.
_FILLERS = frozenset({"\u115f", "\u1160", "\u3164", "\uffa0", "\u2800", "\U0001d159"})
# Letters, numbers, punctuation, and symbols, plus characters unassigned
# in Python's Unicode data (Cn): newer scripts and emoji land there until
# Python catches up.
_VISIBLE_CATEGORY_GROUPS = ("L", "N", "P", "S", "Cn")

# Text fields stripped and checked for control characters before field
# validation, so max_length counts the stored value.
TEXT_FIELDS = ("display_name", "legal_name", "city", "region")
# Optional text fields where None means cleared, stored as "".
OPTIONAL_TEXT_FIELDS = ("legal_name", "city", "region")
# Fields that take only text (or None). Django's own conversion calls
# str(), which turns any value into text and raises ValueError on an int
# past Python's digit limit.
STRING_FIELDS = (*TEXT_FIELDS, "gender_division")
_NOT_TEXT = "Enter text."
BIRTH_FIELDS = ("birth_year", "birth_month")

EARLIEST_BIRTH_YEAR = 1900
# Fields save() checks, since the database cannot (ADR 0046): their
# columns are text, with no length limit or check constraint.
ENCRYPTED_TEXT_FIELDS = ("legal_name", "gender_division")
ENCRYPTED_CHECKED_FIELDS = (*BIRTH_FIELDS, *ENCRYPTED_TEXT_FIELDS)

# One setting per profile detail other than the display name, which has
# none (ADR 0002). ``rating_visibility`` covers current Ratings (#53): it
# is last so the earlier order is kept.
PRIVACY_SETTINGS = (
    "city_region_visibility",
    "match_history_visibility",
    "rating_history_visibility",
    "stats_visibility",
    "home_club_visibility",
    "rating_visibility",
)


class Visibility(models.TextChoices):
    """Who may see a profile detail. ``PRIVATE`` is the most restrictive."""

    SIGNED_IN = "signed_in", "Signed-in players"
    PRIVATE = "private", "Only me"


class GenderDivision(models.TextChoices):
    """The gender division a Player enters (issue #215).

    Every new Player has one. The column still allows an empty value,
    only so that a later removal (#190, #116) can clear it.
    """

    MENS = "mens", "Men's"
    WOMENS = "womens", "Women's"


# The values save() accepts: a division, or "" for a later removal.
_STORED_DIVISIONS = frozenset({"", *GenderDivision.values})


class Player(_LinkMessagesModel, PublicCodeModel):
    """A person who plays matches and has ratings (CONTEXT.md, Player).

    Every Player belongs to one Account, and an Account has at most one
    Player (ADR 0002). The fields are the ones ADR 0009 allows, and no
    others: never a full birthdate or a raw gender.
    """

    public_code_type = CodeType.PLAYER

    # PROTECT: deleting an Account keeps a Player that is in the Ledger,
    # with its personal data cleared, linked to the kept, cleared Account
    # (ADR 0002, ADR 0048).
    account = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="player",
        error_messages={"invalid": "Choose an existing account."},
    )
    display_name = models.CharField(max_length=64)
    # Legal name, birth year and month, and gender division are stored
    # encrypted (ADR 0046). The database cannot check them, so save()
    # applies their rules (_check_encrypted_fields).
    legal_name = EncryptedCharField(max_length=200, blank=True)
    # Required for every new Player (blank=False, so full_clean refuses
    # None). NULL only after Account deletion removes them (#116).
    birth_year = EncryptedPositiveSmallIntegerField(
        null=True,
        validators=[MinValueValidator(EARLIEST_BIRTH_YEAR)],
        error_messages=_INTEGER_MESSAGES,
    )
    # Never the day (ADR 0009).
    birth_month = EncryptedPositiveSmallIntegerField(
        null=True,
        validators=[MinValueValidator(1), MaxValueValidator(12)],
        error_messages=_INTEGER_MESSAGES,
    )
    # Required for every new Player (issue #215). save() still allows "",
    # for a later removal (#190, #116).
    gender_division = EncryptedCharField(
        max_length=8,
        choices=GenderDivision,
        error_messages=_GENDER_DIVISION_MESSAGES,
    )
    city = models.CharField(max_length=100, blank=True)
    region = models.CharField(max_length=100, blank=True)

    class Meta:
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                # Refuses "" and whitespace only.
                condition=~Q(display_name__regex=r"^\s*$"),
                name="players_player_display_name_not_empty",
            ),
        ]
        permissions = (
            (
                "view_personal_data",
                "Can view a player's legal name, birth year and month, "
                "and gender division",
            ),
            (
                "remove_underage_player",
                "Can remove the personal data of a player found to be under 13",
            ),
        )

    def __str__(self) -> str:
        # The OPEN Code, never a name (ADR 0009).
        return self.public_code

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Check the encrypted fields, then save.

        The database cannot check an encrypted value, so this applies the
        rules its check constraints did (ADR 0046) on every save, to the
        fields an ``update_fields`` save names. Raw writes
        (``QuerySet.update``, ``bulk_create``, raw SQL, ``loaddata``) skip
        it. ``update_fields`` is read once, so a one-shot iterable reaches
        the check and Django's save as the same set. A new Player without a
        birth year or month is refused: the columns allow NULL only for
        Account deletion (ADR 0048).
        """
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            update_fields = frozenset(update_fields)
            kwargs["update_fields"] = update_fields
        self._check_encrypted_fields(
            ENCRYPTED_CHECKED_FIELDS if update_fields is None else update_fields
        )
        if self._state.adding:
            # The columns allow NULL only for Account deletion (ADR 0048).
            missing = {
                name: [ValidationError(_BIRTH_REQUIRED)]
                for name in BIRTH_FIELDS
                if getattr(self, name) is None
            }
            if missing:
                raise ValidationError(missing)
        super().save(*args, **kwargs)

    def _check_encrypted_fields(self, fields: Collection[str]) -> None:
        """Refuse a bad birth year or month, legal name, or gender division.

        A birth year or month must be an int in range, a legal name text
        within the field's ``max_length``, and a gender division text and
        one of the stored values (each within its ``max_length``). A None value is
        left alone: the columns allow NULL only for Account deletion, and
        ``save()`` refuses a new Player without a birth year or month. Messages are the model's fixed
        ones, never the value.
        """
        errors: dict[str, list[ValidationError]] = {}
        for name in ENCRYPTED_CHECKED_FIELDS:
            value = getattr(self, name)
            if name not in fields or value is None:
                continue
            if name in BIRTH_FIELDS and not _is_int(value):
                errors[name] = [ValidationError(_WHOLE_NUMBER)]
                continue
            if name in ENCRYPTED_TEXT_FIELDS and not isinstance(value, str):
                errors[name] = [ValidationError(_NOT_TEXT)]
                continue
            if name == "gender_division":
                # Every stored value is within max_length.
                if value not in _STORED_DIVISIONS:
                    errors[name] = [ValidationError(_CHOICE_MESSAGES["invalid_choice"])]
                continue
            field = self._meta.get_field(name)
            assert isinstance(field, models.Field)
            try:
                field.run_validators(value)
            except ValidationError as error:
                errors[name] = error.error_list
        if errors:
            raise ValidationError(errors)

    def clean_fields(self, exclude: Collection[str] | None = None) -> None:
        """Normalize and check the input before Django's field validation.

        None clears an optional text field. A text field refuses a value
        that is not a string (None aside). Text is stripped first, so
        max_length counts the stored value. Control characters and
        lone surrogates are refused: Postgres cannot store NUL, and a
        surrogate cannot be encoded. Birth year and month
        must be real ints: Django's own conversion would truncate a float,
        accept a bool, or overflow on infinity.
        """
        skip = set(exclude or ())
        errors: dict[str, list[ValidationError]] = {}
        for name in OPTIONAL_TEXT_FIELDS:
            if name not in skip and getattr(self, name) is None:
                setattr(self, name, "")
        for name in STRING_FIELDS:
            value = getattr(self, name)
            if name not in skip and value is not None and not isinstance(value, str):
                errors[name] = [ValidationError(_NOT_TEXT)]
        for name in TEXT_FIELDS:
            value = getattr(self, name)
            if name in skip or name in errors or not isinstance(value, str):
                continue
            value = value.strip()
            setattr(self, name, value)
            if has_refused_character(value):
                errors[name] = [ValidationError(CONTROL_CHARACTER_MESSAGE)]
        for name in BIRTH_FIELDS:
            value = getattr(self, name)
            if name in skip or value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int):
                errors[name] = [ValidationError(_WHOLE_NUMBER)]
        try:
            super().clean_fields(exclude=skip | set(errors))
        except ValidationError as error:
            errors = error.update_error_dict(errors)
        if errors:
            raise ValidationError(errors)

    def clean(self) -> None:
        self._clean_rules(None)

    def validate_changes(self, fields: Collection[str]) -> None:
        """Like ``full_clean()``, with the content rules on ``fields`` only.

        A profile change uses this, so a stored value that a later rule (or
        newer Unicode data) refuses does not block an edit to another
        field. Fields not in ``fields`` get no field or content checks.
        Uniqueness and database constraints still run on the whole record,
        skipping only fields that failed the checks above.
        """
        changed = set(fields)
        unchanged = [
            field.name
            for field in self._meta.concrete_fields
            if field.name not in changed
        ]
        errors: dict[str, list[ValidationError]] = {}
        try:
            self.clean_fields(exclude=unchanged)
        except ValidationError as error:
            errors = error.update_error_dict(errors)
        try:
            self._clean_rules(changed)
        except ValidationError as error:
            errors = error.update_error_dict(errors)
        try:
            self.validate_unique(exclude=list(errors))
        except ValidationError as error:
            errors = error.update_error_dict(errors)
        try:
            self.validate_constraints(exclude=list(errors))
        except ValidationError as error:
            errors = error.update_error_dict(errors)
        if errors:
            raise ValidationError(errors)

    def _clean_rules(self, fields: Collection[str] | None) -> None:
        """The model-level rules, for ``fields`` only, or all when None."""
        errors: dict[str, str] = {}
        name_changed = fields is None or "display_name" in fields
        if name_changed and not has_visible_character(self.display_name):
            errors["display_name"] = "Enter a display name."
        birth_changed = fields is None or not set(BIRTH_FIELDS).isdisjoint(fields)
        if birth_changed and self._birth_month_is_in_the_future():
            errors["birth_month"] = FUTURE_BIRTH_MONTH_MESSAGE
        elif birth_changed and self._is_under_minimum_age():
            # A fixed message, with no age in it (issue #114).
            errors["birth_month"] = UNDER_MINIMUM_AGE_MESSAGE
        if errors:
            raise ValidationError(errors)

    def _birth_month_is_in_the_future(self) -> bool:
        year, month = self.birth_year, self.birth_month
        if not _is_int(year) or not _is_int(month) or not 1 <= month <= 12:
            # Field validation reports a missing or malformed value, so a
            # month out of range never gets the future message, which the
            # Player create endpoint treats as a refusal (issue #225).
            return False
        today = timezone.localdate()
        return (year, month) > (today.year, today.month)

    def _is_under_minimum_age(self) -> bool:
        """Under 13 on the age limit date (``apps.players.age``)."""
        year, month = self.birth_year, self.birth_month
        if not _is_int(year) or not _is_int(month) or not 1 <= month <= 12:
            # Field validation reports a missing or malformed value.
            return False
        return not is_old_enough(year, month)


def normalize_input(name: str, value: object) -> object:
    """``value`` for field ``name`` after the normalization validation applies.

    None becomes "" for an optional text field, and text fields are
    stripped. A profile change compares the result with the stored value
    to tell whether a submitted field changed.
    """
    if value is None and name in OPTIONAL_TEXT_FIELDS:
        return ""
    if name in TEXT_FIELDS and isinstance(value, str):
        return value.strip()
    return value


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def has_refused_character(value: str) -> bool:
    """True when ``value`` holds a control character or a lone surrogate.

    Postgres cannot store NUL, and a surrogate cannot be encoded as UTF-8.
    """
    return any(unicodedata.category(char) in _REFUSED_CATEGORIES for char in value)


def has_visible_character(value: object) -> bool:
    """True when ``value`` has at least one character that renders.

    That is a letter, number, punctuation mark, or symbol (L*, N*, P*,
    S*), or a character unassigned in Python's Unicode data (Cn, which
    holds newer scripts and emoji), other than a filler that renders
    blank. Spaces, control and format characters, combining marks, and
    private-use characters do not count on their own, though they may
    appear next to one that does.
    """
    if not isinstance(value, str):
        return False
    return any(
        unicodedata.category(char).startswith(_VISIBLE_CATEGORY_GROUPS)
        and char not in _FILLERS
        for char in value
    )


class PlayerPrivacy(_LinkMessagesModel, models.Model):
    """Who may see each of a Player's other profile details.

    There is no setting for the display name: once Ratings are published,
    every signed-in Account sees it (ADR 0002). ``rating_visibility``
    covers current Ratings (#53): a player created at 13 to 17 starts
    private and stays private at 18 until the player widens it, and an
    adult's is always signed-in (``apps.players.services.update_privacy``
    enforces both). Its model default is private, so a row made outside
    ``create_player()`` fails closed; ``create_player()`` sets it for an
    adult.
    """

    SETTINGS: ClassVar[tuple[str, ...]] = PRIVACY_SETTINGS

    player = models.OneToOneField(
        Player,
        on_delete=models.CASCADE,
        primary_key=True,
        related_name="privacy",
        error_messages={"invalid": "Choose an existing player."},
    )
    city_region_visibility = models.CharField(
        max_length=16,
        choices=Visibility,
        default=Visibility.SIGNED_IN,
        error_messages=_CHOICE_MESSAGES,
    )
    match_history_visibility = models.CharField(
        max_length=16,
        choices=Visibility,
        default=Visibility.SIGNED_IN,
        error_messages=_CHOICE_MESSAGES,
    )
    rating_history_visibility = models.CharField(
        max_length=16,
        choices=Visibility,
        default=Visibility.SIGNED_IN,
        error_messages=_CHOICE_MESSAGES,
    )
    stats_visibility = models.CharField(
        max_length=16,
        choices=Visibility,
        default=Visibility.SIGNED_IN,
        error_messages=_CHOICE_MESSAGES,
    )
    home_club_visibility = models.CharField(
        max_length=16,
        choices=Visibility,
        default=Visibility.SIGNED_IN,
        error_messages=_CHOICE_MESSAGES,
    )
    rating_visibility = models.CharField(
        max_length=16,
        choices=Visibility,
        default=Visibility.PRIVATE,
        error_messages=_CHOICE_MESSAGES,
    )

    class Meta:
        verbose_name = "player privacy settings"
        verbose_name_plural = "player privacy settings"
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=Q(**{f"{name}__in": Visibility.values}),
                name=f"players_playerprivacy_{name}_valid",
            )
            for name in PRIVACY_SETTINGS
        ]

    def __str__(self) -> str:
        # The OPEN Code, never a name (ADR 0009).
        return self.player.public_code

    def visible_to(self, setting: str, viewer: "_User | AnonymousUser | None") -> bool:
        """True when ``viewer`` may see the detail ``setting`` covers.

        Nobody signed out or inactive sees anything. The player's own
        Account always does. Another Account does only at ``SIGNED_IN``,
        and only while the player's Account is active: ADR 0002 shows a
        Player's name only then, so its other details must not outlast it.
        """
        if setting not in self.SETTINGS:
            raise ValueError(f"{setting} is not a privacy setting.")
        if viewer is None or not viewer.is_authenticated or not viewer.is_active:
            return False
        if viewer.pk == self.player.account_id:
            return True
        if not self.player.account.is_active:
            return False
        return bool(getattr(self, setting) == Visibility.SIGNED_IN)
