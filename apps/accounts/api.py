"""The signed-in Account, at /api/v1/accounts/ (issues #30 and #116)."""

from contextlib import suppress

from django.contrib.auth import logout
from django.http import HttpRequest
from ninja import Router, Schema, Status
from ninja.errors import HttpError

from apps.accounts.deletion import AccountAlreadyDeleted, delete_account
from config.api_errors import CONFLICT_STATUS, ErrorResponse
from config.auth import account_auth, require_recent_reauthentication
from config.throttling import account_throttle

router = Router(tags=["accounts"], auth=account_auth, throttle=account_throttle)

STAFF_DELETION_MESSAGE = "A staff account cannot be deleted here."


class CurrentAccount(Schema):
    """The signed-in Account."""

    email: str


@router.get("/me", response=CurrentAccount, operation_id="get_current_account")
def get_current_account(request: HttpRequest) -> CurrentAccount:
    """Return the Account that the session token or session cookie belongs to."""
    return CurrentAccount(email=request.auth.email)  # type: ignore[attr-defined]


@router.delete(
    "/me",
    response={204: None, 403: ErrorResponse, 409: ErrorResponse},
    operation_id="delete_current_account",
)
def delete_current_account(request: HttpRequest) -> Status[None]:
    """Delete your Account and your personal data.

    Reauthenticate first, through allauth's `auth/reauthenticate` (your
    password) or `auth/2fa/reauthenticate` (a two-factor code); without a
    recent one you get 403 with code `reauthentication_required`. Your
    email address, name, birth year and month, gender division, city and
    region, privacy settings, sessions, two-factor methods, and
    organizer memberships go. Matches you played stay in the ledger, with
    your player shown as a private player, so other players' ratings do
    not change. Every session of the Account ends, this one included.
    The same email address can sign up again afterwards. A staff or
    superuser Account gets 409.
    """
    account = request.auth  # type: ignore[attr-defined]
    require_recent_reauthentication(request)
    if account.is_staff or account.is_superuser:
        raise HttpError(CONFLICT_STATUS, STAFF_DELETION_MESSAGE)
    # Deleted by a concurrent request: the same answer as success.
    with suppress(AccountAlreadyDeleted):
        delete_account(account, actor=account)
    # The sessions are gone from the store; drop this request's too, so
    # Django's middleware does not save the browser session back.
    logout(request)
    return Status(204, None)
