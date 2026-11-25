"""The Audit Event actions the match confirmation workflow records (#26)."""

import pytest

from apps.audit.models import AuditAction


@pytest.mark.parametrize(
    ("member", "value"),
    [
        ("MATCH_CONFIRM", "match.confirm"),
        ("MATCH_DISPUTE", "match.dispute"),
        ("MATCH_DISPUTE_WITHDRAW", "match.dispute_withdraw"),
        ("MATCH_VOID", "match.void"),
        ("MATCH_EXPIRE", "match.expire"),
        ("MATCH_EDIT", "match.edit"),
        ("DISPUTE_OUTCOME", "dispute.outcome"),
    ],
)
def test_match_workflow_action_exists(member: str, value: str) -> None:
    assert AuditAction[member].value == value
