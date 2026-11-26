"""Email normalization and validation for Account emails.

The single rule: normalize first, then validate the normalized value, which
is what clean() and the manager store. Every check (ASCII-only, length,
format) runs on that value.
"""

import unicodedata

from django.core.exceptions import ValidationError
from django.core.validators import validate_email

ASCII_EMAIL_MESSAGE: str = "Email addresses may contain only ASCII characters."
# The single source of the limit: User.email uses it as max_length too.
EMAIL_MAX_LENGTH: int = 254
EMAIL_LENGTH_MESSAGE: str = (
    f"Email addresses may be at most {EMAIL_MAX_LENGTH} characters."
)


def normalize_email_address(email: str | None) -> str:
    """NFKC-normalize, strip surrounding whitespace, and lowercase.

    NFKC matches what AbstractBaseUser.clean() applies on the form path,
    so the manager and forms store the same value.
    """
    email = unicodedata.normalize("NFKC", email or "").strip()
    # Lowercase one character at a time. str.lower() on the whole string
    # applies the context-sensitive final-sigma rule (capital sigma at a
    # word end becomes U+03C2), which Postgres LOWER() does not, so the
    # stored value and the Lower("email") constraint would disagree.
    return "".join(c.lower() for c in email)


def _format_errors(email: str) -> list[ValidationError]:
    try:
        validate_email(email)
    except ValidationError as error:
        return [error]
    return []


def normalized_email_errors(normalized: str) -> list[ValidationError]:
    """Return every rule an already-normalized address breaks, in order.

    - ASCII-only: lookalike (homoglyph) characters such as U+0131 dotless i
      could otherwise create a second account that reads like an existing
      one. Characters NFKC folds to ASCII (U+017F long s) are accepted.
    - Length: NFKC can expand an address (U+FB00 becomes "ff") past the
      column size that max_length checked on the raw value.
    - Format: Django's EmailValidator, because the expansion can also turn a
      valid raw address into a malformed one (a domain label over 63).
    """
    errors = []
    if not normalized.isascii():
        errors.append(ValidationError(ASCII_EMAIL_MESSAGE, code="email_not_ascii"))
    if len(normalized) > EMAIL_MAX_LENGTH:
        errors.append(ValidationError(EMAIL_LENGTH_MESSAGE, code="email_too_long"))
    errors.extend(_format_errors(normalized))
    return errors


def is_canonical_email(email: str) -> bool:
    """Return whether an address is already in its stored form, give or take
    case and surrounding whitespace.

    It is canonical when it is ASCII once stripped, stripping and
    lowercasing it gives the same value as full normalization (so NFKC
    changes nothing, as it would for U+017F long s or fullwidth letters),
    and that value passes every rule. Only a canonical address may identify
    an existing Account (login, password reset): a variant that NFKC or
    lowercasing folds onto an Account's address is a different string, not
    that Account's login.
    """
    stripped = email.strip()
    # Stored addresses are ASCII. Checking the input too refuses U+212A
    # KELVIN SIGN, whose lowercase is an ASCII "k".
    if not stripped.isascii():
        return False
    normalized = normalize_email_address(email)
    lowered = "".join(c.lower() for c in stripped)
    return lowered == normalized and not normalized_email_errors(normalized)


def validate_normalized_email(value: str) -> None:
    """Field validator: normalize, then apply every rule to the result.

    EmailField already runs Django's EmailValidator on the raw value, so a
    format message that the raw check also produces is dropped here, and
    identical messages are reported once.
    """
    raw_messages = {m for error in _format_errors(value) for m in error.messages}
    seen = set(raw_messages)
    errors = []
    for error in normalized_email_errors(normalize_email_address(value)):
        message = error.messages[0]
        if message not in seen:
            seen.add(message)
            errors.append(error)
    if errors:
        raise ValidationError(errors)


# A password Python cannot encode as UTF-8 (JSON allows a lone surrogate)
# cannot be hashed. Fixed text: never the submitted value.
PASSWORD_NOT_TEXT_MESSAGE = "Use only letters, numbers, spaces, and symbols."
PASSWORD_NOT_TEXT_CODE = "password_invalid_characters"


def is_encodable_password(password: object) -> bool:
    """Whether ``password`` is a string Python can encode as UTF-8."""
    if not isinstance(password, str):
        return False
    try:
        password.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


class EncodablePasswordValidator:
    """Refuses a password that is not valid UTF-8 text (#100), so it never
    reaches the hasher. First in ``AUTH_PASSWORD_VALIDATORS``."""

    def validate(self, password: str, user: object = None) -> None:
        if not is_encodable_password(password):
            raise ValidationError(
                PASSWORD_NOT_TEXT_MESSAGE, code=PASSWORD_NOT_TEXT_CODE
            )

    def get_help_text(self) -> str:
        return "Your password must be text: letters, numbers, spaces, and symbols."
