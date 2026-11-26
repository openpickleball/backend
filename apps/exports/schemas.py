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

from datetime import datetime
from enum import StrEnum
from typing import Final, Literal

from ninja import Field, Schema
from pydantic import JsonValue

from apps.clubs.models import ClubRoleKind, OrganizerReportReason
from apps.clubs.schemas import OrganizerMembershipOut
from apps.matches.models import ConfirmationDecision, RatingFormat, ResultsFileStatus
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
    """The Account: its login email and its dates."""

    email: str
    date_joined: datetime
    last_login: datetime | None


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
    created_at: datetime
    processed_at: datetime | None


class ExportSubmissionBatchOut(Schema):
    """One upload or bulk request of matches the Account submitted."""

    created_at: datetime
    event: str | None = Field(description="The Event's OPEN Code, if any.")
    club: str | None = Field(description="The Club's OPEN Code, if any.")
    matches: list[str] = Field(description="The OPEN Codes of its matches.")
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
    """A Partner Application the Account owns, never its secret."""

    client_id: str
    name: str
    created: datetime


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
    created: datetime | None
    expires: datetime | None


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
    matches: list[MatchOut]
    confirmations: list[ExportConfirmationOut]
    vouches: list[ExportVouchOut]
    submitted_matches: list[ExportSubmittedMatchOut]
    submission_batches: list[ExportSubmissionBatchOut]
    ratings: dict[RatingFormat, RatingOut]
    rating_history: list[RatingHistoryItemOut]
    organizer_memberships: list[OrganizerMembershipOut]
    club_roles: list[ExportClubRoleOut]
    organizer_reports: list[ExportOrganizerReportOut]
    partner_applications: list[ExportPartnerApplicationOut]
    oauth: list[ExportOAuthRecordOut]
    audit_events: list[ExportAuditEventOut]
