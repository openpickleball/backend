"""No model, fixture, signup field, or session value stores a full
birthdate or a raw gender (ADR 0009, issue #114)."""

from collections.abc import Iterable, Iterator
from importlib import import_module
from pathlib import Path

import pytest
from allauth.headless.account.inputs import SignupInput
from django.apps import apps
from django.conf import settings
from django.core.cache import cache
from django.db import models
from django.test import Client
from django.test.utils import isolate_apps

from apps.accounts.forms import SignupAgeForm
from apps.accounts.tests.headless import APP, signup
from apps.players.signup import SIGNUP_DETAILS_SESSION_KEY

APPS_DIR = Path(__file__).resolve().parents[2]

RAW_GENDER_NAMES = {"gender", "sex"}


def _offending_fields(model_classes: Iterable[type[models.Model]]) -> list[str]:
    found: list[str] = []
    for model in model_classes:
        for field in model._meta.get_fields():
            name = field.name.lower()
            label = f"{model._meta.label}.{field.name}"
            if isinstance(field, models.DateField) and "birth" in name:
                # DateTimeField is a DateField subclass.
                found.append(label)
            if name in RAW_GENDER_NAMES:
                found.append(label)
    return found


def test_no_installed_model_has_a_birthdate_or_raw_gender_field() -> None:
    assert _offending_fields(apps.get_models()) == []


def test_player_stores_birth_year_and_month_only() -> None:
    player = apps.get_model("players", "Player")
    names = {field.name for field in player._meta.get_fields()}

    assert {name for name in names if "birth" in name} == {
        "birth_year",
        "birth_month",
    }
    assert {name for name in names if "gender" in name} == {"gender_division"}


@isolate_apps("apps.players")
def test_scan_catches_a_birthdate_and_a_raw_gender() -> None:
    class Bad(models.Model):
        date_of_birth = models.DateField()
        birth_moment = models.DateTimeField()
        gender = models.CharField(max_length=8)
        sex = models.CharField(max_length=8)

        def __str__(self) -> str:
            return "bad"

    assert _offending_fields([Bad]) == [
        "players.Bad.date_of_birth",
        "players.Bad.birth_moment",
        "players.Bad.gender",
        "players.Bad.sex",
    ]


def test_no_fixtures_under_apps() -> None:
    # No fixtures exist today. A fixture added later must not carry a
    # birthdate or a raw gender: extend this test when one lands.
    assert sorted(APPS_DIR.glob("**/fixtures/*")) == []


def test_signup_age_form_asks_for_year_month_division_name_and_place_only() -> None:
    assert set(SignupAgeForm.base_fields) == {
        "birth_year",
        "birth_month",
        "gender_division",
        "first_name",
        "last_name",
        "city",
        "region",
    }


def test_signup_input_has_no_day_or_raw_gender_field() -> None:
    names = {name.lower() for name in SignupInput().fields}

    assert {name for name in names if "birth" in name} == {
        "birth_year",
        "birth_month",
    }
    assert not {name for name in names if "day" in name or "date" in name}
    assert not names & RAW_GENDER_NAMES
    assert {name for name in names if "gender" in name} == {"gender_division"}


@pytest.fixture
def _clear_rate_limits() -> Iterator[None]:
    """allauth keeps its rate limits in the cache."""
    cache.clear()
    yield
    cache.clear()


@pytest.mark.django_db
@pytest.mark.usefixtures("_clear_rate_limits", "commit_after_each_request")
def test_signup_session_holds_no_birthdate_or_raw_gender() -> None:
    response = signup(Client(), APP, "no.birthdate@example.org")
    token = response.json()["meta"]["session_token"]

    store = import_module(settings.SESSION_ENGINE).SessionStore(session_key=token)
    held = store[SIGNUP_DETAILS_SESSION_KEY]

    assert set(held) == {
        "account",
        "year",
        "month",
        "gender_division",
        "legal_name",
        "city",
        "region",
    }
    assert all(isinstance(held[key], int) for key in ("year", "month"))
    assert held["gender_division"] in {"mens", "womens"}
