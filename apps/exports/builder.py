"""Build the player data export from the database (issue #39, ADR 0051).

``build_export(account)`` only reads. It gathers every record that
belongs to the Account (``docs/data-export.md`` lists the sections) and
returns a ``DataExportOut``. Decrypted values (the legal name, birth year
and month, and gender division) are the Account's own Player's only.
Other players appear only in matches, through ``match_out`` with this
Account as the viewer, so another player's private data never appears:
an inactive Account's Player shows as a private player.

Audit Events (decision 4 on #39): events that target the Account, its
Player, its privacy row, its rating states, or its agreement rows, plus
events the Account made. Each shows its action, time, target type, the
target's OPEN Code (null when it has none), and the actor only as
``you``, ``staff``, ``other``, or ``system``. ``before`` and ``after``
appear only for the Account's own records, with every row id removed;
the reason never appears.

No session key, MFA secret, client secret, token, or code is read into
the export.
"""

from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast

from allauth.account.models import EmailAddress
from allauth.mfa.models import Authenticator
from allauth.usersessions.models import UserSession
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.db.models import Q
from django.utils import timezone
from oauth2_provider.models import (
    get_access_token_model,
    get_application_model,
    get_device_grant_model,
    get_grant_model,
    get_id_token_model,
    get_refresh_token_model,
)
from pydantic import JsonValue

from apps.accounts.models import AgreementAcceptance
from apps.audit.models import AuditEvent
from apps.clubs.models import (
    ClubRole,
    ClubRoleKind,
    MemberRole,
    OrganizerMember,
    OrganizerReport,
    OrganizerReportReason,
)
from apps.clubs.schemas import OrganizerMembershipOut
from apps.exports.schemas import (
    FORMAT_VERSION,
    AuditActor,
    DataExportOut,
    ExportAccountOut,
    ExportAgreementOut,
    ExportAuditEventOut,
    ExportClubRoleOut,
    ExportConfirmationOut,
    ExportEmailAddressOut,
    ExportMFAMethodOut,
    ExportOAuthRecordOut,
    ExportOrganizerReportOut,
    ExportPartnerApplicationOut,
    ExportResultsFileOut,
    ExportSessionOut,
    ExportSubmissionBatchOut,
    ExportSubmittedMatchOut,
    ExportVouchOut,
    OAuthRecordKind,
)
from apps.matches.models import (
    ConfirmationDecision,
    Match,
    MatchConfirmation,
    MatchParticipant,
    MatchVouch,
    RatingFormat,
    ResultsFile,
    ResultsFileStatus,
    SubmissionBatch,
)
from apps.matches.schemas import MatchOut, match_out, with_match_details
from apps.players.models import Player, PlayerPrivacy
from apps.players.schemas import MyPlayerOut, my_player_out
from apps.public_ids.models import PublicCodeModel
from apps.ratings.models import RatingHistory, RatingState
from apps.ratings.schemas import (
    RatingHistoryItemOut,
    RatingOut,
    rating_history_item_out,
    rating_out,
)

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

# Keys in an Audit Event's values that hold row ids, which the API never
# returns (ADR 0008): the replay of a rating change, and the groups and
# permissions of an admin change. Keys named like a row id (``id``,
# ``pk``, ``*_id``) are removed too.
ROW_ID_VALUE_KEYS = frozenset({"replay", "groups", "user_permissions"})


def build_export(account: "_User", *, now: datetime | None = None) -> DataExportOut:
    """Every record that belongs to ``account``, as the export's sections."""
    player = (
        Player.objects.filter(account_id=account.pk).select_related("privacy").first()
    )
    return DataExportOut(
        format_version=FORMAT_VERSION,
        generated_at=now or timezone.now(),
        account=ExportAccountOut(
            email=account.email,
            date_joined=account.date_joined,
            last_login=account.last_login,
        ),
        email_addresses=_email_addresses(account),
        sessions=_sessions(account),
        mfa_methods=_mfa_methods(account),
        agreements=_agreements(account),
        profile=_profile(player),
        matches=_matches(player, account),
        confirmations=_confirmations(player),
        vouches=_vouches(account),
        submitted_matches=_submitted_matches(account),
        submission_batches=_submission_batches(account),
        ratings=_ratings(player),
        rating_history=_rating_history(player),
        organizer_memberships=_memberships(account),
        club_roles=_club_roles(account),
        organizer_reports=_organizer_reports(account),
        partner_applications=_partner_applications(account),
        oauth=_oauth(account),
        audit_events=_audit_events(account, player),
    )


def _email_addresses(account: "_User") -> list[ExportEmailAddressOut]:
    rows = EmailAddress.objects.filter(user_id=account.pk).order_by("pk")
    return [
        ExportEmailAddressOut(
            email=row.email, verified=row.verified, primary=row.primary
        )
        for row in rows
    ]


def _sessions(account: "_User") -> list[ExportSessionOut]:
    rows = (
        UserSession.objects.filter(user_id=account.pk)
        .only("ip", "user_agent", "created_at", "last_seen_at")
        .order_by("created_at", "pk")
    )
    return [
        ExportSessionOut(
            ip=row.ip,
            user_agent=row.user_agent,
            created_at=row.created_at,
            last_seen_at=row.last_seen_at,
        )
        for row in rows
    ]


def _mfa_methods(account: "_User") -> list[ExportMFAMethodOut]:
    # Never ``data``, which holds the secrets.
    rows = (
        Authenticator.objects.filter(user_id=account.pk)
        .only("type", "created_at", "last_used_at")
        .order_by("created_at", "pk")
    )
    return [
        ExportMFAMethodOut(
            type=row.type, created_at=row.created_at, last_used_at=row.last_used_at
        )
        for row in rows
    ]


def _agreements(account: "_User") -> list[ExportAgreementOut]:
    rows = AgreementAcceptance.objects.filter(account_id=account.pk).order_by(
        "accepted_at", "pk"
    )
    return [
        ExportAgreementOut(
            agreement=row.agreement, version=row.version, accepted_at=row.accepted_at
        )
        for row in rows
    ]


def _profile(player: Player | None) -> MyPlayerOut | None:
    if player is None:
        return None
    privacy: PlayerPrivacy = player.privacy
    return my_player_out(player, privacy)


def _matches(player: Player | None, account: "_User") -> list[MatchOut]:
    if player is None:
        return []
    played = MatchParticipant.objects.filter(player=player).values("match_id")
    matches = with_match_details(
        Match.objects.filter(pk__in=played).order_by("played_on", "created_at", "pk")
    )
    return [match_out(match, viewer=account) for match in matches]


def _confirmations(player: Player | None) -> list[ExportConfirmationOut]:
    if player is None:
        return []
    rows = (
        MatchConfirmation.objects.filter(player=player)
        .select_related("match")
        .only("decision", "created_at", "match__public_code")
        .order_by("created_at", "pk")
    )
    return [
        ExportConfirmationOut(
            match=row.match.public_code,
            decision=ConfirmationDecision(row.decision),
            created_at=row.created_at,
        )
        for row in rows
    ]


def _vouches(account: "_User") -> list[ExportVouchOut]:
    # The match vouched for, never the flag's other match (ADR 0011).
    rows = (
        MatchVouch.objects.filter(account_id=account.pk)
        .select_related("match")
        .only("created_at", "match__public_code")
        .order_by("created_at", "pk")
    )
    return [
        ExportVouchOut(match=row.match.public_code, created_at=row.created_at)
        for row in rows
    ]


def _submitted_matches(account: "_User") -> list[ExportSubmittedMatchOut]:
    rows = (
        Match.objects.filter(submitted_by_user_id=account.pk)
        .only("public_code", "created_at")
        .order_by("created_at", "pk")
    )
    return [
        ExportSubmittedMatchOut(match=row.public_code, submitted_at=row.created_at)
        for row in rows
    ]


def _submission_batches(account: "_User") -> list[ExportSubmissionBatchOut]:
    batches = list(
        SubmissionBatch.objects.filter(submitted_by_user_id=account.pk)
        .select_related("event", "club")
        .order_by("created_at", "pk")
    )
    ids = [batch.pk for batch in batches]
    codes: dict[int, list[str]] = defaultdict(list)
    for batch_id, code in (
        Match.objects.filter(submission_batch_id__in=ids)
        .order_by("created_at", "pk")
        .values_list("submission_batch_id", "public_code")
    ):
        codes[batch_id].append(code)
    # Never the file's content or report, which name other players.
    files = {
        row.submission_batch_id: row
        for row in ResultsFile.objects.filter(submission_batch_id__in=ids).only(
            "submission_batch_id", "public_code", "status", "created_at", "processed_at"
        )
    }
    return [
        ExportSubmissionBatchOut(
            created_at=batch.created_at,
            event=None if batch.event is None else batch.event.public_code,
            club=None if batch.club is None else batch.club.public_code,
            matches=codes[batch.pk],
            results_file=_results_file(files.get(batch.pk)),
        )
        for batch in batches
    ]


def _results_file(row: ResultsFile | None) -> ExportResultsFileOut | None:
    if row is None:
        return None
    return ExportResultsFileOut(
        code=row.public_code,
        status=ResultsFileStatus(row.status),
        created_at=row.created_at,
        processed_at=row.processed_at,
    )


def _ratings(player: Player | None) -> dict[RatingFormat, RatingOut]:
    if player is None:
        return {}
    states = RatingState.objects.filter(player=player).order_by("rating_format")
    return {RatingFormat(state.rating_format): rating_out(state) for state in states}


def _rating_history(player: Player | None) -> list[RatingHistoryItemOut]:
    if player is None:
        return []
    rows = RatingHistory.objects.filter(player=player).order_by("created_at", "pk")
    return [rating_history_item_out(row) for row in rows]


def _memberships(account: "_User") -> list[OrganizerMembershipOut]:
    # As list_my_organizers shows them (ADR 0050).
    rows = (
        OrganizerMember.objects.filter(account_id=account.pk)
        .select_related("organizer")
        .order_by("organizer__public_code")
    )
    return [
        OrganizerMembershipOut(
            organizer=row.organizer.public_code,
            name=row.organizer.name,
            verified=row.organizer.is_verified,
            role=MemberRole(row.role),
        )
        for row in rows
    ]


def _club_roles(account: "_User") -> list[ExportClubRoleOut]:
    rows = (
        ClubRole.objects.filter(account_id=account.pk)
        .select_related("club")
        .order_by("club__public_code")
    )
    return [
        ExportClubRoleOut(
            club=row.club.public_code, name=row.club.name, role=ClubRoleKind(row.role)
        )
        for row in rows
    ]


def _organizer_reports(account: "_User") -> list[ExportOrganizerReportOut]:
    # Never the close reason or the closer, which live only in the close
    # event (ADR 0037).
    rows = (
        OrganizerReport.objects.filter(reporter_id=account.pk)
        .select_related("organizer")
        .only("reason", "created_at", "closed_at", "organizer__public_code")
        .order_by("created_at", "pk")
    )
    return [
        ExportOrganizerReportOut(
            organizer=row.organizer.public_code,
            reason=OrganizerReportReason(row.reason),
            created_at=row.created_at,
            closed_at=row.closed_at,
        )
        for row in rows
    ]


def _partner_applications(account: "_User") -> list[ExportPartnerApplicationOut]:
    # Never the client secret.
    rows = (
        get_application_model()
        ._default_manager.filter(user_id=account.pk)
        .only("client_id", "name", "created")
        .order_by("created", "pk")
    )
    return [
        ExportPartnerApplicationOut(
            client_id=row.client_id, name=row.name, created=row.created
        )
        for row in rows
    ]


def _oauth(account: "_User") -> list[ExportOAuthRecordOut]:
    """Each OAuth token and grant issued to the Account, never its value."""
    records: list[ExportOAuthRecordOut] = []
    timed: list[tuple[OAuthRecordKind, Any, bool]] = [
        (OAuthRecordKind.ACCESS_TOKEN, get_access_token_model(), True),
        (OAuthRecordKind.REFRESH_TOKEN, get_refresh_token_model(), False),
        (OAuthRecordKind.ID_TOKEN, get_id_token_model(), True),
        (OAuthRecordKind.GRANT, get_grant_model(), True),
    ]
    for kind, model, has_scope in timed:
        fields = ["created", "application__client_id"]
        if has_scope:
            fields += ["scope", "expires"]
        rows = (
            model._default_manager.filter(user_id=account.pk)
            .select_related("application")
            .only(*fields)
            .order_by("created", "pk")
        )
        for row in rows:
            application = row.application
            records.append(
                ExportOAuthRecordOut(
                    kind=kind,
                    application=None if application is None else application.client_id,
                    scope=row.scope if has_scope else None,
                    created=row.created,
                    expires=row.expires if has_scope else None,
                )
            )
    for grant in (
        get_device_grant_model()
        .objects.filter(user_id=account.pk)
        .only("client_id", "scope", "expires")
        .order_by("pk")
    ):
        records.append(
            ExportOAuthRecordOut(
                kind=OAuthRecordKind.DEVICE_GRANT,
                application=grant.client_id,
                scope=grant.scope,
                created=None,
                expires=grant.expires,
            )
        )
    return records


def _own_targets(account: "_User", player: Player | None) -> dict[int, set[str]]:
    """The Account's own records, as content type id to target ids."""
    own: dict[type[models.Model], Iterable[Any]] = {
        get_user_model(): [account.pk],
        AgreementAcceptance: AgreementAcceptance.objects.filter(
            account_id=account.pk
        ).values_list("pk", flat=True),
    }
    if player is not None:
        own[Player] = [player.pk]
        own[PlayerPrivacy] = PlayerPrivacy.objects.filter(player=player).values_list(
            "pk", flat=True
        )
        own[RatingState] = RatingState.objects.filter(player=player).values_list(
            "pk", flat=True
        )
    content_types = ContentType.objects.get_for_models(*own)
    return {
        content_types[model].pk: {str(pk) for pk in pks} for model, pks in own.items()
    }


def _audit_events(account: "_User", player: Player | None) -> list[ExportAuditEventOut]:
    own = _own_targets(account, player)
    about_own = Q(pk__in=[])
    for content_type_id, ids in own.items():
        if ids:
            about_own |= Q(
                target_content_type_id=content_type_id, target_object_id__in=ids
            )
    events = list(
        AuditEvent.objects.filter(about_own | Q(actor_user_id=account.pk))
        .select_related("target_content_type")
        .defer("reason")
        .order_by("created_at", "pk")
    )
    staff = set(
        get_user_model()
        .objects.filter(
            Q(is_staff=True) | Q(is_superuser=True),
            pk__in={e.actor_user_id for e in events if e.actor_user_id is not None},
        )
        .values_list("pk", flat=True)
    )
    codes = _target_codes(events)
    out: list[ExportAuditEventOut] = []
    for event in events:
        content_type = event.target_content_type
        is_own = event.target_object_id in own.get(content_type.pk, set())
        out.append(
            ExportAuditEventOut(
                action=event.action,
                at=event.created_at,
                target_type=f"{content_type.app_label}.{content_type.model}",
                target_code=codes.get((content_type.pk, event.target_object_id)),
                actor=_actor(event, account, staff),
                before=_without_row_ids(event.before) if is_own else None,
                after=_without_row_ids(event.after) if is_own else None,
            )
        )
    return out


def _actor(event: AuditEvent, account: "_User", staff: set[Any]) -> AuditActor:
    if event.actor_user_id == account.pk:
        return AuditActor.YOU
    if event.actor_user_id is not None:
        return AuditActor.STAFF if event.actor_user_id in staff else AuditActor.OTHER
    if event.actor_application_id is not None:
        return AuditActor.OTHER
    return AuditActor.SYSTEM


def _target_codes(events: list[AuditEvent]) -> dict[tuple[int, str], str]:
    """The OPEN Code of each event's target that has one and still exists."""
    wanted: dict[int, set[str]] = defaultdict(set)
    for event in events:
        wanted[event.target_content_type_id].add(event.target_object_id)
    codes: dict[tuple[int, str], str] = {}
    for content_type_id, ids in wanted.items():
        model = ContentType.objects.get_for_id(content_type_id).model_class()
        if model is None or not issubclass(model, PublicCodeModel):
            continue
        numeric = [value for value in ids if value.isdigit()]
        for pk, code in model._default_manager.filter(pk__in=numeric).values_list(
            "pk", "public_code"
        ):
            codes[(content_type_id, str(pk))] = code
    return codes


def _is_row_id_key(key: str) -> bool:
    return (
        key in ROW_ID_VALUE_KEYS
        or key.lower() in {"id", "ids", "pk", "pks"}
        or key.endswith(("_id", "_ids"))
    )


def _without_row_ids(value: Any) -> JsonValue:
    """``value`` with every row id key removed, at any depth."""
    if isinstance(value, dict):
        return {
            str(key): _without_row_ids(item)
            for key, item in value.items()
            if not _is_row_id_key(str(key))
        }
    if isinstance(value, list):
        return [_without_row_ids(item) for item in value]
    return cast("JsonValue", value)
