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
