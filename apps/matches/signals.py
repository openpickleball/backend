"""Signals the match workflow sends when what counts changes (#26).

``match_confirmed`` is sent with ``sender=Match`` and ``match``, the Match
that became confirmed: a pending match confirmed by both teams or by an
organizer, a match created confirmed by ``record_match()``, an existing
pending match confirmed by a permitted duplicate submission, a disputed
match a moderator or the Event's verified Organizer resolved as confirmed,
and a confirmed correction of a match that was not counting (such as a
disputed match corrected by a moderator or the Organizer). It is sent once
per confirmation, never for a repeated confirm.

``match_changed`` is sent with ``sender=Match``, ``match``,
``earliest_date`` (the earliest ``played_on`` whose ratings the change can
affect), and ``change``, a ``MatchChange``:

- ``void``, ``edit``, ``dispute``: a match that was counting was voided,
  corrected (``match`` is the old one; ``earliest_date`` is the earlier of
  the old and new dates), or disputed;
- ``hold``: a match that was counting is now held by a new possible
  duplicate flag;
- ``release``: a match held by a possible duplicate flag counts again,
  because the match that held it was voided or because a moderator
  resolved the flag (``resolve_flags``, also through ``approve_match``).

The workflow sends at most one ``match_changed`` per match per call.

Timing contract: both signals are sent synchronously with ``Signal.send``
inside the transaction that made the change, after the rows and the Audit
Event are written. A receiver may write rows in that transaction (the
webhook outbox, #85) and should enqueue work with
``transaction.on_commit`` (rating recalculation, #46 and #47). A receiver
that raises rolls the whole change back, and a rolled back change leaves
nothing of its receivers' work.
"""

from enum import StrEnum

from django.dispatch import Signal


class MatchChange(StrEnum):
    """Why ``match_changed`` was sent."""

    VOID = "void"
    EDIT = "edit"
    DISPUTE = "dispute"
    HOLD = "hold"
    RELEASE = "release"


match_confirmed = Signal()
match_changed = Signal()
