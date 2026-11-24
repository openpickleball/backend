"""The Audit Event action a moderator's bulk void records (#205)."""

from apps.audit.models import AuditAction


def test_bulk_void_action_exists() -> None:
    assert AuditAction["MATCH_BULK_VOID"].value == "match.bulk_void"
    assert AuditAction.MATCH_BULK_VOID.label == "Matches voided in bulk"
