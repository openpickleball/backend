"""What the player data export holds (issue #39, ADR 0051).

The builder reads every record that belongs to the Account and shows
other players only as match responses show them. Made-up data only.
"""

import json
from datetime import date, timedelta
from typing import Any

import pytest
from allauth.account.models import EmailAddress
from allauth.usersessions.models import UserSession
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.utils import timezone
from oauth2_provider.models import get_access_token_model

from apps.accounts.models import Agreement, AgreementAcceptance
from apps.accounts.tests.factories import UserFactory, enroll_totp, totp_secret
from apps.audit.models import AuditAction
from apps.audit.services import record_event
from apps.clubs.models import OrganizerReportReason
from apps.clubs.reports import close_report, report_organizer
from apps.clubs.tests.factories import (
    ClubFactory,
    ClubRoleFactory,
    OrganizerMemberFactory,
    verified_organizer,
)
from apps.events.tests.factories import EventFactory
from apps.exports.builder import build_export
from apps.exports.schemas import AuditActor, DataExportOut
from apps.matches.models import (
    FlagReason,
    Match,
    MatchFlag,
    MatchFormat,
    MatchVouch,
    ResultsFile,
    SubmissionBatch,
)
from apps.matches.schemas import PRIVATE_PLAYER_NAME
from apps.matches.services import record_match
from apps.matches.workflow import confirm_match
from apps.partners.tests.factories import PartnerApplicationFactory
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory
from apps.ratings.models import RatingChangeKind
from apps.ratings.tests.factories import make_history, make_state

pytestmark = pytest.mark.django_db

LEGAL_NAME = "Quorrin Made-up Legal"
OTHER_LEGAL_NAME = "Zephyrine Made-up Stranger"
OTHER_CITY = "Brindlewick-Madeup"
OTHER_REGION = "Harrowmere-Madeup"
SESSION_KEY = "plantedsessionkeyvexmoor0123456789"
USER_AGENT = "MadeUpBrowser/1.0 (Vexmoor)"
ACCESS_TOKEN = "planted-access-token-vexmoor"
CLOSE_REASON = "Planted close reason Vexmoor"
EVENT_REASON = "Planted event reason Vexmoor"


def _json(export: DataExportOut) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(export.model_dump_json())
    return loaded


def _self_match(players: list[Player], played_on: date) -> Match:
    return record_match(
        format=MatchFormat.SINGLES,
        team_a=players[:1],
        team_b=players[1:],
        games=((11, 7),),
        played_on=played_on,
        submitted_by_user=players[0].account,
    ).match


def test_an_account_with_no_player_still_builds() -> None:
    account = UserFactory.create()

    data = _json(build_export(account))

    assert data["format_version"] == "1"
    assert data["account"]["email"] == account.email
    assert data["profile"] is None
    assert data["matches"] == []
    assert data["confirmations"] == []
    assert data["ratings"] == {}
    assert data["rating_history"] == []


def test_account_addresses_sessions_mfa_and_agreements() -> None:
    account = UserFactory.create()
    EmailAddress.objects.create(
        user=account, email=account.email, verified=True, primary=True
    )
    UserSession.objects.create(
        user=account,
        session_key=SESSION_KEY,
        ip="192.0.2.10",
        user_agent=USER_AGENT,
    )
    enroll_totp(account)
    accepted = timezone.now()
    AgreementAcceptance.objects.create(
        account=account,
        agreement=Agreement.TERMS_OF_SERVICE,
        version=1,
        accepted_at=accepted,
    )

    data = _json(build_export(account))

    assert data["account"]["date_joined"] is not None
    assert data["email_addresses"] == [
        {"email": account.email, "verified": True, "primary": True}
    ]
    (session,) = data["sessions"]
    assert session["ip"] == "192.0.2.10"
    assert session["user_agent"] == USER_AGENT
    assert set(session) == {"ip", "user_agent", "created_at", "last_seen_at"}
    assert sorted(method["type"] for method in data["mfa_methods"]) == [
        "recovery_codes",
        "totp",
    ]
    (agreement,) = data["agreements"]
    assert agreement["agreement"] == Agreement.TERMS_OF_SERVICE
    assert agreement["version"] == 1


def test_the_profile_has_the_players_own_decrypted_fields() -> None:
    player = PlayerFactory.create(legal_name=LEGAL_NAME, birth_month=4)

    profile = _json(build_export(player.account))["profile"]

    assert profile["code"] == player.public_code
    assert profile["legal_name"] == LEGAL_NAME
    assert profile["birth_year"] == player.birth_year
    assert profile["birth_month"] == 4
    assert profile["gender_division"] == player.gender_division
    assert profile["privacy"]["rating_visibility"] == "signed_in"


def test_matches_show_other_players_only_as_match_responses_do() -> None:
    player = PlayerFactory.create()
    other = PlayerFactory.create(
        legal_name=OTHER_LEGAL_NAME, city=OTHER_CITY, region=OTHER_REGION
    )
    match = _self_match([player, other], date(2026, 5, 1))
    gone = PlayerFactory.create()
    second = _self_match([player, gone], date(2026, 6, 1))
    get_user_model().objects.filter(pk=gone.account_id).update(is_active=False)
    _self_match(PlayerFactory.create_batch(2), date(2026, 6, 2))

    data = _json(build_export(player.account))
    text = json.dumps(data)

    assert [m["code"] for m in data["matches"]] == [
        match.public_code,
        second.public_code,
    ]
    first = data["matches"][0]
    assert first["team_a"] == [
        {"code": player.public_code, "display_name": player.display_name}
    ]
    assert first["team_b"] == [
        {"code": other.public_code, "display_name": other.display_name}
    ]
    assert first["my_team"] == "A"
    assert data["matches"][1]["team_b"] == [
        {"code": None, "display_name": PRIVATE_PLAYER_NAME}
    ]
    for leaked in (
        OTHER_LEGAL_NAME,
        OTHER_CITY,
        OTHER_REGION,
        other.account.email,
        gone.account.email,
        gone.display_name,
    ):
        assert leaked not in text


def test_confirmations_submitted_matches_and_vouches() -> None:
    player, other = PlayerFactory.create(), PlayerFactory.create()
    match = _self_match([player, other], date(2026, 5, 1))
    confirm_match(match, actor=player.account)
    held = _self_match([player, other], date(2026, 5, 2))
    later = _self_match([player, other], date(2026, 5, 3))
    flag = MatchFlag.objects.create(
        match=later, duplicate_of=held, reason=FlagReason.POSSIBLE_DUPLICATE
    )
    MatchVouch.objects.create(flag=flag, match=held, account=player.account)

    data = _json(build_export(player.account))

    assert [(c["match"], c["decision"]) for c in data["confirmations"]] == [
        (match.public_code, "confirm")
    ]
    assert [s["match"] for s in data["submitted_matches"]] == [
        match.public_code,
        held.public_code,
        later.public_code,
    ]
    assert [v["match"] for v in data["vouches"]] == [held.public_code]


def test_submission_batches_and_results_files_show_codes_and_dates() -> None:
    member = OrganizerMemberFactory.create(organizer=verified_organizer())
    event = EventFactory.create(organizer=member.organizer)
    batch = SubmissionBatch.objects.create(
        submitted_by_user=member.account, event=event
    )
    results_file = ResultsFile.objects.create(
        submission_batch=batch, content=b"name,made-up\nVexmoor Row,1\n"
    )

    data = _json(build_export(member.account))

    (entry,) = data["submission_batches"]
    assert entry["event"] == event.public_code
    assert entry["club"] is None
    assert entry["matches"] == []
    assert entry["results_file"]["code"] == results_file.public_code
    assert entry["results_file"]["status"] == "pending"
    assert "Vexmoor Row" not in json.dumps(data)


def test_ratings_and_history_carry_reliability_and_demonstration() -> None:
    player = PlayerFactory.create()
    make_state(player, rating_format="singles")
    make_history(player, rating_format="singles", kind=RatingChangeKind.REPLAY)

    data = _json(build_export(player.account))

    rating = data["ratings"]["singles"]
    assert rating["demonstration"] is True
    assert "reliability" in rating
    (row,) = data["rating_history"]
    assert row["demonstration"] is True
    assert row["rating_format"] == "singles"


def test_memberships_club_roles_reports_and_applications() -> None:
    player = PlayerFactory.create()
    account = player.account
    organizer = verified_organizer()
    OrganizerMemberFactory.create(organizer=organizer, account=account)
    club = ClubFactory.create()
    ClubRoleFactory.create(club=club, account=account)
    reported = verified_organizer()
    report = report_organizer(
        reported.pk, actor=account, reason=OrganizerReportReason.WRONG_RESULTS
    )
    moderator = UserFactory.create(is_staff=True)
    moderator.user_permissions.add(
        *Permission.objects.filter(codename="verify_organizer")
    )
    close_report(report, actor=moderator, reason=CLOSE_REASON)
    application = PartnerApplicationFactory.create(user=account)

    data = _json(build_export(account))
    text = json.dumps(data)

    assert data["organizer_memberships"] == [
        {
            "organizer": organizer.public_code,
            "name": organizer.name,
            "verified": True,
            "verification_requested": False,
            "role": "match_manager",
        }
    ]
    assert data["club_roles"] == [
        {"club": club.public_code, "name": club.name, "role": "admin"}
    ]
    (entry,) = data["organizer_reports"]
    assert entry["organizer"] == reported.public_code
    assert entry["reason"] == "wrong_results"
    assert entry["closed_at"] is not None
    assert CLOSE_REASON not in text
    assert moderator.email not in text
    (app,) = data["partner_applications"]
    assert app["client_id"] == application.client_id
    assert app["name"] == application.name
    assert not {"client_secret", "hash_client_secret", "approved_by"} & set(app)


def test_oauth_records_show_metadata_only() -> None:
    account = UserFactory.create()
    application = PartnerApplicationFactory.create()
    get_access_token_model().objects.create(
        user=account,
        application=application,
        token=ACCESS_TOKEN,
        scope="read",
        expires=timezone.now() + timedelta(hours=1),
    )

    data = _json(build_export(account))

    (record,) = data["oauth"]
    assert record["kind"] == "access_token"
    assert record["application"] == application.client_id
    assert record["scope"] == "read"
    assert ACCESS_TOKEN not in json.dumps(data)


def test_audit_events_follow_the_selection_and_redaction_rules() -> None:
    player = PlayerFactory.create()
    account = player.account
    staff = UserFactory.create(is_staff=True)
    other_player = PlayerFactory.create()
    record_event(
        action=AuditAction.PLAYER_PERSONAL_DATA_VIEW,
        target=player,
        actor=staff,
        after={"fields": ["legal_name"]},
        reason=EVENT_REASON,
    )
    record_event(
        action=AuditAction.SESSION_END,
        target=account,
        actor=account,
        after={"current": False},
    )
    # An event the Account made on another player's record: no values.
    record_event(
        action=AuditAction.PLAYER_PROFILE_CHANGE,
        target=other_player,
        actor=account,
        after={"fields": ["display_name"]},
    )
    # An event about someone else, by someone else: left out.
    record_event(
        action=AuditAction.SESSION_END,
        target=other_player.account,
        actor=other_player.account,
        after={"current": True},
    )
    # The system changed this player's rating: values without row ids.
    state = make_state(player, rating_format="singles")
    record_event(
        action=AuditAction.RATING_CHANGE,
        target=state,
        before=None,
        after={"rating": 3.5, "kind": "replay", "match": None, "replay": 41},
    )

    events = _json(build_export(account))["audit_events"]
    text = json.dumps(events)

    by_action = {(e["action"], e["target_type"]): e for e in events}
    assert len(events) == 4
    view = by_action[("player.personal_data_view", "players.player")]
    assert view["actor"] == AuditActor.STAFF
    assert view["target_code"] == player.public_code
    assert view["after"] == {"fields": ["legal_name"]}
    ended = by_action[("session.end", "accounts.user")]
    assert ended["actor"] == AuditActor.YOU
    assert ended["target_code"] is None
    assert ended["after"] == {"current": False}
    made = by_action[("player.profile_change", "players.player")]
    assert made["target_code"] == other_player.public_code
    assert made["before"] is None
    assert made["after"] is None
    rating = by_action[("rating.change", "ratings.ratingstate")]
    assert rating["actor"] == AuditActor.SYSTEM
    assert rating["after"] == {"rating": 3.5, "kind": "replay", "match": None}
    assert EVENT_REASON not in text
    assert staff.email not in text
    assert set(view) == {
        "action",
        "at",
        "target_type",
        "target_code",
        "actor",
        "before",
        "after",
    }


def test_another_accounts_event_shows_as_other() -> None:
    player = PlayerFactory.create()
    stranger = UserFactory.create()
    record_event(
        action=AuditAction.PLAYER_PROFILE_CHANGE,
        target=player,
        actor=stranger,
        after={"fields": ["city"]},
    )

    (event,) = _json(build_export(player.account))["audit_events"]

    assert event["actor"] == AuditActor.OTHER


def test_no_secret_reaches_the_export() -> None:
    account = UserFactory.create()
    UserSession.objects.create(
        user=account, session_key=SESSION_KEY, ip="192.0.2.11", user_agent="x"
    )
    enroll_totp(account)
    secret = totp_secret(account)
    application = PartnerApplicationFactory.create(user=account)

    text = build_export(account).model_dump_json()

    assert SESSION_KEY not in text
    assert secret not in text
    assert application.client_secret not in text
    assert account.password not in text
