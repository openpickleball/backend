"""Events: their OPEN Codes, kinds, dates, and links."""

import re
from datetime import date
from typing import Any

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError

from apps.clubs.models import Club, Organizer
from apps.clubs.tests.factories import ClubFactory, OrganizerFactory
from apps.events.models import Event, EventKind

pytestmark = pytest.mark.django_db

EVENT_CODE = re.compile(r"^E-[0-9A-Z]{4}-[0-9A-Z]{4}$")

# A made-up value that must never show up in a validation message.
MARKER = "zqxjmarker"


def _messages(error: ValidationError) -> str:
    return " ".join(
        message for messages in error.message_dict.values() for message in messages
    )


@pytest.fixture
def organizer() -> Organizer:
    made: Organizer = OrganizerFactory.create()
    return made


def _values(organizer: Organizer, **overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "name": "Made-up Spring League",
        "kind": EventKind.LEAGUE,
        "organizer": organizer,
        "start_date": date(2026, 3, 1),
        "end_date": date(2026, 5, 31),
    }
    values.update(overrides)
    return values


def _clean_errors(organizer: Organizer, **overrides: Any) -> ValidationError:
    event = Event(**_values(organizer, **overrides))
    with pytest.raises(ValidationError) as error:
        event.full_clean()
    return error.value


def test_event_gets_an_event_code(organizer: Organizer) -> None:
    event = Event.objects.create(**_values(organizer))

    assert EVENT_CODE.match(event.public_code)


def test_str_is_the_open_code_not_the_name(organizer: Organizer) -> None:
    event = Event.objects.create(**_values(organizer))

    assert str(event) == event.public_code


def test_valid_event_passes_full_clean(organizer: Organizer) -> None:
    Event(**_values(organizer)).full_clean()


def test_club_is_optional(organizer: Organizer) -> None:
    event = Event(**_values(organizer))

    event.full_clean()
    event.save()

    assert event.club is None


def test_event_links_organizer_and_club(organizer: Organizer) -> None:
    club: Club = ClubFactory.create()

    event = Event.objects.create(**_values(organizer, club=club))

    assert list(organizer.events.all()) == [event]
    assert list(club.events.all()) == [event]


def test_kind_values_are_the_engine_match_types_without_self_reported() -> None:
    # The engine's open_engine.MatchType values, minus self_reported (F1).
    assert EventKind.values == ["tournament", "league", "rec_verified"]


@pytest.mark.parametrize("kind", ["self_reported", "rec", MARKER])
def test_full_clean_refuses_other_kinds_without_echo(
    organizer: Organizer, kind: str
) -> None:
    error = _clean_errors(organizer, kind=kind)

    assert "kind" in error.message_dict
    assert kind not in _messages(error)


@pytest.mark.parametrize("kind", ["self_reported", "rec", ""])
def test_database_refuses_other_kinds(organizer: Organizer, kind: str) -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        Event.objects.create(**_values(organizer, kind=kind))


def test_event_has_no_weight() -> None:
    # The engine takes no per-event weight (openpickleball/engine#30, #189).
    field_names = {field.name for field in Event._meta.get_fields()}
    constraint_names = {constraint.name for constraint in Event._meta.constraints}

    assert "weight" not in field_names
    assert "events_event_weight_range" not in constraint_names


@pytest.mark.parametrize("field", ["start_date", "end_date"])
@pytest.mark.parametrize("bad", [MARKER, "2026-02-31"])
def test_bad_date_is_refused_without_echo(
    organizer: Organizer, field: str, bad: str
) -> None:
    error = _clean_errors(organizer, **{field: bad})

    assert field in error.message_dict
    assert bad not in _messages(error)


def test_end_date_before_start_date_is_refused(organizer: Organizer) -> None:
    error = _clean_errors(
        organizer, start_date=date(2026, 5, 2), end_date=date(2026, 5, 1)
    )

    assert "end_date" in error.message_dict


def test_equal_dates_pass(organizer: Organizer) -> None:
    event = Event(
        **_values(organizer, start_date=date(2026, 5, 1), end_date=date(2026, 5, 1))
    )

    event.full_clean()
    event.save()


def test_database_refuses_end_date_before_start_date(organizer: Organizer) -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        Event.objects.create(
            **_values(organizer, start_date=date(2026, 5, 2), end_date=date(2026, 5, 1))
        )


def test_full_clean_strips_name(organizer: Organizer) -> None:
    event = Event(**_values(organizer, name="  Made-up Open  "))

    event.full_clean()

    assert event.name == "Made-up Open"


@pytest.mark.parametrize("name", ["", "   "])
def test_full_clean_refuses_blank_name(organizer: Organizer, name: str) -> None:
    error = _clean_errors(organizer, name=name)

    assert "name" in error.message_dict


@pytest.mark.parametrize("name", ["", "   "])
def test_database_refuses_blank_name(organizer: Organizer, name: str) -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        Event.objects.create(**_values(organizer, name=name))


@pytest.mark.parametrize("bad", ["\x00", "\x07", "\ud800"])
def test_name_refuses_control_characters_without_echo(
    organizer: Organizer, bad: str
) -> None:
    error = _clean_errors(organizer, name=f"{MARKER}{bad}")

    assert "name" in error.message_dict
    assert MARKER not in _messages(error)


def test_name_refuses_non_text(organizer: Organizer) -> None:
    error = _clean_errors(organizer, name=12345)

    assert "name" in error.message_dict


def test_organizer_is_required(organizer: Organizer) -> None:
    values = _values(organizer)
    del values["organizer"]
    event = Event(**values)

    with pytest.raises(ValidationError) as error:
        event.full_clean()

    assert "organizer" in error.value.message_dict


def test_organizer_with_an_event_cannot_be_deleted(organizer: Organizer) -> None:
    Event.objects.create(**_values(organizer))

    with pytest.raises(ProtectedError):
        organizer.delete()

    assert Organizer.objects.filter(pk=organizer.pk).exists()


def test_club_with_an_event_cannot_be_deleted(organizer: Organizer) -> None:
    club: Club = ClubFactory.create()
    Event.objects.create(**_values(organizer, club=club))

    with pytest.raises(ProtectedError):
        club.delete()

    assert Club.objects.filter(pk=club.pk).exists()
