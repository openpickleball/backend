"""The legal agreements an Account accepts at sign-up (#222, ADR 0026).

Sign-up requires accepting the current version of each agreement, which
settings hold (``TERMS_OF_SERVICE_VERSION``, ``PRIVACY_POLICY_VERSION``).
The sign-up view holds the accepted versions in the session with the other
signup details, and records them for the new Account only
(``record_acceptance``).
"""

from datetime import datetime
from typing import TYPE_CHECKING

from django.conf import settings

from apps.accounts.models import Agreement, AgreementAcceptance

if TYPE_CHECKING:
    from django.contrib.auth.base_user import AbstractBaseUser


def current_versions() -> dict[str, int]:
    """Each agreement's current version, keyed by its value."""
    return {
        Agreement.TERMS_OF_SERVICE.value: settings.TERMS_OF_SERVICE_VERSION,
        Agreement.PRIVACY_POLICY.value: settings.PRIVACY_POLICY_VERSION,
    }


def record_acceptance(
    account: "AbstractBaseUser", versions: dict[str, int], accepted_at: datetime
) -> None:
    """Record that ``account`` accepted each agreement's version at
    ``accepted_at``. Call it in the transaction that created the Account."""
    AgreementAcceptance.objects.bulk_create(
        AgreementAcceptance(
            account_id=account.pk,
            agreement=agreement,
            version=version,
            accepted_at=accepted_at,
        )
        for agreement, version in versions.items()
    )
