"""A passkey registration whose client extension results are not an
object, whose ``credProps`` is not an object, or whose ``rk`` is not a
boolean is refused with allauth's 400 ``incorrect_code`` and stores
nothing, never a server error or a passkey listed with a non-boolean
``is_passwordless`` (issue #117). Made-up data only."""

from typing import Any

import pytest

from apps.accounts.tests.headless import APP, BROWSER
from apps.accounts.tests.passkey_flows import (
    changes,
    creation_options,
    error_codes,
    new_passkey,
    passkeys,
    post_registration,
    signed_in,
    use_passkey_settings,
    verified_account,
)

pytestmark = pytest.mark.django_db

RESULTS: dict[str, object] = {
    "null": None,
    "list": ["credProps"],
    "cred-props-null": {"credProps": None},
    "cred-props-text": {"credProps": "x"},
    "rk-text": {"credProps": {"rk": "yes"}},
    "rk-number": {"credProps": {"rk": 1}},
}


@pytest.fixture(autouse=True)
def _passkey_settings(settings: Any) -> None:
    use_passkey_settings(settings)


@pytest.mark.parametrize("base", [BROWSER, APP], ids=["browser", "app"])
@pytest.mark.parametrize("results", list(RESULTS))
def test_malformed_extension_results_are_refused(base: str, results: str) -> None:
    account = verified_account()
    session = signed_in(base, account)
    credential = new_passkey().register(creation_options(session))
    credential["clientExtensionResults"] = RESULTS[results]

    response = post_registration(session, credential)

    assert response.status_code == 400
    assert error_codes(response) == ["incorrect_code"]
    assert passkeys(account) == []
    assert changes(account) == []


@pytest.mark.parametrize(
    "results",
    [{}, {"credProps": {}}, {"credProps": {"rk": False}}],
    ids=["empty", "cred-props-empty", "rk-false"],
)
def test_well_formed_extension_results_are_accepted(results: object) -> None:
    account = verified_account()
    session = signed_in(BROWSER, account)
    credential = new_passkey().register(creation_options(session))
    credential["clientExtensionResults"] = results

    response = post_registration(session, credential)

    assert response.status_code == 200, response.content
    assert response.json()["data"].get("is_passwordless") in (None, False)
