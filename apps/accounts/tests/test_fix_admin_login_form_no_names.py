"""The admin login form never puts a password in a URL (issue #101).

Without its script, a browser submits the form natively. It must post,
and its inputs must have no names, so nothing typed is sent in a query
string or a body. The script reads the inputs by id. Made-up data only.
"""

import re
from typing import Any

import pytest
from django.test import Client
from django.urls import reverse

from apps.accounts.tests.admin import staff_with_totp
from apps.accounts.tests.headless import PASSWORD

pytestmark = pytest.mark.django_db


def _form_tag(content: str) -> str:
    match = re.search(r"<form[^>]*>", content)
    assert match is not None
    return match.group(0)


def _input_names(content: str) -> set[str]:
    return set(re.findall(r'<input[^>]*\bname="([^"]*)"', content))


def _assert_safe(response: Any) -> None:
    content = response.content.decode()
    assert response.status_code == 200
    assert re.search(r'\bmethod="post"', _form_tag(content))
    assert _input_names(content) == {"csrfmiddlewaretoken"}


def test_the_sign_in_form_posts_and_names_no_input(client: Client) -> None:
    _assert_safe(client.get(reverse("admin:login")))


def test_the_code_form_posts_and_names_no_input(client: Client) -> None:
    staff, _ = staff_with_totp()
    assert client.login(username=staff.email, password=PASSWORD)

    _assert_safe(client.get(reverse("admin:login")))
