"""The reason box for moderator admin actions (#40).

Every moderator action records why it was taken in its Audit Event. The
admin's action bar gets a text box for that reason, and each action reads
it with ``action_reason`` and checks it with ``reason_problem``. An action
refuses to run on a blank reason or one over ``REASON_MAX_LENGTH``.
"""

from django import forms
from django.contrib.admin.helpers import ActionForm
from django.http import HttpRequest

REASON_MAX_LENGTH = 1000
REASON_TOO_LONG = f"Keep the reason to {REASON_MAX_LENGTH} characters or fewer."


class ReasonActionForm(ActionForm):
    """Django's action form, plus the moderator's reason.

    Neither required nor limited here: an invalid action form makes
    Django say only "No action selected.", so the action itself warns
    about a blank or over-long reason (``reason_problem``).
    """

    reason = forms.CharField(
        required=False,
        label="Reason:",
        widget=forms.TextInput(attrs={"size": 40}),
    )


def action_reason(request: HttpRequest) -> str:
    """The reason posted with the action, stripped, or "" when blank."""
    reason = request.POST.get("reason", "")
    return reason.strip() if isinstance(reason, str) else ""


def reason_problem(reason: str, blank_message: str) -> str | None:
    """The warning for a reason an action must refuse, or None.

    ``blank_message`` for a blank reason, ``REASON_TOO_LONG`` for one over
    the limit. Both are fixed text that never repeats the reason.
    """
    if not reason:
        return blank_message
    if len(reason) > REASON_MAX_LENGTH:
        return REASON_TOO_LONG
    return None
