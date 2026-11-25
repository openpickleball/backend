"""A non-integer organizer or club id is refused without echo (#163 review).

``ForeignKey.to_python`` converts the id with the target's primary key
field, whose ``invalid`` message repeats the submitted value. ``save_event``
runs ``full_clean()``, so the Event gives each link's own fixed message
instead, as Match does (CLAUDE.md, API). Made-up data only.
"""

import pytest
from django.core.exceptions import ValidationError

from apps.accounts.tests.factories import UserFactory
from apps.events.services import save_event
from apps.events.tests.factories import EventFactory

pytestmark = pytest.mark.django_db

MARKER = "zqxjmarker"


@pytest.mark.parametrize(
    ("column", "field", "message"),
    [
        ("organizer_id", "organizer", "Choose an existing organizer."),
        ("club_id", "club", "Choose an existing club."),
    ],
)
def test_save_event_refuses_a_non_integer_id_without_echo(
    column: str, field: str, message: str
) -> None:
    event = EventFactory.create()
    setattr(event, column, MARKER)

    with pytest.raises(ValidationError) as caught:
        save_event(event, actor=UserFactory.create(is_staff=True))

    assert caught.value.message_dict[field] == [message]
    assert MARKER not in str(caught.value)
