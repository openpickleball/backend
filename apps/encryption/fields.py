"""Model fields that store their values encrypted (ADR 0046).

Each value is stored as a Fernet token in a ``text`` column, encrypted
with the newest key in ``FIELD_ENCRYPTION_KEYS``, and read back as the
field's Python value. Validators, choices, ``max_length`` (validation
only), and error messages work as on the plain field, so forms and
``full_clean()`` are unchanged.

Fernet tokens differ each time the same text is encrypted, so the
database cannot compare them: every lookup but ``isnull`` raises
``FieldError``, rather than an exact match that silently finds nothing.
The database checks nothing about the value either (no CHECK from the
integer field), so the model checks it before saving.
"""

from typing import TYPE_CHECKING, Any, TypeVar

from django.db import models
from django.db.backends.base.base import BaseDatabaseWrapper
from django.db.models.expressions import Expression
from django.db.models.lookups import IsNull, Lookup, Transform

from apps.encryption.keys import decrypt_text, encrypt_text

if TYPE_CHECKING:
    _FieldBase = models.Field[Any, Any]
else:
    _FieldBase = object

# The set and get types, as on Django's fields (django-stubs).
_ST = TypeVar("_ST")
_GT = TypeVar("_GT")


class EncryptedFieldMixin(_FieldBase):
    """Store the field's value as a Fernet token in a text column."""

    def db_type(self, connection: BaseDatabaseWrapper) -> str:
        return "text"

    def db_check(self, connection: BaseDatabaseWrapper) -> None:
        return None

    def get_db_prep_value(
        self, value: Any, connection: BaseDatabaseWrapper, prepared: bool = False
    ) -> Any:
        if not prepared:
            value = self.get_prep_value(value)
        if value is None:
            return None
        return encrypt_text(str(value))

    def from_db_value(
        self, value: str | None, expression: Expression | None, connection: Any
    ) -> Any:
        if value is None:
            return None
        return self.to_python(decrypt_text(value))

    def get_lookup(self, lookup_name: str) -> type[Lookup[Any]] | None:
        if lookup_name == "isnull":
            return IsNull
        return None

    def get_transform(self, lookup_name: str) -> type[Transform] | None:
        return None


class EncryptedCharField(EncryptedFieldMixin, models.CharField[_ST, _GT]):
    """A ``CharField`` stored encrypted."""


class EncryptedPositiveSmallIntegerField(
    EncryptedFieldMixin, models.PositiveSmallIntegerField[_ST, _GT]
):
    """A ``PositiveSmallIntegerField`` stored encrypted, read back as an int."""
