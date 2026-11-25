"""An Organizer Report's text names neither the Organizer nor the reporter (#271).

Django lists cascaded rows by ``str()``, for example on the Account
admin's delete confirmation page, so a report's text must not say which
Organizer the Account reported. Made-up data only.
"""

import pytest

from apps.clubs.models import OrganizerReportReason
from apps.clubs.reports import report_organizer
from apps.clubs.tests.factories import verified_organizer
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db


def test_str_names_neither_the_organizer_nor_the_reporter() -> None:
    organizer = verified_organizer(name="Made-up Organizer Person")
    player: Player = PlayerFactory.create()
    report = report_organizer(
        organizer.pk, actor=player.account, reason=OrganizerReportReason.OTHER
    )

    text = str(report)

    assert organizer.public_code not in text
    assert organizer.name not in text
    assert player.account.email not in text
    assert player.public_code not in text
    assert player.display_name not in text
