"""OPEN's replacements for some of allauth's headless views (issue #30).

allauth's headless API takes no form classes from settings, so where an
input needs OPEN's rules, the view is subclassed with an OPEN input, and
`headless_urlpatterns()` serves it at allauth's own path, ahead of
allauth's URLconf (`config/urls.py`).

- Password reset request: the Account is found only through the manager's
  `get_by_natural_key` on the canonical address, active Accounts only
  (ADR 0003). allauth matches every EmailAddress row instead.
- Sign-up: an address is taken only when it is some Account's `User.email`
  (ADR 0003). allauth also counts every other EmailAddress row, so an
  address another Account had only added, unverified, blocked sign-up.
- Sign-up age screen (issues #114 and #215, ADR 0017): the birth year
  and month and the gender division (`SignupAgeForm`) are checked before
  anything else. Under 13 gets one fixed refusal, whatever the email and
  password, before allauth reads them, so no lookup, Account, or mail
  results. The session is then flagged, and every later sign-up in it
  gets the same refusal. Thirteen and older signs up as before, and the
  age and gender division are held in the session, bound to the new
  Account (or to none, for a taken address, so both answers stay alike),
  for the Player create endpoint (#124).
- Email verification: an address that is another Account's login cannot
  be verified (allauth's `email_taken`, a 400). allauth checks only other
  verified EmailAddress rows, so verifying would set the address as this
  Account's `User.email` and break the unique constraint with a 500.
"""

from typing import TYPE_CHECKING, ClassVar

from allauth.account.adapter import get_adapter
from allauth.account.models import EmailAddress
from allauth.account.utils import user_email
from allauth.decorators import rate_limit
from allauth.headless.account.inputs import (
    RequestPasswordResetInput as BaseRequestPasswordResetInput,
)
from allauth.headless.account.inputs import SignupInput as BaseSignupInput
from allauth.headless.account.inputs import VerifyEmailInput as BaseVerifyEmailInput
from allauth.headless.account.views import (
    RequestPasswordResetView as BaseRequestPasswordResetView,
)
from allauth.headless.account.views import SignupView as BaseSignupView
from allauth.headless.account.views import VerifyEmailView as BaseVerifyEmailView
from allauth.headless.base.response import ConflictResponse, ForbiddenResponse
from allauth.headless.constants import Client
from allauth.headless.internal.restkit.response import ErrorResponse
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.urls import URLPattern, path
from django.utils.decorators import method_decorator

from apps.accounts.forms import SignupAgeForm
from apps.accounts.validators import is_canonical_email
from apps.players.age import is_old_enough
from apps.players.signup import (
    SIGNUP_DETAILS_SESSION_KEY,
    SIGNUP_REFUSED_SESSION_KEY,
    SignupDetails,
    hold_signup_details,
)

if TYPE_CHECKING:
    from django.contrib.auth.base_user import AbstractBaseUser
    from django.http import HttpRequest, HttpResponseBase


def account_for_login(email: str) -> "AbstractBaseUser | None":
    """The active Account whose login is this address, or None.

    Only a canonical address can name an Account, and only through the
    manager's natural key lookup (ADR 0003, CLAUDE.md "Account emails").
    """
    if not is_canonical_email(email):
        return None
    manager = get_user_model()._default_manager
    try:
        account = manager.get_by_natural_key(email)
    except manager.model.DoesNotExist:
        return None
    return account if account.is_active else None


class RequestPasswordResetInput(BaseRequestPasswordResetInput):
    """allauth's reset request, finding the Account by its login only.

    Never raises for the address: an unknown or non-canonical address gets
    the same answer as a known one and no mail (enumeration prevention).
    """

    def clean_email(self) -> str:
        email: str = self.cleaned_data["email"]
        account = account_for_login(email)
        self.users = [account] if account else []
        return email


class RequestPasswordResetView(BaseRequestPasswordResetView):
    input_class = RequestPasswordResetInput


def is_any_accounts_login(email: str) -> bool:
    """Whether the address is some Account's login (User.email)."""
    manager = get_user_model()._default_manager
    try:
        manager.get_by_natural_key(email)
    except manager.model.DoesNotExist:
        return False
    return True


class SignupInput(BaseSignupInput):
    """allauth's sign-up input, with an address taken only by a login.

    allauth treats an address as taken when any EmailAddress row has it,
    including another Account's unverified, pending change of address. Here
    only an Account whose `User.email` it is takes it. A taken address keeps
    allauth's enumeration-safe answer (the owner is mailed that an account
    exists); any other address signs up and gets a verification link.
    """

    def validate_unique_email(self, value: str) -> str:
        self.account_already_exists = is_any_accounts_login(value)
        email: str = get_adapter().validate_unique_email(value)
        return email

    def try_save(
        self, request: "HttpRequest"
    ) -> "tuple[AbstractBaseUser | None, HttpResponseBase | None]":
        """allauth's save, then the signup details held for the new Account.

        A taken address creates no Account, and its details are held bound
        to none: both paths write the session alike, so the app client's
        answers (a session token or not) stay the same (enumeration). One
        exception: when the session already holds details bound to the
        Account whose login is this address (a repeated signup), that hold
        is written back unchanged, so the Account keeps its details.
        """
        user, response = super().try_save(request)
        session = request.session
        held = session.get(SIGNUP_DETAILS_SESSION_KEY)
        if user is None and _is_held_for_login(held, self.cleaned_data.get("email")):
            # Written back, not skipped, so the session changes as on the
            # other paths.
            session[SIGNUP_DETAILS_SESSION_KEY] = held
        else:
            details = SignupDetails(
                birth_year=self.cleaned_data["birth_year"],
                birth_month=self.cleaned_data["birth_month"],
                gender_division=self.cleaned_data["gender_division"],
            )
            hold_signup_details(session, user, details)
        return user, response

    def save(self, request: "HttpRequest") -> "AbstractBaseUser":
        user: AbstractBaseUser = super().save(request)
        # allauth's setup skips an address another Account's pending change
        # still holds, which would leave the new Account without a row to
        # verify. Give it its own primary, unverified row.
        if not EmailAddress.objects.filter(user=user).exists():
            EmailAddress.objects.create(
                user=user, email=user_email(user), primary=True, verified=False
            )
        return user


def _is_held_for_login(held: object, email: object) -> bool:
    """Whether ``held`` is bound to the Account whose login is ``email``."""
    if not isinstance(held, dict) or held.get("account") is None:
        return False
    if not isinstance(email, str):
        return False
    manager = get_user_model()._default_manager
    try:
        account = manager.get_by_natural_key(email)
    except manager.model.DoesNotExist:
        return False
    return bool(account.pk == held["account"])


# allauth's own signup limit, on OPEN's handle, so every signup POST
# counts once, age screen answers included, and a 429 comes first.
@method_decorator(rate_limit(action="signup"), name="handle")
class SignupView(BaseSignupView):
    """allauth's sign-up, behind the age screen (issues #114 and #215).

    Order: allauth's rate limit, then its signed-in (409) and closed
    signup (403) answers, made as allauth makes them, then the age screen
    (the age and the gender division), then allauth's input (and so its
    email lookup). allauth itself answers invalid input before its 409 and
    403; here a malformed age or gender division comes after them too,
    so the screen only ever answers, or flags, a session that allauth
    would let sign up.
    """

    input_class: ClassVar[dict[str, type[SignupInput]]] = {"POST": SignupInput}

    def handle(
        self, request: "HttpRequest", *args: object, **kwargs: object
    ) -> "HttpResponseBase":
        if request.method == "POST":
            early: HttpResponseBase | None = None
            if request.user.is_authenticated:
                early = ConflictResponse(request)
            elif not get_adapter().is_open_for_signup(request):
                early = ForbiddenResponse(request)
            else:
                early = self._screen_age(request)
            if early is not None:
                return early
        # Past allauth's SignupView, whose handle carries the same rate
        # limit: calling it would count this request a second time.
        response: HttpResponseBase = super(BaseSignupView, self).handle(
            request, *args, **kwargs
        )
        return response

    def _screen_age(self, request: "HttpRequest") -> "HttpResponseBase | None":
        """A response that ends the sign-up, or None to go on."""
        if request.session.get(SIGNUP_REFUSED_SESSION_KEY):
            # Never reads the body: the answer depends on nothing sent.
            return _refusal(request)
        age = SignupAgeForm(data=self._parse_json(request) or {})
        if not age.is_valid():
            # allauth's 400, with only the signup details' errors.
            invalid: HttpResponseBase = ErrorResponse(request, input=age)
            return invalid
        if not is_old_enough(
            age.cleaned_data["birth_year"], age.cleaned_data["birth_month"]
        ):
            # A flag only, never the age or gender division (ADR 0009).
            request.session.pop(SIGNUP_DETAILS_SESSION_KEY, None)
            request.session[SIGNUP_REFUSED_SESSION_KEY] = True
            return _refusal(request)
        return None


# One fixed answer, whatever was sent: no field, no age, no detail.
_SIGNUP_REFUSED = "We can't create an account with these details."


def _refusal(request: "HttpRequest") -> "HttpResponseBase":
    """allauth's 400 for an under-13 sign-up, and for every later one in
    the session. It carries no ``param``."""
    error = ValidationError(_SIGNUP_REFUSED, code="signup_refused")
    response: HttpResponseBase = ErrorResponse(request, exception=error)
    return response


def is_another_accounts_login(email: str, account_id: object) -> bool:
    """Whether the address is the login (User.email) of another Account."""
    manager = get_user_model()._default_manager
    try:
        holder = manager.get_by_natural_key(email)
    except manager.model.DoesNotExist:
        return False
    return bool(holder.pk != account_id)


class VerifyEmailInput(BaseVerifyEmailInput):
    """allauth's verify input, refusing another Account's login.

    The person verifying holds the key mailed to the address, so the
    refusal tells them nothing their mailbox does not (a sign-up with it
    would mail them that an account exists). The message is allauth's
    fixed text and never names the address.
    """

    def clean_key(self) -> str:
        key: str = super().clean_key()
        address = (
            self.process.email_address
            if self.process
            else self.verification.email_address
        )
        if not address.verified and is_another_accounts_login(
            address.email, address.user_id
        ):
            raise get_adapter().validation_error("email_taken")
        return key


class VerifyEmailView(BaseVerifyEmailView):
    input_class = VerifyEmailInput


def headless_urlpatterns() -> list[URLPattern]:
    """OPEN's views at allauth's paths, for each enabled headless client."""
    patterns = []
    for client in (Client.BROWSER, Client.APP):
        if client.value not in settings.HEADLESS_CLIENTS:
            continue
        prefix = f"{client.value}/v1/auth/"
        patterns += [
            path(f"{prefix}signup", SignupView.as_api_view(client=client)),
            path(
                f"{prefix}password/request",
                RequestPasswordResetView.as_api_view(client=client),
            ),
            path(
                f"{prefix}email/verify",
                VerifyEmailView.as_api_view(client=client),
            ),
        ]
    return patterns
