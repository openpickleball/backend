"""Partner application tasks (#52), on the default queue."""

import logging

from celery import shared_task
from oauth2_provider.models import clear_expired

logger = logging.getLogger(__name__)


class ClearTokensFailed(Exception):
    """Clearing expired tokens failed, without the error's text.

    Holds only the error's class, so Celery's worker log never prints the
    original text, which can quote values.
    """

    def __init__(self, error_class: str) -> None:
        super().__init__(f"Clearing expired tokens failed ({error_class}).")
        self.error_class = error_class


@shared_task(
    name="apps.partners.tasks.clear_expired_tokens",
    soft_time_limit=540,
    time_limit=600,
)
def clear_expired_tokens() -> None:
    """Delete expired OAuth tokens and grants, as ``cleartokens`` does.

    Runs django-oauth-toolkit's ``clear_expired()``, which deletes expired
    access tokens with no refresh token, expired ID tokens with no access
    token, expired grants (the ``Grant`` model only, never a
    ``DeviceGrant``), and orphaned refresh tokens. Expired access tokens
    that still have a refresh token stay, and so do revoked refresh
    tokens while ``REFRESH_TOKEN_REUSE_PROTECTION`` is on and no refresh
    token lifetime is set, since they let a reused token revoke its
    family. Beat runs it daily. Takes no arguments and
    returns nothing, so nothing personal reaches the worker log. Safe to
    run twice. An error is logged by class only and replaced by a
    fixed-text ``ClearTokensFailed``; the next daily run tries again.
    """
    try:
        clear_expired()
    except Exception as exc:
        error_class = type(exc).__name__
        logger.error("Clearing expired tokens failed (%s).", error_class)
        raise ClearTokensFailed(error_class) from None
