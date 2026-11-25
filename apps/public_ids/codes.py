"""OPEN Codes: the public identifiers of players, clubs, events, organizers,
matches, and results files.

A code is a type prefix, then random characters and one check character
from Crockford's base32 alphabet, in groups of four: ``P-7K3M-QX9H``. The
check character is Luhn mod 32 over the prefix and the random characters,
so one wrong character, or a code moved to another type, fails the check.

These are pure functions. ``apps.public_ids.models`` issues and stores
codes. See ADR 0008.
"""

import enum
import secrets

ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_BASE = len(ALPHABET)
_GROUP = 4
# Longer input is refused before any other work.
_MAX_INPUT = 64
# Letters people type for digits. No prefix is one of these.
_LOOKALIKES = str.maketrans({"I": "1", "L": "1", "O": "0"})


class CodeType(enum.Enum):
    """Each kind of record with a public code: its prefix and length.

    ``length`` counts the random characters and the check character. It
    does not count the prefix.
    """

    PLAYER = ("P", 8)
    CLUB = ("C", 8)
    EVENT = ("E", 8)
    ORGANIZER = ("G", 8)
    MATCH = ("M", 12)
    RESULTS_FILE = ("F", 8)

    @property
    def prefix(self) -> str:
        prefix: str = self.value[0]
        return prefix

    @property
    def length(self) -> int:
        length: int = self.value[1]
        return length


def _luhn_sum(text: str, factor: int) -> int:
    total = 0
    for char in reversed(text):
        addend = factor * ALPHABET.index(char)
        factor = 1 if factor == 2 else 2
        total += addend // _BASE + addend % _BASE
    return total


def check_character(text: str) -> str:
    """The Luhn mod 32 check character for ``text`` (prefix and random part)."""
    return ALPHABET[(_BASE - _luhn_sum(text, 2) % _BASE) % _BASE]


def is_valid(text: str) -> bool:
    """Whether ``text`` ends in the right check character.

    ``text`` must use only alphabet characters.
    """
    return _luhn_sum(text, 1) % _BASE == 0


def format_code(code_type: CodeType, body: str) -> str:
    """The canonical form: prefix, then the body in groups of four."""
    body = body.upper()
    groups = [body[start : start + _GROUP] for start in range(0, len(body), _GROUP)]
    return "-".join([code_type.prefix, *groups])


def generate(code_type: CodeType) -> str:
    """A new random code in canonical form, drawn from ``secrets``."""
    random_part = "".join(secrets.choice(ALPHABET) for _ in range(code_type.length - 1))
    check = check_character(code_type.prefix + random_part)
    return format_code(code_type, random_part + check)


def normalize(raw: str, code_type: CodeType) -> str | None:
    """The canonical form of ``raw`` as a code of ``code_type``, or None.

    Accepts any case, spaces and hyphens anywhere, a missing prefix, and
    I, L, or O typed for 1 or 0. Returns None for anything else: the wrong
    prefix, the wrong length, a character outside the alphabet, or a
    failed check. It never raises for bad input.
    """
    # Refuse non-ASCII before changing case: uppercasing some Unicode
    # letters (a dotless i) gives an ASCII letter.
    if not isinstance(raw, str) or len(raw) > _MAX_INPUT or not raw.isascii():
        return None
    compact = raw.upper().replace(" ", "").replace("-", "")
    if len(compact) == code_type.length + 1:
        if compact[0] != code_type.prefix:
            return None
        body = compact[1:]
    elif len(compact) == code_type.length:
        body = compact
    else:
        return None
    body = body.translate(_LOOKALIKES)
    if any(char not in ALPHABET for char in body):
        return None
    if not is_valid(code_type.prefix + body):
        return None
    return format_code(code_type, body)
