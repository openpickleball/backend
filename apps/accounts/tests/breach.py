"""A stand-in for the breached password service in tests (#100).

The root ``conftest.py`` puts a ``FakeBreachService`` in place of
``apps.accounts.breached.fetch_range`` for every test, so no test reaches
the network. By default it knows no breached password. A test takes the
``breach_service`` fixture to mark passwords as breached, to make the
service fail, or to answer with a given body. Made-up passwords only.
"""

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field

# A padding entry, as the real service adds them: count 0.
PADDING_LINE = "0" * 35 + ":0"


def sha1_hex(password: str) -> str:
    """The password's uppercase SHA-1, as the range API lists it."""
    return hashlib.sha1(password.encode(), usedforsecurity=False).hexdigest().upper()


@dataclass
class FakeBreachService:
    """Answers range requests like the real service, from made-up data."""

    original: Callable[[str], str]
    breached: dict[str, int] = field(default_factory=dict)
    error: Exception | None = None
    body: str | None = None
    prefixes: list[str] = field(default_factory=list)

    def add(self, password: str, count: int = 3) -> None:
        """Mark ``password`` as seen ``count`` times in breaches."""
        self.breached[password] = count

    def __call__(self, prefix: str) -> str:
        self.prefixes.append(prefix)
        if self.error is not None:
            raise self.error
        if self.body is not None:
            return self.body
        lines = [PADDING_LINE]
        for password, count in self.breached.items():
            digest = sha1_hex(password)
            if digest[:5] == prefix:
                lines.append(f"{digest[5:]}:{count}")
        return "\r\n".join(lines)
