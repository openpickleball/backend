"""A lockout at the admin's code step is shown as too many attempts (#101).

allauth answers the code step (``auth/2fa/authenticate`` and
``auth/2fa/reauthenticate``) with 400 and ``too_many_login_attempts``
once a user has failed it too often, the same status as a wrong code.
OPEN's views turn that into allauth's 429 (#100). The page's script must
show its fixed too-many-attempts message there too. The script has no test runner here, so its code step's source is
checked for the code and the branch that maps it. Made-up data only.
"""

import re
from pathlib import Path
from typing import Any

import pytest

from apps.accounts.tests.admin import sign_in_to_admin, staff_with_totp

pytestmark = pytest.mark.django_db

SCRIPT = (
    Path(__file__).resolve().parent.parent / "static" / "accounts" / "admin_login.js"
)
WRONG = "000000"


@pytest.fixture(autouse=True)
def _one_step_of_clock_drift(settings: Any) -> None:
    settings.MFA_TOTP_TOLERANCE = 1


def test_a_code_step_lockout_gets_429() -> None:
    staff, secret = staff_with_totp()
    session = sign_in_to_admin(staff, secret)

    status = 400
    for _ in range(20):
        status = session.post("auth/2fa/reauthenticate", {"code": WRONG}).status_code
        if status != 400:
            break

    assert status == 429


def _confirm_code_body(source: str) -> str:
    match = re.search(r"async function confirmCode\([\s\S]*?\n}\n", source)
    assert match is not None
    return match.group(0)


def test_the_code_step_shows_the_throttled_message_for_that_code() -> None:
    body = _confirm_code_body(SCRIPT.read_text())

    branch = re.search(
        r"too_many_login_attempts[\s\S]{0,200}?showError\(\"throttled\"\)", body
    )
    assert branch is not None
