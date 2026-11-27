"""Questions about an Account's email addresses (#229).

``has_verified_login_address`` asks whether the Account's own login
address (``User.email``) has a verified ``EmailAddress`` row. Only that
row counts: a verified old address, or another pending one, does not.
Read from the database by the Account's id.
"""

from typing import TYPE_CHECKING

from allauth.account.models import EmailAddress

if TYPE_CHECKING:
    from django.contrib.auth.models import _User


def has_verified_login_address(account: "_User") -> bool:
    """Whether ``account``'s login address is verified."""
    if account.pk is None:
        return False
    return bool(
        EmailAddress.objects.filter(
            user_id=account.pk, email=account.email, verified=True
        ).exists()
    )
