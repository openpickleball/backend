"""The Player changelist refuses every query-string lookup (#190).

The Player admin searches by OPEN Code only and has no filters, so a
lookup on a name, a place, or an encrypted field (``isnull``) in the
query string must not list Players. Made-up data only.
"""

from collections.abc import Callable

import pytest
from django.contrib.auth.models import Group
from django.test import Client
from django.urls import reverse

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.matches.permissions import MODERATOR_GROUP
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db

LOOKUPS = [
    "display_name__icontains=Zephyrine",
    "city__iexact=Madeupville&region=Madeup Region",
    "birth_year__isnull=True",
]


def _moderator() -> User:
    account: User = UserFactory.create(is_staff=True)
    account.groups.add(Group.objects.get(name=MODERATOR_GROUP))
    return account


def _superuser() -> User:
    account: User = UserFactory.create(is_staff=True, is_superuser=True)
    return account


@pytest.mark.parametrize(
    "make", [_moderator, _superuser], ids=["moderator", "superuser"]
)
@pytest.mark.parametrize("query", LOOKUPS)
def test_a_query_string_lookup_is_refused(
    client: Client, make: Callable[[], User], query: str
) -> None:
    PlayerFactory.create(
        display_name="Zephyrine Madeup", city="Madeupville", region="Madeup Region"
    )
    client.force_login(make())

    response = client.get(f"{reverse('admin:players_player_changelist')}?{query}")

    assert response.status_code == 400
    assert "Zephyrine" not in response.content.decode()


@pytest.mark.parametrize(
    "make", [_moderator, _superuser], ids=["moderator", "superuser"]
)
def test_the_list_and_the_code_search_still_work(
    client: Client, make: Callable[[], User]
) -> None:
    player = PlayerFactory.create()
    other = PlayerFactory.create()
    client.force_login(make())
    url = reverse("admin:players_player_changelist")

    assert client.get(url).status_code == 200
    response = client.get(url, {"q": player.public_code})

    assert response.status_code == 200
    text = response.content.decode()
    assert player.public_code in text
    assert other.public_code not in text
