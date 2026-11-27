"""The player data export's format, and the request and download schemas
(issue #39, ADR 0051).

``DataExportOut`` is the whole file. Its sections reuse the schemas the
other first-party operations return, so the export shows a record the
way the API already shows it to the same Account: the Player through
``MyPlayerOut``, each match through ``MatchOut`` (other players only as
``match_out`` shows them to this Account), ratings through ``RatingOut``
and ``RatingHistoryItemOut``, and memberships through
``OrganizerMembershipOut``. ``docs/data-export.md`` describes every
section. No row id, session key, secret, token, or event reason appears.
"""

from datetime import date, datetime
from enum import StrEnum
from typing import Final, Literal

from ninja import Field, Schema
from pydantic import JsonValue

from apps.clubs.models import ClubRoleKind, OrganizerReportReason
from apps.clubs.schemas import OrganizerMembershipOut
from apps.matches.models import (
    ConfirmationDecision,
    MatchType,
    RatingFormat,
    ResultsFileStatus,
)
from apps.matches.schemas import MatchOut
from apps.players.schemas import MyPlayerOut
from apps.ratings.schemas import RatingHistoryItemOut, RatingOut

FORMAT_VERSION: Final = "1"
EXPORT_FILENAME: Final = "open-data-export.json"


class DataExportRequestOut(Schema):
    """A data export request was stored; the email follows."""

    requested_at: datetime = Field(
        description=(
            "When the request was stored. An email with a single-use download "
            "link follows once the export is built; the link lasts 24 hours."
        )
    )


class DataExportDownloadIn(Schema):
    """The download key from the data export email."""

    key: str = Field(
        description=(
            "The key from the link in the data export email. A key longer "
            "than 128 characters gets the same 404 as an unknown one."
        )
    )


class ExportAccountOut(Schema):
    """The Account: its login email, dates, and access."""

    email: str
    date_joined: datetime
    last_login: datetime | None
    is_active: bool
    is_staff: bool
    is_superuser: bool
    groups: list[str] = Field(description="The names of the Account's groups.")
    permissions: list[str] = Field(
        description="The Account's own permissions, as app_label.codename."
    )


class ExportEmailAddressOut(Schema):
    """One email address on the Account."""

    email: str
    verified: bool
    primary: bool


class ExportSessionOut(Schema):
    """One signed-in session, as the session list records it."""

    ip: str | None
    user_agent: str
    created_at: datetime
    last_seen_at: datetime


class ExportMFAMethodOut(Schema):
    """One two-factor method: its type and dates, never its secret."""

    type: str
    created_at: datetime
    last_used_at: datetime | None


class ExportAgreementOut(Schema):
    """One version of a legal agreement the Account accepted."""

    agreement: str
    version: int
    accepted_at: datetime


class ExportConfirmationOut(Schema):
    """One confirm, dispute, or withdrawal the Player made on a match."""

    match: str = Field(description="The match's OPEN Code.")
    decision: ConfirmationDecision
    created_at: datetime


class ExportVouchOut(Schema):
    """One vouch the Account gave for a held match."""

    match: str = Field(description="The match's OPEN Code.")
    created_at: datetime


class ExportSubmittedMatchOut(Schema):
    """A match the Account submitted."""

    match: str = Field(description="The match's OPEN Code.")
    submitted_at: datetime


class ExportResultsFileOut(Schema):
    """A CSV results file the Account uploaded, without its contents."""

    code: str
    status: ResultsFileStatus
    error_code: str = Field(description="Why processing failed; empty if not.")
    location: str
    game_target: int
    created_at: datetime
    processed_at: datetime | None


class ExportSubmissionBatchOut(Schema):
    """One upload or bulk request of matches the Account submitted."""

    created_at: datetime
    event: str | None = Field(description="The Event's OPEN Code, if any.")
    club: str | None = Field(description="The Club's OPEN Code, if any.")
    matches: list[str] = Field(description="The OPEN Codes of its matches.")
    expected_size: int | None = Field(
        description="How many matches the batch meant to record, if set."
    )
    results_file: ExportResultsFileOut | None


class ExportClubRoleOut(Schema):
    """One of the Account's Club Roles."""

    club: str = Field(description="The Club's OPEN Code.")
    name: str
    role: ClubRoleKind


class ExportOrganizerReportOut(Schema):
    """An Organizer Report the Account filed."""

    organizer: str = Field(description="The Organizer's OPEN Code.")
    reason: OrganizerReportReason
    created_at: datetime
    closed_at: datetime | None


class ExportPartnerApplicationOut(Schema):
    """A Partner Application the Account owns, never its secret or who
    approved it."""

    client_id: str
    name: str
    organizer: str | None = Field(description="The owning Organizer's OPEN Code.")
    redirect_uris: str
    post_logout_redirect_uris: str
    allowed_origins: str
    client_type: str
    authorization_grant_type: str
    skip_authorization: bool
    algorithm: str
    is_sandbox: bool
    approved_at: datetime | None
    created: datetime
    updated: datetime


class OAuthRecordKind(StrEnum):
    """Which kind of OAuth record an entry describes."""

    ACCESS_TOKEN = "access_token"
    REFRESH_TOKEN = "refresh_token"
    ID_TOKEN = "id_token"
    GRANT = "grant"
    DEVICE_GRANT = "device_grant"


class ExportOAuthRecordOut(Schema):
    """An OAuth token or grant issued to the Account, never its value."""

    kind: OAuthRecordKind
    application: str | None = Field(
        description="The application's client_id, if the record names one."
    )
    scope: str | None
    redirect_uri: str | None = Field(description="A grant's redirect URI.")
    created: datetime | None
    expires: datetime | None
    revoked: datetime | None = Field(description="When a refresh token was revoked.")


class ExportMatchOut(MatchOut):
    """A match the player played: ``MatchOut`` as match responses show it
    to the player, plus the match's own details. Never its rating format,
    which reveals the players' gender divisions."""

    match_type: MatchType
    location: str
    played_at: datetime | None
    played_at_utc_offset: int | None = Field(
        description="Minutes east of UTC where the match was played."
    )
    event_label: str
    bracket_label: str
    corrects: str | None = Field(
        description="The OPEN Code of the match this one corrects, if any."
    )


class _RatingDetail(Schema):
    """The engine's detail and versions behind a rating, as stored."""

    rd: float
    matches_played: int
    last_played: date | None
    calculated_as_of: date | None
    live_updates_since_recalculation: int
    config_version: str
    params_version: str
    parameter_fingerprint: str
    engine_version: str


class ExportRatingOut(RatingOut, _RatingDetail):
    """A current rating, with its reliability and ``demonstration: true``,
    plus the stored detail and versions."""


class ExportRatingHistoryItemOut(RatingHistoryItemOut, _RatingDetail):
    """A rating change, with its reliability and ``demonstration: true``,
    plus the match that caused it, the change, and the stored detail."""

    match: str | None = Field(description="The causing match's OPEN Code.")
    delta: float | None = Field(description="The change a live update made.")


class AuditActor(StrEnum):
    """Who acted, without naming another Account."""

    YOU = "you"
    STAFF = "staff"
    OTHER = "other"
    SYSTEM = "system"


class ExportAuditEventOut(Schema):
    """One Audit Event about the Account's own records, or that it made.

    ``before`` and ``after`` appear only when the target is one of the
    Account's own records (the Account, its Player and privacy row, its
    ratings, and its agreement rows), without row ids. The reason is
    never included.
    """

    action: str
    at: datetime
    target_type: str = Field(description="The target's model, as app.model.")
    target_code: str | None = Field(
        description="The target's OPEN Code, or null when it has none."
    )
    actor: AuditActor
    before: JsonValue = None
    after: JsonValue = None


class DataExportOut(Schema):
    """A copy of the data OPEN holds about the signed-in Account.

    Player sections are null or empty for an Account with no Player. Other
    players appear only as match responses show them. Every rating carries
    its reliability and ``demonstration: true``.
    """

    format_version: Literal["1"]
    generated_at: datetime
    account: ExportAccountOut
    email_addresses: list[ExportEmailAddressOut]
    sessions: list[ExportSessionOut]
    mfa_methods: list[ExportMFAMethodOut]
    agreements: list[ExportAgreementOut]
    profile: MyPlayerOut | None
    matches: list[ExportMatchOut]
    confirmations: list[ExportConfirmationOut]
    vouches: list[ExportVouchOut]
    submitted_matches: list[ExportSubmittedMatchOut]
    submission_batches: list[ExportSubmissionBatchOut]
    ratings: dict[RatingFormat, ExportRatingOut]
    rating_history: list[ExportRatingHistoryItemOut]
    organizer_memberships: list[OrganizerMembershipOut]
    club_roles: list[ExportClubRoleOut]
    organizer_reports: list[ExportOrganizerReportOut]
    partner_applications: list[ExportPartnerApplicationOut]
    oauth: list[ExportOAuthRecordOut]
    audit_events: list[ExportAuditEventOut]
