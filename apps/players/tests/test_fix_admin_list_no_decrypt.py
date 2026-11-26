"""The Player admin list never decrypts the encrypted fields
(issue #115, ADR 0046).

It shows none of them, so one row no configured key decrypts must not
turn the whole list into a server error.
"""

import pytest
from cryptography.fernet import Fernet
from django.contrib.auth.models import Permission
from django.db import connection
from django.test import Client
from django.urls import reverse

from apps.accounts.tests.factories import UserFactory
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db


def test_the_list_renders_with_a_row_no_key_decrypts(client: Client) -> None:
    good = PlayerFactory.create()
    bad = PlayerFactory.create()
    foreign = Fernet(Fernet.generate_key()).encrypt(b"1990").decode()
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE players_player SET legal_name = %s, birth_year = %s, "
            "birth_month = %s, gender_division = %s WHERE id = %s",
            [foreign, foreign, foreign, foreign, bad.pk],
        )
    staff = UserFactory.create(is_staff=True)
    staff.user_permissions.add(
        Permission.objects.get(
            content_type__app_label="players", codename="view_player"
        )
    )
    client.force_login(staff)

    response = client.get(reverse("admin:players_player_changelist"))

    assert response.status_code == 200
    text = response.content.decode()
    assert good.public_code in text
    assert bad.public_code in text
