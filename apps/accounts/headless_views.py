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
- Sign-up age screen (issues #114, #215, and #221, ADR 0017): the birth
  year and month, the gender division, the first and last name, and the
  city and region (`SignupAgeForm`) are checked before anything else. Under 13 gets one fixed refusal, whatever the email and
  password, before allauth reads them, so no lookup, Account, or mail
  results. The session is then flagged, and every later sign-up in it
  gets the same refusal. Thirteen and older signs up as before, and the
  details are held in the session, bound to the new
  Account (or to none, for a taken address, so both answers stay alike),
  for the Player create endpoint (#124). The accepted legal agreements
  (#222, ADR 0026) are checked with them, held with them, and recorded
  for a new Account.
- Sign-up age check (issue #223, ADR 0020): the birth year and month
  alone (`SignupBirthForm`), before the app asks for a name or a location,
  with the age screen's rules, order, rate limit, and refusal. A pass keeps
  nothing in the session; under 13 flags it as a refused sign-up does.
  It is OPEN's own view, at a new path under allauth's sign-up.
- Sign-up password check (issue #289, ADR 0039): the password alone, with
  the email only for the similarity rule, through the same validators and
  error format as sign-up, before the app asks for the age. It finds no
  Account and writes nothing to the session. Sign-up checks the password
  against its email the same way (`clean_signup_password`).
- allauth's OpenAPI document: served with the age and password checks
  added (`apps/accounts/openapi.py`).
- MFA (issue #99, ADR 0043): allauth's TOTP, recovery codes, MFA login,
  and MFA reauthentication views, recording an Audit Event for each
  change in the change's transaction (`apps/accounts/mfa_views.py`).
- Login (#100): allauth's failed login limit answers 429.
- Sessions, password change, and password reset from a key (#100, ADR
  0047): the session list by opaque ids, and audited changes that end
  other sessions (``apps/accounts/session_views.py``).
- Email verification: an address that is another Account's login cannot
  be verified (allauth's `email_taken`, a 400). allauth checks only other
  verified EmailAddress rows, so verifying would set the address as this
  Account's `User.email` and break the unique constraint with a 500.
"""

import json
from typing import TYPE_CHECKING, Any, ClassVar

import yaml
from allauth.account.adapter import get_adapter
from allauth.account.models import EmailAddress
from allauth.account.utils import user_email
from allauth.decorators import rate_limit
from allauth.headless.account.inputs import (
    RequestPasswordResetInput as BaseRequestPasswordResetInput,
)
from allauth.headless.account.inputs import SignupInput as BaseSignupInput
from allauth.headless.account.inputs import VerifyEmailInput as BaseVerifyEmailInput
from allauth.headless.account.views import LoginView as BaseLoginView
from allauth.headless.account.views import (
    RequestPasswordResetView as BaseRequestPasswordResetView,
)
from allauth.headless.account.views import SignupView as BaseSignupView
from allauth.headless.account.views import VerifyEmailView as BaseVerifyEmailView
from allauth.headless.base.response import (
    ConflictResponse,
    ForbiddenResponse,
    RateLimitResponse,
)
from allauth.headless.base.views import APIView
from allauth.headless.constants import Client
from allauth.headless.internal.restkit import inputs
from allauth.headless.internal.restkit.response import APIResponse, ErrorResponse
from allauth.headless.spec.views import OpenAPIJSONView as BaseOpenAPIJSONView
from allauth.headless.spec.views import OpenAPIYAMLView as BaseOpenAPIYAMLView
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import HttpResponse
from django.urls import URLPattern, path
from django.utils import timezone
from django.utils.decorators import method_decorator

from apps.accounts.agreements import current_versions, record_acceptance
from apps.accounts.forms import SignupAgeForm, SignupBirthForm, signup_full_name
from apps.accounts.mfa_views import (
    AuthenticateView,
    ManageRecoveryCodesView,
    ManageTOTPView,
    ReauthenticateView,
)
from apps.accounts.openapi import open_schema
from apps.accounts.session_views import SessionsView
from apps.accounts.validators import is_canonical_email, normalize_email_address
from apps.players.age import is_old_enough
from apps.players.signup import (
    SIGNUP_DETAILS_SESSION_KEY,
    SignupDetails,
    hold_signup_details,
    is_signup_refused,
    refuse_signup,
)

if TYPE_CHECKING:
    from collections.abc import Callable

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


# allauth's error code when its failed login limit refuses a login.
_TOO_MANY_LOGIN_ATTEMPTS = "too_many_login_attempts"


class LoginView(BaseLoginView):
    """allauth's login, answering its failed login limit with 429 (#100).

    allauth refuses a login over ``login_failed`` (per client IP, and per
    address) with a 400 and ``too_many_login_attempts``, like a wrong
    password. OPEN answers it with allauth's own 429, as every other rate
    limit answers. allauth's ``login`` limit still decorates ``handle``.
    """

    def handle_invalid_input(self, input: "inputs.Input") -> "HttpResponseBase":
        if _TOO_MANY_LOGIN_ATTEMPTS in _error_codes(input):
            limited: HttpResponseBase = RateLimitResponse(self.request)
            return limited
        invalid: HttpResponseBase = super().handle_invalid_input(input)
        return invalid


def _error_codes(form: "inputs.Input") -> set[str]:
    """Every error code on ``form``, its non-field errors included."""
    return {
        str(error.code)
        for errors in form.errors.as_data().values()
        for error in errors
        if error.code
    }


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


def clean_signup_password(password: str, email: str | None) -> None:
    """Sign-up's password rules (issue #289, ADR 0039), or ValidationError.

    allauth's own password check, with an unsaved Account that holds only
    the address, so ``AUTH_PASSWORD_VALIDATORS`` runs whole: the similarity
    rule included, which allauth's headless sign-up skips (it passes no
    Account). Nothing is saved or looked up. The error lists one entry per
    failed rule, with Django's message and code. Sign-up and the password
    check both call this, so they always agree.
    """
    account = get_user_model()(email=email or "")
    get_adapter().clean_password(password, user=account)


def _check_password(form: "inputs.Input", email: str | None) -> None:
    """Add sign-up's password errors to ``form``'s ``password`` field."""
    password = form.cleaned_data.get("password")
    if not password:
        return
    try:
        clean_signup_password(password, email)
    except ValidationError as error:
        form.add_error("password", error)


class SignupInput(BaseSignupInput):
    """allauth's sign-up input, with an address taken only by a login.

    allauth treats an address as taken when any EmailAddress row has it,
    including another Account's unverified, pending change of address. Here
    only an Account whose `User.email` it is takes it. A taken address keeps
    allauth's enumeration-safe answer (the owner is mailed that an account
    exists); any other address signs up and gets a verification link.

    The password is checked in ``clean()``, against the cleaned address,
    by ``clean_signup_password`` (ADR 0039), as the password check does.
    """

    def validate_unique_email(self, value: str) -> str:
        self.account_already_exists = is_any_accounts_login(value)
        email: str = get_adapter().validate_unique_email(value)
        return email

    def clean_password(self) -> str:
        # Checked in clean(), once the address is cleaned.
        password: str = self.cleaned_data["password"]
        return password

    def clean(self) -> dict[str, Any]:
        cleaned: dict[str, Any] = super().clean()
        _check_password(self, cleaned.get("email"))
        return cleaned

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

        The accepted version of each legal agreement is held with the
        details, and recorded for a new Account in the transaction that
        creates it (#222, ADR 0026). A taken address records nothing.
        """
        versions = current_versions()
        with transaction.atomic():
            user, response = super().try_save(request)
            if user is not None:
                record_acceptance(user, versions, timezone.now())
        session = request.session
        held = session.get(SIGNUP_DETAILS_SESSION_KEY)
        if user is None and _is_held_for_login(held, self.cleaned_data.get("email")):
            # Written back, not skipped, so the session changes as on the
            # other paths.
            session[SIGNUP_DETAILS_SESSION_KEY] = held
        else:
            cleaned = self.cleaned_data
            details = SignupDetails(
                birth_year=cleaned["birth_year"],
                birth_month=cleaned["birth_month"],
                gender_division=cleaned["gender_division"],
                legal_name=signup_full_name(
                    cleaned["first_name"], cleaned["last_name"]
                ),
                city=cleaned["city"],
                region=cleaned["region"],
                first_name=cleaned["first_name"],
            )
            hold_signup_details(session, user, details, versions)
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
    """allauth's sign-up, behind the age screen (issues #114, #215, #221).

    Order: allauth's rate limit, then its signed-in (409) and closed
    signup (403) answers, made as allauth makes them, then the age screen
    (every signup detail besides the email and password), then allauth's
    input (and so its email lookup). allauth itself answers invalid input
    before its 409 and 403; here a missing or malformed signup detail comes after them too,
    so the screen only ever answers, or flags, a session that allauth
    would let sign up.
    """

    input_class: ClassVar[dict[str, type[SignupInput]]] = {"POST": SignupInput}

    def handle(
        self, request: "HttpRequest", *args: object, **kwargs: object
    ) -> "HttpResponseBase":
        if request.method == "POST":
            early = _signup_not_allowed(request) or self._screen_age(request)
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
        return _screen_birth(request, SignupAgeForm, self._parse_json)


def _signup_not_allowed(request: "HttpRequest") -> "HttpResponseBase | None":
    """allauth's sign-up answers for a signed-in session (409) and for
    closed sign-up (403), made as allauth makes them, or None."""
    answer: HttpResponseBase | None = None
    if request.user.is_authenticated:
        answer = ConflictResponse(request)
    elif not get_adapter().is_open_for_signup(request):
        answer = ForbiddenResponse(request)
    return answer


def _screen_birth(
    request: "HttpRequest",
    form_class: type[SignupBirthForm],
    parse_json: "Callable[[HttpRequest], dict[str, Any] | None]",
) -> "HttpResponseBase | None":
    """The age screen: a response that ends the request, or None to go on.

    A flagged session gets the refusal without its body being read, so
    the answer depends on nothing sent: ``parse_json`` (allauth's, which
    answers a body that is not a JSON object with a bare 400) runs only
    after the flag check. Otherwise a missing or malformed field gets
    allauth's 400 with only the form's errors, and under 13 flags the
    session and gets the refusal.
    """
    if is_signup_refused(request.session):
        return _refusal(request)
    form = form_class(data=parse_json(request) or {})
    if not form.is_valid():
        invalid: HttpResponseBase = ErrorResponse(request, input=form)
        return invalid
    if not is_old_enough(
        form.cleaned_data["birth_year"], form.cleaned_data["birth_month"]
    ):
        refuse_signup(request.session)
        return _refusal(request)
    return None


# allauth's own signup limit, on the same counter as sign-up, so the check
# gives no attempts that sign-up would not, and a 429 comes first.
@method_decorator(rate_limit(action="signup"), name="handle")
class SignupAgeCheckView(APIView):
    """The sign-up age check (issue #223, ADR 0020).

    The birth year and month alone, with the age screen's rules and order:
    allauth's rate limit, its 409 and 403, the flag, the input, the age.
    It reads no email address, so its answer says nothing about one. A
    pass writes nothing to the session: the sign-up request sends and
    checks the age itself. Under 13 flags the session (``refuse_signup``)
    as a refused sign-up does.
    """

    # The body is parsed only after allauth's 409 and 403, as at sign-up.
    handle_json_input = False

    def post(
        self, request: "HttpRequest", *args: object, **kwargs: object
    ) -> "HttpResponseBase":
        answer = _signup_not_allowed(request) or _screen_birth(
            request, SignupBirthForm, self._parse_json
        )
        if answer is not None:
            return answer
        passed: HttpResponseBase = APIResponse(request)
        return passed


_NOT_TEXT = "Enter text."


class SignupPasswordField(inputs.CharField):
    """Sign-up's password field, refusing anything but a JSON string.

    Django's conversion would turn a number or a list into text. Otherwise
    the same as allauth's sign-up field (stripped, then required).
    """

    def to_python(self, value: object) -> str:
        if value not in self.empty_values and not isinstance(value, str):
            raise ValidationError(_NOT_TEXT, code="invalid")
        text: str = super().to_python(value)
        return text


class SignupPasswordCheckInput(inputs.Input):
    """The password check's input: the password, and the address only for
    the similarity rule.

    The address is never validated or looked up: a missing, non-string, or
    malformed one is simply not compared (the password still gets every
    other rule), so the answer never depends on whether an Account has it.
    It is normalized as sign-up normalizes it, so both compare the same
    text.
    """

    password = SignupPasswordField()

    def clean(self) -> dict[str, Any]:
        cleaned: dict[str, Any] = super().clean() or {}
        email = self.data.get("email")
        _check_password(
            self, normalize_email_address(email) if isinstance(email, str) else None
        )
        return cleaned


# allauth's own signup limit, on the same counter as sign-up and the age
# check, so a 429 comes first.
@method_decorator(rate_limit(action="signup"), name="handle")
class SignupPasswordCheckView(APIView):
    """The sign-up password check (issue #289, ADR 0039).

    In the age check's order: allauth's rate limit, its 409 and 403, the
    session's refusal flag, then the input. A pass is ``{"status": 200}``;
    a failed rule is allauth's 400 with one ``password`` error per rule,
    the same errors sign-up gives. It finds no Account or address, writes
    nothing to the session, and sends no mail.
    """

    # The body is parsed only after allauth's 409 and 403, as at sign-up.
    handle_json_input = False

    def post(
        self, request: "HttpRequest", *args: object, **kwargs: object
    ) -> "HttpResponseBase":
        answer = _signup_not_allowed(request)
        if answer is None and is_signup_refused(request.session):
            answer = _refusal(request)
        if answer is not None:
            return answer
        form = SignupPasswordCheckInput(data=self._parse_json(request) or {})
        if not form.is_valid():
            invalid: HttpResponseBase = ErrorResponse(request, input=form)
            return invalid
        passed: HttpResponseBase = APIResponse(request)
        return passed


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


class OpenAPIJSONView(BaseOpenAPIJSONView):
    """allauth's OpenAPI document as JSON, with OPEN's signup checks."""

    def get(self, request: "HttpRequest") -> HttpResponse:
        return HttpResponse(
            json.dumps(open_schema()),
            content_type="application/vnd.oai.openapi+json",
            headers={"Content-Disposition": "inline; filename=allauth-openapi.json"},
        )


class OpenAPIYAMLView(BaseOpenAPIYAMLView):
    """allauth's OpenAPI document as YAML, with OPEN's signup checks."""

    def get(self, request: "HttpRequest") -> HttpResponse:
        return HttpResponse(
            yaml.dump(open_schema(), Dumper=yaml.Dumper),
            content_type="application/vnd.oai.openapi",
            headers={"Content-Disposition": "inline; filename=allauth-openapi.yaml"},
        )


def headless_urlpatterns() -> list[URLPattern]:
    """OPEN's views at allauth's paths, for each enabled headless client,
    and its own age and password checks under allauth's sign-up.

    The MFA views assume ``MFA_SUPPORTED_TYPES`` holds TOTP and recovery
    codes (settings, ADR 0043), as allauth would mount them."""
    patterns = []
    if settings.HEADLESS_SERVE_SPECIFICATION:
        patterns += [
            path("openapi.yaml", OpenAPIYAMLView.as_view()),
            path("openapi.json", OpenAPIJSONView.as_view()),
        ]
    for client in (Client.BROWSER, Client.APP):
        if client.value not in settings.HEADLESS_CLIENTS:
            continue
        prefix = f"{client.value}/v1/auth/"
        patterns += [
            path(f"{prefix}login", LoginView.as_api_view(client=client)),
            path(f"{prefix}sessions", SessionsView.as_api_view(client=client)),
            path(f"{prefix}signup", SignupView.as_api_view(client=client)),
            path(
                f"{prefix}signup/age-check",
                SignupAgeCheckView.as_api_view(client=client),
            ),
            path(
                f"{prefix}signup/password-check",
                SignupPasswordCheckView.as_api_view(client=client),
            ),
            path(
                f"{prefix}password/request",
                RequestPasswordResetView.as_api_view(client=client),
            ),
            path(
                f"{prefix}email/verify",
                VerifyEmailView.as_api_view(client=client),
            ),
            path(
                f"{prefix}2fa/authenticate",
                AuthenticateView.as_api_view(client=client),
            ),
            path(
                f"{prefix}2fa/reauthenticate",
                ReauthenticateView.as_api_view(client=client),
            ),
            path(
                f"{client.value}/v1/account/authenticators/totp",
                ManageTOTPView.as_api_view(client=client),
            ),
            path(
                f"{client.value}/v1/account/authenticators/recovery-codes",
                ManageRecoveryCodesView.as_api_view(client=client),
            ),
        ]
    return patterns
