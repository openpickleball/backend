"""Club and Organizer links refuse a bad id without echoing it (issue #162).

Django's foreign key messages name the submitted id: an unknown id gets
"<model> instance with id <value> is not a valid choice.", and an id of
the wrong type gets the target key's "'<value>' value must be an
integer.". Each link here gives fixed text instead. Made-up data only.
"""

from typing import Any

import pytest
from django.core.exceptions import ValidationError
from django.db import models

from apps.accounts.tests.factories import UserFactory
from apps.clubs.models import (
    ClubOrganizerGrant,
    ClubRole,
    MemberRole,
    Organizer,
    OrganizerMember,
)
from apps.clubs.tests.factories import ClubFactory, OrganizerFactory

pytestmark = pytest.mark.django_db

MARKER = "zqxjmarker"
UNKNOWN_ID = 987654321
BAD_IDS = [UNKNOWN_ID, MARKER]

ORGANIZER = "Choose an existing organizer."
ACCOUNT = "Choose an existing account."
CLUB = "Choose an existing club."


def _messages(instance: models.Model) -> dict[str, list[str]]:
    with pytest.raises(ValidationError) as excinfo:
        instance.full_clean()
    return excinfo.value.message_dict


def _assert_fixed(instance: models.Model, field: str, expected: str) -> None:
    messages = _messages(instance)
    assert messages[field] == [expected]
    text = " ".join(" ".join(items) for items in messages.values())
    assert MARKER not in text
    assert str(UNKNOWN_ID) not in text


def _member(**links: Any) -> OrganizerMember:
    fields: dict[str, Any] = {"role": MemberRole.ADMIN}
    fields.setdefault("organizer", OrganizerFactory.create())
    fields.setdefault("account", UserFactory.create())
    for name, value in links.items():
        fields.pop(name.removesuffix("_id"))
        fields[name] = value
    return OrganizerMember(**fields)


@pytest.mark.parametrize("bad", BAD_IDS)
def test_member_organizer_bad_id(bad: object) -> None:
    _assert_fixed(_member(organizer_id=bad), "organizer", ORGANIZER)


@pytest.mark.parametrize("bad", BAD_IDS)
def test_member_account_bad_id(bad: object) -> None:
    _assert_fixed(_member(account_id=bad), "account", ACCOUNT)


@pytest.mark.parametrize("bad", BAD_IDS)
def test_club_role_club_bad_id(bad: object) -> None:
    role = ClubRole(club_id=bad, account=UserFactory.create(), role="admin")
    _assert_fixed(role, "club", CLUB)


@pytest.mark.parametrize("bad", BAD_IDS)
def test_club_role_account_bad_id(bad: object) -> None:
    role = ClubRole(club=ClubFactory.create(), account_id=bad, role="admin")
    _assert_fixed(role, "account", ACCOUNT)


@pytest.mark.parametrize("bad", BAD_IDS)
def test_grant_club_bad_id(bad: object) -> None:
    grant = ClubOrganizerGrant(club_id=bad, organizer=OrganizerFactory.create())
    _assert_fixed(grant, "club", CLUB)


@pytest.mark.parametrize("bad", BAD_IDS)
def test_grant_organizer_bad_id(bad: object) -> None:
    grant = ClubOrganizerGrant(club=ClubFactory.create(), organizer_id=bad)
    _assert_fixed(grant, "organizer", ORGANIZER)


@pytest.mark.parametrize("bad", BAD_IDS)
def test_organizer_verified_by_bad_id(bad: object) -> None:
    organizer = Organizer(name="Made-up Organizer", verified_by_id=bad)
    _assert_fixed(organizer, "verified_by", ACCOUNT)


@pytest.mark.parametrize("field", ["organizer_id", "account_id"])
def test_member_wrong_type_id_is_a_validation_error_not_a_crash(field: str) -> None:
    # clean() looked up the Organizer's verifier with the raw value and
    # raised ValueError.
    member = _member(**{field: MARKER})

    messages = _messages(member)

    assert field.removesuffix("_id") in messages
    assert MARKER not in str(messages)
