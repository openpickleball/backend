"""allauth's failed login lockout, answered with 429 (#100, ADR 0047).

allauth refuses a request over ``login_failed`` (per client IP, and per
address or MFA user) with a 400 and ``too_many_login_attempts``, like a
wrong password or code. OPEN's login, MFA login stage, password
reauthentication, and MFA reauthentication views take
``LockoutAnswers429`` first in their bases, so that lockout answers with
allauth's own 429, as every other rate limit does.
"""

from typing import TYPE_CHECKING

from allauth.headless.base.response import RateLimitResponse

if TYPE_CHECKING:
    from allauth.headless.internal.restkit import inputs
    from django.http import HttpRequest, HttpResponseBase

TOO_MANY_LOGIN_ATTEMPTS = "too_many_login_attempts"


def error_codes(form: "inputs.Input") -> set[str]:
    """Every error code on ``form``, its non-field errors included."""
    return {
        str(error.code)
        for errors in form.errors.as_data().values()
        for error in errors
        if error.code
    }


class LockoutAnswers429:
    """A headless view whose lockout refusal is allauth's 429."""

    request: "HttpRequest"

    def handle_invalid_input(self, input: "inputs.Input") -> "HttpResponseBase":
        if TOO_MANY_LOGIN_ATTEMPTS in error_codes(input):
            limited: HttpResponseBase = RateLimitResponse(self.request)
            return limited
        invalid: HttpResponseBase = super().handle_invalid_input(input)  # type: ignore[misc]
        return invalid
