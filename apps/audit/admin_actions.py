"""The reason box for moderator admin actions (#40).

Every moderator action records why it was taken in its Audit Event. The
admin's action bar gets a text box for that reason, and each action reads
it with ``action_reason``. An action refuses to run on a blank reason.
"""

from django import forms
from django.contrib.admin.helpers import ActionForm
from django.http import HttpRequest

REASON_MAX_LENGTH = 1000


class ReasonActionForm(ActionForm):
    """Django's action form, plus the moderator's reason.

    Not required here, so the action itself can warn about a blank one.
    A reason over the limit makes the form invalid, and Django then runs
    no action.
    """

    reason = forms.CharField(
        required=False,
        max_length=REASON_MAX_LENGTH,
        label="Reason:",
        widget=forms.TextInput(attrs={"size": 40}),
    )


def action_reason(request: HttpRequest) -> str:
    """The reason posted with the action, stripped, or "" when blank."""
    reason = request.POST.get("reason", "")
    return reason.strip() if isinstance(reason, str) else ""
