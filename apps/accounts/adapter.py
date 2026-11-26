"""OPEN's django-allauth account adapter (``ACCOUNT_ADAPTER``)."""

from typing import TYPE_CHECKING, Any

from allauth.account.adapter import DefaultAccountAdapter
from allauth.core import context as allauth_context
from django.contrib.auth import get_user_model
from django.contrib.auth.base_user import AbstractBaseUser
from django.contrib.sites.shortcuts import get_current_site
from django.db import transaction
from django.http import HttpRequest

from apps.accounts.validators import (
    is_canonical_email,
    normalize_email_address,
    normalized_email_errors,
)
from apps.emails.services import queue_email
from apps.players.signup import is_signup_refused, refuse_signup

if TYPE_CHECKING:
    from allauth.account.models import EmailAddress


class AccountAdapter(DefaultAccountAdapter):
    """allauth's account adapter, with OPEN's email rules (ADR 0003)."""

    def logout(self, request: HttpRequest) -> None:
        """allauth's logout, which flushes the session, keeping a signup
        refusal (issue #114).

        Without this, a refused session could log out and sign up again
        with another age. Only the flag carries over, nothing else.
        """
        refused = is_signup_refused(request.session)
        super().logout(request)
        if refused:
            refuse_signup(request.session)

    def clean_email(self, email: str) -> str:
        """Normalize an address, then apply the Account email rules to it.

        allauth calls this for a new address: at sign-up, and when an
        Account adds an address (with ACCOUNT_CHANGE_EMAIL, how it changes
        its address). Login and the password reset request never call it:
        they check the address with is_canonical_email instead (here in
        authenticate, and in OPEN's RequestPasswordResetInput in
        headless_views.py). The address is normalized like the manager does
        (NFKC, strip, lowercase per character), then checked for ASCII,
        length, and format, in that order. The error carries the rule's
        fixed message, never the submitted value.
        """
        normalized = normalize_email_address(email)
        errors = normalized_email_errors(normalized)
        if errors:
            raise errors[0]
        cleaned: str = super().clean_email(normalized)
        return cleaned

    def authenticate(
        self, request: HttpRequest, **credentials: Any
    ) -> AbstractBaseUser | None:
        """Refuse a non-canonical address before any backend sees it.

        Both backends normalize the address and so would find the Account
        for a variant that NFKC folds onto its address (U+017F long s, or
        fullwidth letters). allauth would then treat the variant as a new,
        unverified address of that Account. A variant is no Account's login
        (ADR 0003), so it fails like a wrong password: the login rate limit
        is still consumed, the password is still hashed (the same time as a
        miss), and no backend runs.
        """
        email = credentials.get("email")
        if isinstance(email, str) and not is_canonical_email(email):
            self.pre_authenticate(request, **credentials)
            get_user_model()().set_password(credentials.get("password"))
            self.authentication_failed(request, **credentials)
            return None
        user: AbstractBaseUser | None = super().authenticate(request, **credentials)
        return user

    def _get_login_attempts_cache_key(
        self, request: HttpRequest, **credentials: Any
    ) -> str:
        """The failed login counter's key: the submitted address alone.

        allauth prefixes the current site's domain, which, with no sites
        app, is the Host header as sent. An allowed host with another
        port or letter case passes Django's host check, so each spelling
        would start a fresh counter (#100). allauth's password reset
        clears the counter through this same key.
        """
        login = credentials.get("email", credentials.get("username", ""))
        return f"login:{str(login).lower()}"

    def send_mail(
        self, template_prefix: str, email: str, context: dict[str, Any]
    ) -> None:
        """Queue every allauth email instead of sending it in the request.

        The message is rendered here with allauth's own context (the
        request, the address, the current site, then the flow's context),
        as allauth's send_mail does, and stored by ``queue_email``. A task
        on the email queue sends it after the transaction commits, so the
        request never waits on the provider, and the address and any key
        stay out of the task's arguments (ADR 0016).
        """
        request = allauth_context.request
        ctx: dict[str, Any] = {
            "request": request,
            "email": email,
            "current_site": get_current_site(request),
        }
        ctx.update(context)
        message = self.render_mail(template_prefix, email, ctx)
        queue_email(message, template=template_prefix)

    def send_password_reset_mail(
        self, user: AbstractBaseUser, email: str, context: dict[str, Any]
    ) -> None:
        """Mail the reset key to the Account's stored address only.

        allauth passes the submitted spelling as ``email``. A key goes to
        ``User.email``, never to that spelling, and a submitted address that
        is not canonical gets nothing, like an unknown address (the answer is
        the same either way).
        """
        if not is_canonical_email(email):
            return
        stored = getattr(user, get_user_model().EMAIL_FIELD)
        self.send_mail("account/email/password_reset_key", stored, context)

    def confirm_email(
        self, request: HttpRequest, email_address: "EmailAddress"
    ) -> bool:
        """Verify an address in one transaction.

        Verifying with ACCOUNT_CHANGE_EMAIL changes several rows: the old
        primary EmailAddress, the new one, the Account's User.email, and
        then removes the other addresses. If any write fails (the database
        refuses a User.email that another Account took meanwhile), all of
        them roll back, so User.email and the EmailAddress rows never split.
        """
        with transaction.atomic():
            confirmed: bool = super().confirm_email(request, email_address)
        return confirmed
