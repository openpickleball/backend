"""Every model that links to an Account or a Player is in the export, or
listed here with the reason it is not (issue #39, ADR 0051).

A new model with a foreign key or one-to-one link to ``AUTH_USER_MODEL``
or ``players.Player`` fails this test until it is exported (a section in
``apps.exports.builder``) or excluded below with a reason.
"""

from django.apps import apps
from django.contrib.auth import get_user_model

from apps.players.models import Player

# (model label, field name): the export section that holds it.
EXPORTED: dict[tuple[str, str], str] = {
    ("account.EmailAddress", "user"): "email_addresses",
    ("mfa.Authenticator", "user"): "mfa_methods",
    ("usersessions.UserSession", "user"): "sessions",
    ("accounts.AgreementAcceptance", "account"): "agreements",
    ("players.Player", "account"): "profile",
    ("players.PlayerPrivacy", "player"): "profile.privacy",
    ("clubs.OrganizerMember", "account"): "organizer_memberships",
    ("clubs.ClubRole", "account"): "club_roles",
    ("clubs.OrganizerReport", "reporter"): "organizer_reports",
    ("partners.PartnerApplication", "user"): "partner_applications",
    ("matches.SubmissionBatch", "submitted_by_user"): "submission_batches",
    ("matches.Match", "submitted_by_user"): "submitted_matches",
    ("matches.MatchParticipant", "player"): "matches",
    ("matches.MatchConfirmation", "player"): "confirmations",
    ("matches.MatchVouch", "account"): "vouches",
    ("ratings.RatingState", "player"): "ratings",
    ("ratings.RatingHistory", "player"): "rating_history",
    ("audit.AuditEvent", "actor_user"): "audit_events",
    ("oauth2_provider.AccessToken", "user"): "oauth",
    ("oauth2_provider.RefreshToken", "user"): "oauth",
    ("oauth2_provider.IDToken", "user"): "oauth",
    ("oauth2_provider.Grant", "user"): "oauth",
    ("oauth2_provider.DeviceGrant", "user"): "oauth",
}

# (model label, field name): why it is not exported.
EXCLUDED: dict[tuple[str, str], str] = {
    ("admin.LogEntry", "user"): (
        "Staff admin history; its object_repr can name other records. The "
        "admin changes the Account made are in audit_events (admin.*)."
    ),
    ("clubs.Organizer", "verified_by"): (
        "A staff action on an Organizer, shown as the Account's own "
        "organizer.verify event in audit_events."
    ),
    ("partners.PartnerApplication", "approved_by"): (
        "A staff action on an application, shown as the Account's own "
        "events in audit_events."
    ),
    ("exports.DataExport", "account"): "The export itself.",
    ("clubs.OrganizerInvite", "invited_by"): (
        "Short-lived and deleted once accepted or expired; the sender's own "
        "organizer_invite.create events are in audit_events."
    ),
}


def _links() -> set[tuple[str, str]]:
    """Every (model label, field) with a link to an Account or a Player."""
    targets = {get_user_model(), Player}
    return {
        (model._meta.label, field.name)
        for model in apps.get_models()
        for field in model._meta.concrete_fields
        if field.is_relation
        and (field.many_to_one or field.one_to_one)
        and field.related_model in targets
    }


def _unlisted(links: set[tuple[str, str]]) -> set[tuple[str, str]]:
    return links - set(EXPORTED) - set(EXCLUDED)


def test_every_link_is_exported_or_excluded() -> None:
    assert _unlisted(_links()) == set()


def test_every_listed_link_exists() -> None:
    assert set(EXPORTED) | set(EXCLUDED) <= _links()


def test_no_link_is_both_exported_and_excluded() -> None:
    assert set(EXPORTED) & set(EXCLUDED) == set()


def test_a_new_unlisted_link_is_reported() -> None:
    new = ("madeup.NewThing", "account")

    assert _unlisted(_links() | {new}) == {new}


def test_no_many_to_many_link_to_an_account_or_player() -> None:
    # Groups and permissions are reached from the Account, not to it.
    targets = {get_user_model(), Player}
    found = {
        (model._meta.label, field.name)
        for model in apps.get_models()
        for field in model._meta.many_to_many
        if field.related_model in targets
    }
    assert found == set()
