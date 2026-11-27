"""The export holds the player's own fields that it left out (#39).

ADR 0009 says the export holds every inventory field that belongs to
the player. It left out the rating detail and version fields, a rating
change's match and delta, the Account's access flags, groups, and
permissions, an owned application's settings, a batch's expected size,
a results file's settings and error code, and a played match's place,
time, labels, and type. Never a secret, a key, a token, a results
file's cells, or another person's data. Made-up data only.
"""

import json
from datetime import UTC, date, datetime
from typing import Any

import pytest
from django.contrib.auth.models import Group, Permission

from apps.accounts.tests.factories import UserFactory
from apps.clubs.tests.factories import OrganizerMemberFactory, verified_organizer
from apps.events.tests.factories import EventFactory
from apps.exports.builder import build_export
from apps.matches.models import MatchFormat, ResultsFile, SubmissionBatch
from apps.matches.services import record_match
from apps.partners.tests.factories import PartnerApplicationFactory
from apps.players.tests.factories import PlayerFactory
from apps.ratings.models import RatingChangeKind
from apps.ratings.tests.factories import make_history, make_state

pytestmark = pytest.mark.django_db

LOCATION = "Made-up Courts Vexmoor"
FILE_LOCATION = "Made-up Park Vexmoor"
CELL = "Vexmoor Cell Name"
PLAYED_AT = datetime(2026, 5, 1, 15, 30, tzinfo=UTC)


def _data(account: Any) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(build_export(account).model_dump_json())
    return loaded


def test_the_account_has_its_access_flags_groups_and_permissions() -> None:
    account = UserFactory.create(is_staff=True)
    group = Group.objects.create(name="Made-up Reviewers")
    account.groups.add(group)
    account.user_permissions.add(Permission.objects.get(codename="view_player"))

    data = _data(account)["account"]

    assert data["is_active"] is True
    assert data["is_staff"] is True
    assert data["is_superuser"] is False
    assert data["groups"] == ["Made-up Reviewers"]
    assert data["permissions"] == ["players.view_player"]


def test_ratings_and_history_carry_their_detail_and_versions() -> None:
    player = PlayerFactory.create()
    make_state(player, rating_format="singles")
    other = PlayerFactory.create()
    match = record_match(
        format=MatchFormat.SINGLES,
        team_a=[player],
        team_b=[other],
        games=((11, 7),),
        played_on=date(2026, 5, 1),
        submitted_by_user=player.account,
    ).match
    make_history(
        player,
        rating_format="singles",
        kind=RatingChangeKind.LIVE_UPDATE,
        match=match,
        delta=0.125,
    )

    data = _data(player.account)

    rating = data["ratings"]["singles"]
    for name in (
        "rd",
        "matches_played",
        "last_played",
        "calculated_as_of",
        "live_updates_since_recalculation",
        "config_version",
        "params_version",
        "parameter_fingerprint",
        "engine_version",
    ):
        assert name in rating, name
    assert rating["demonstration"] is True
    assert "reliability" in rating
    (row,) = data["rating_history"]
    assert row["match"] == match.public_code
    assert row["delta"] == 0.125
    assert "rd" in row
    assert row["demonstration"] is True


def test_played_matches_carry_place_time_labels_and_type() -> None:
    player, other = PlayerFactory.create(), PlayerFactory.create()
    match = record_match(
        format=MatchFormat.SINGLES,
        team_a=[player],
        team_b=[other],
        games=((11, 7),),
        played_on=date(2026, 5, 1),
        played_at=PLAYED_AT,
        played_at_utc_offset=-300,
        location=LOCATION,
        submitted_by_user=player.account,
    ).match

    (entry,) = _data(player.account)["matches"]

    assert entry["code"] == match.public_code
    assert entry["location"] == LOCATION
    assert entry["played_at"] is not None
    assert entry["played_at_utc_offset"] == -300
    assert entry["match_type"] == "self_reported"
    assert "event_label" in entry
    assert "bracket_label" in entry
    assert entry["corrects"] is None
    assert "rating_format" not in entry


def test_owned_applications_carry_their_settings_never_the_secret() -> None:
    account = UserFactory.create()
    organizer = verified_organizer()
    application = PartnerApplicationFactory.create(user=account, organizer=organizer)

    (app,) = _data(account)["partner_applications"]
    text = json.dumps(app)

    assert app["organizer"] == organizer.public_code
    assert app["is_sandbox"] is True
    assert app["approved_at"] is None
    assert "redirect_uris" in app
    assert "allowed_origins" in app
    assert application.client_secret not in text
    assert "approved_by" not in app
    assert "client_secret" not in app


def test_batches_and_results_files_carry_their_settings_never_cells() -> None:
    member = OrganizerMemberFactory.create(organizer=verified_organizer())
    event = EventFactory.create(organizer=member.organizer)
    batch = SubmissionBatch.objects.create(
        submitted_by_user=member.account, event=event, expected_size=4
    )
    ResultsFile.objects.create(
        submission_batch=batch,
        content=f"name\n{CELL}\n".encode(),
        location=FILE_LOCATION,
        game_target=15,
    )

    data = _data(member.account)
    (entry,) = data["submission_batches"]

    assert entry["expected_size"] == 4
    results_file = entry["results_file"]
    assert results_file["location"] == FILE_LOCATION
    assert results_file["game_target"] == 15
    assert results_file["error_code"] == ""
    assert CELL not in json.dumps(data)
