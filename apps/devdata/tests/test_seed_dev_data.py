"""The ``seed_dev_data`` command (#41): made-up data for local development.

Every test runs in the default rolling-back transaction, since verifying
an Organizer and recording a Match write Audit Events.
"""

import sys

import pytest
from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from pytest_django.fixtures import Settings

from apps.clubs.models import Club, ClubOrganizerGrant, Organizer
from apps.events.models import Event
from apps.matches.models import Match, MatchFlag, MatchStatus
from apps.players.models import Player

SEED_MODULE = "apps.devdata.seed"
EXAMPLE_DOMAINS = ("example.com", "example.net", "example.org")


@pytest.fixture
def debug_on(settings: Settings) -> None:
    settings.DEBUG = True


def _counts() -> dict[str, int]:
    return {
        "accounts": get_user_model().objects.count(),
        "players": Player.objects.count(),
        "clubs": Club.objects.count(),
        "organizers": Organizer.objects.count(),
        "events": Event.objects.count(),
        "matches": Match.objects.count(),
    }


@pytest.mark.django_db
@pytest.mark.usefixtures("debug_on")
def test_seeds_empty_database() -> None:
    call_command("seed_dev_data", "--players", "8")

    assert Player.objects.count() == 8
    assert Club.objects.count() == 2
    assert Organizer.objects.count() == 2
    assert not Organizer.objects.filter(verified_at__isnull=True).exists()
    assert Event.objects.count() == 2
    assert Match.objects.count() == 16
    assert Match.objects.exclude(status=MatchStatus.CONFIRMED).count() == 0
    assert Match.objects.counted().count() == 16
    assert MatchFlag.objects.count() == 0
    for match in Match.objects.all():
        assert match.participants.count() in (2, 4)
        assert match.games.exists()
    assert Match.objects.filter(event__isnull=False).exists()
    assert Match.objects.filter(club__isnull=False).exists()
    # ADR 0021: a match with a Club has an Event, and the Club granted the
    # Event's Organizer.
    assert not Match.objects.filter(club__isnull=False, event__isnull=True).exists()
    for match in Match.objects.filter(club__isnull=False).select_related("event"):
        assert match.event is not None
        assert match.club_id is not None
        assert ClubOrganizerGrant.objects.filter(
            club_id=match.club_id, organizer_id=match.event.organizer_id
        ).exists()


@pytest.mark.django_db
@pytest.mark.usefixtures("debug_on")
def test_event_matches_fall_inside_their_event() -> None:
    call_command("seed_dev_data", "--players", "8")

    for match in Match.objects.filter(event__isnull=False).select_related("event"):
        assert match.event is not None
        assert match.event.start_date <= match.played_on <= match.event.end_date


@pytest.mark.django_db
@pytest.mark.usefixtures("debug_on")
def test_prints_a_summary(capsys: pytest.CaptureFixture[str]) -> None:
    call_command("seed_dev_data", "--players", "8")

    out = capsys.readouterr().out
    assert "8 players" in out
    assert "16 confirmed matches" in out


@pytest.mark.django_db
@pytest.mark.usefixtures("debug_on")
def test_players_option() -> None:
    call_command("seed_dev_data", "--players", "5")

    assert Player.objects.count() == 5


@pytest.mark.django_db
@pytest.mark.usefixtures("debug_on")
def test_default_player_count() -> None:
    call_command("seed_dev_data")

    assert Player.objects.count() == 20
    assert Match.objects.counted().count() == 40


@pytest.mark.django_db
@pytest.mark.usefixtures("debug_on")
def test_refuses_fewer_than_four_players() -> None:
    with pytest.raises(CommandError, match="at least 4"):
        call_command("seed_dev_data", "--players", "3")

    assert set(_counts().values()) == {0}


@pytest.mark.django_db
def test_refuses_with_debug_off(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.DEBUG = False
    monkeypatch.delitem(sys.modules, SEED_MODULE, raising=False)

    with pytest.raises(CommandError, match="DJANGO_DEBUG"):
        call_command("seed_dev_data", "--players", "8")

    assert set(_counts().values()) == {0}
    assert SEED_MODULE not in sys.modules


@pytest.mark.django_db
@pytest.mark.usefixtures("debug_on")
def test_emails_are_on_example_domains() -> None:
    call_command("seed_dev_data", "--players", "8")

    emails = list(get_user_model().objects.values_list("email", flat=True))
    assert emails
    for email in emails:
        assert email.rsplit("@", 1)[1] in EXAMPLE_DOMAINS


@pytest.mark.django_db
@pytest.mark.usefixtures("debug_on")
def test_runs_twice() -> None:
    call_command("seed_dev_data", "--players", "8")
    call_command("seed_dev_data", "--players", "8")

    assert Player.objects.count() == 16
    assert Match.objects.counted().count() == 32
    assert MatchFlag.objects.count() == 0


@pytest.mark.django_db
@pytest.mark.usefixtures("debug_on")
def test_missing_dev_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    # Make every factory_boy import fail (a cached submodule would skip
    # its parent), and drop every cached module that imported it, so the
    # seed module and the factories import again.
    for name in list(sys.modules):
        if name == "factory" or name.startswith("factory."):
            monkeypatch.setitem(sys.modules, name, None)
        elif name == SEED_MODULE or name.endswith(".tests.factories"):
            monkeypatch.delitem(sys.modules, name)
    # "from apps.devdata import seed" reads the package attribute first.
    monkeypatch.delattr(SEED_MODULE, raising=False)

    with pytest.raises(CommandError, match="uv sync"):
        call_command("seed_dev_data", "--players", "8")

    assert set(_counts().values()) == {0}
