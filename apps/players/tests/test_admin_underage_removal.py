"""The Player admin's removal of an under-13 player's data (#190, ADR 0059).

An Account with ``players.view_player`` and
``players.remove_underage_player`` sees a button on the Player change
page. It leads to a confirmation page naming only the OPEN Code; its
POST runs ``remove_underage_player``, which records the one Audit Event.
Made-up data only.
"""

from typing import Any

import pytest
from django.contrib.auth.models import Permission
from django.contrib.messages import get_messages
from django.test import Client
from django.urls import reverse

from apps.accounts.deletion import DELETED_PLAYER_NAME
from apps.accounts.models import User
from apps.accounts.tests.admin import age_mfa_steps
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditAction, AuditEvent
from apps.events.models import EventKind
from apps.events.tests.factories import EventFactory
from apps.matches.models import Team
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory
from apps.ratings.tests.builders import saved_match

pytestmark = pytest.mark.django_db

DISPLAY_NAME = "Wrenna Madeup"
LEGAL_NAME = "Wrenna Made-up Legalname"
BUTTON = "Remove personal data (under 13)"


def _staff(*codenames: str) -> User:
    account: User = UserFactory.create(is_staff=True)
    account.user_permissions.add(
        *Permission.objects.filter(
            content_type__app_label="players", codename__in=codenames
        )
    )
    return account


def _moderator() -> User:
    return _staff("view_player", "remove_underage_player")


def _player(*, played: bool = True) -> Player:
    player: Player = PlayerFactory.create(
        display_name=DISPLAY_NAME, legal_name=LEGAL_NAME
    )
    if played:
        others = [PlayerFactory.create() for _ in range(3)]
        saved_match(
            players={Team.A: [player, others[0]], Team.B: others[1:]},
            event=EventFactory.create(kind=EventKind.TOURNAMENT),
        )
    return player


def _change(player: Player) -> str:
    return reverse("admin:players_player_change", args=[player.pk])


def _remove(player: Player) -> str:
    return reverse("admin:players_player_remove_underage", args=[player.pk])


def _events() -> list[AuditEvent]:
    return list(AuditEvent.objects.filter(action=AuditAction.PLAYER_UNDERAGE_REMOVE))


def _messages(response: Any) -> list[str]:
    return [str(message) for message in get_messages(response.wsgi_request)]


def test_the_button_shows_to_an_account_with_both_permissions(client: Client) -> None:
    player = _player()
    client.force_login(_moderator())

    response = client.get(_change(player))

    assert response.status_code == 200
    text = response.content.decode()
    assert BUTTON in text
    assert _remove(player) in text


def test_a_superuser_sees_the_button(client: Client) -> None:
    player = _player()
    client.force_login(UserFactory.create(is_staff=True, is_superuser=True))

    assert BUTTON in client.get(_change(player)).content.decode()


def test_without_the_permission_there_is_no_button(client: Client) -> None:
    player = _player()
    client.force_login(_staff("view_player", "view_personal_data"))

    response = client.get(_change(player))

    assert response.status_code == 200
    assert BUTTON not in response.content.decode()


def test_the_confirmation_page_names_only_the_open_code(client: Client) -> None:
    player = _player()
    client.force_login(_moderator())

    response = client.get(_remove(player))

    assert response.status_code == 200
    text = response.content.decode()
    assert player.public_code in text
    assert DISPLAY_NAME not in text
    assert LEGAL_NAME not in text
    assert player.account.email not in text
    assert 'method="post"' in text
    assert not _events()
    player.refresh_from_db()
    assert player.display_name == DISPLAY_NAME


def test_a_post_removes_a_kept_player_and_returns_to_its_page(client: Client) -> None:
    player = _player()
    moderator = _moderator()
    client.force_login(moderator)
    events = AuditEvent.objects.count()

    response = client.post(_remove(player))

    assert response.status_code == 302
    assert response["Location"] == _change(player)
    player.refresh_from_db()
    assert player.display_name == DELETED_PLAYER_NAME
    assert player.legal_name == ""
    assert player.account.is_active is False
    [event] = _events()
    assert event.actor_user_id == moderator.pk
    # The removal's own event only: the view records nothing else.
    assert not AuditEvent.objects.filter(action__startswith="admin.").exists()
    assert AuditEvent.objects.count() == events + 1
    [message] = _messages(response)
    assert player.public_code in message
    assert DISPLAY_NAME not in message


def test_a_post_for_an_unprotected_player_returns_to_the_list(client: Client) -> None:
    player = _player(played=False)
    client.force_login(_moderator())

    response = client.post(_remove(player))

    assert response.status_code == 302
    assert response["Location"] == reverse("admin:players_player_changelist")
    assert not Player.objects.filter(pk=player.pk).exists()
    assert len(_events()) == 1


@pytest.mark.parametrize(
    "codenames", [("view_player",), ("remove_underage_player",), ()], ids=str
)
@pytest.mark.parametrize("method", ["get", "post"])
def test_without_both_permissions_it_is_refused(
    client: Client, codenames: tuple[str, ...], method: str
) -> None:
    player = _player()
    client.force_login(_staff(*codenames))

    response = getattr(client, method)(_remove(player))

    assert response.status_code == 403
    player.refresh_from_db()
    assert player.display_name == DISPLAY_NAME
    assert not _events()


def test_a_refused_removal_shows_an_error_and_changes_nothing(client: Client) -> None:
    player = _player()
    player.account.is_staff = True
    player.account.save(update_fields=["is_staff"])
    client.force_login(_moderator())

    response = client.post(_remove(player))

    assert response.status_code == 302
    assert response["Location"] == _change(player)
    player.refresh_from_db()
    assert player.display_name == DISPLAY_NAME
    assert not _events()
    [message] = _messages(response)
    assert player.public_code in message
    assert DISPLAY_NAME not in message


def test_an_already_removed_player_shows_an_error(client: Client) -> None:
    player = _player()
    client.force_login(_moderator())
    client.post(_remove(player))

    response = client.post(_remove(player))

    assert response.status_code == 302
    assert len(_events()) == 1
    assert len(_messages(response)) == 2


def test_an_unknown_player_gets_404(client: Client) -> None:
    client.force_login(_moderator())

    url = reverse("admin:players_player_remove_underage", args=[0])
    assert client.get(url).status_code == 404
    assert client.post(url).status_code == 404
    assert not _events()


def test_a_removal_needs_a_csrf_token() -> None:
    player = _player()
    client = Client(enforce_csrf_checks=True)
    client.force_login(_moderator())

    response = client.post(_remove(player))

    assert response.status_code == 403
    assert not _events()


def test_a_removal_without_a_recent_code_is_sent_to_the_code_page(
    client: Client, settings: Any
) -> None:
    settings.ADMIN_REAUTHENTICATION_SECONDS = 30
    player = _player()
    client.force_login(_moderator())
    age_mfa_steps(client.session, 60)

    response = client.post(_remove(player))

    assert response.status_code == 302
    assert response["Location"].startswith(reverse("admin:login"))
    player.refresh_from_db()
    assert player.display_name == DISPLAY_NAME
    assert not _events()


def test_an_anonymous_removal_removes_nothing(client: Client) -> None:
    player = _player()

    response = client.post(_remove(player))

    assert response.status_code == 302
    assert response["Location"].startswith(reverse("admin:login"))
    assert not _events()
