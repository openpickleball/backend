"""The ``seed_dev_data`` command (#41): made-up data for local development.

Every test runs in the default rolling-back transaction, since verifying
an Organizer and recording a Match write Audit Events.
"""

import sys

import pytest
from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.db import connection
from pytest_django.fixtures import Settings

from apps.clubs.models import Club, ClubOrganizerGrant, Organizer
from apps.encryption.keys import is_token
from apps.events.models import Event
from apps.matches.models import FlagReason, FlagRule, Match, MatchFlag, MatchStatus
from apps.players.models import GenderDivision, Player

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
    assert not MatchFlag.objects.filter(reason=FlagReason.POSSIBLE_DUPLICATE).exists()
    # Each seeded Organizer is newly verified, so every match is among its
    # first and carries one signal-only first_matches flag (#206), and
    # still counts.
    assert MatchFlag.objects.count() == 16
    assert (
        MatchFlag.objects.filter(
            reason=FlagReason.UNUSUAL_SUBMISSION, rule=FlagRule.FIRST_MATCHES
        )
        .values("match")
        .distinct()
        .count()
        == 16
    )
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
    assert not MatchFlag.objects.filter(reason=FlagReason.POSSIBLE_DUPLICATE).exists()
    # Every match is among its new Organizer's first (#206).
    assert (
        MatchFlag.objects.filter(rule=FlagRule.FIRST_MATCHES)
        .values("match")
        .distinct()
        .count()
        == 32
    )


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


@pytest.mark.django_db
@pytest.mark.usefixtures("debug_on")
def test_players_get_made_up_personal_data_stored_encrypted() -> None:
    # The factories set these (issue #115): a made-up legal name built from
    # the first and last name (ADR 0062), and a made-up adult birth year and
    # month and division. The columns hold tokens.
    call_command("seed_dev_data", "--players", "8")

    players = list(
        Player.objects.values_list(
            "legal_name",
            "first_name",
            "last_name",
            "birth_year",
            "birth_month",
            "gender_division",
        )
    )
    assert len(players) == 8
    for legal_name, first, last, birth_year, birth_month, division in players:
        assert legal_name == f"{first} {last}"
        assert isinstance(birth_year, int)
        assert birth_month is not None
        assert 1 <= birth_month <= 12
        assert division in GenderDivision.values
    with connection.cursor() as cursor:
        cursor.execute("SELECT birth_year, gender_division FROM players_player")
        for row in cursor.fetchall():
            assert all(is_token(value) for value in row)
