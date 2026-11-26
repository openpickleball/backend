"""A locked-out staff member is told so on the admin login page (#101).

allauth answers a login it refuses for too many failed attempts with 400
and the error code ``too_many_login_attempts``, the same status as a
wrong password. OPEN's login view turns that into allauth's 429 (#100),
and allauth's code step keeps the 400 and its code. The page's script
must show its fixed too-many-attempts message for both, not "The email
or password is wrong." The script has no test runner here, so its source
is checked for the code and the branch that maps it. Made-up data only.
"""

import re
from pathlib import Path

import pytest

from apps.accounts.tests.admin import staff_with_totp
from apps.accounts.tests.headless import BROWSER, PASSWORD, HeadlessSession

pytestmark = pytest.mark.django_db

SCRIPT = (
    Path(__file__).resolve().parent.parent / "static" / "accounts" / "admin_login.js"
)


def test_a_rate_limited_login_gets_429() -> None:
    staff, _ = staff_with_totp()
    session = HeadlessSession(BROWSER)
    for _ in range(10):
        session.login(staff.email, "not-the-password")

    response = session.login(staff.email, PASSWORD)

    assert response.status_code == 429


def test_the_script_shows_the_throttled_message_for_429() -> None:
    source = SCRIPT.read_text()

    assert re.search(r"status === 429\)\s*\{\s*showError\(\"throttled\"\)", source)


def test_the_script_shows_the_throttled_message_for_that_code() -> None:
    source = SCRIPT.read_text()

    assert "too_many_login_attempts" in source
    # The code leads to the fixed throttled message.
    branch = re.search(
        r"too_many_login_attempts[\s\S]{0,200}?showError\(\"throttled\"\)", source
    )
    assert branch is not None
