"""The stored engine snapshot (#46, ADR 0027).

One ``EngineSnapshot`` row per parameter fingerprint. Only the live update
service writes it, so the ORM refuses deletes, bulk inserts, bulk changes,
and upserts. Made-up data only.
"""

from typing import Any

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from apps.ratings.models import EngineSnapshot, EngineSnapshotWriteRefused

pytestmark = pytest.mark.django_db

FINGERPRINT = "0123456789abcdef"


def _values(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "parameter_fingerprint": FINGERPRINT,
        "config_fingerprint": "fedcba9876543210",
        "config_version": "1.0.0",
        "params_version": "v1",
        "engine_version": "9.9.9",
        "snapshot": {"snapshot_version": 9},
    }
    values.update(overrides)
    return values


def _make(**overrides: Any) -> EngineSnapshot:
    row: EngineSnapshot = EngineSnapshot.objects.create(**_values(**overrides))
    return row


def test_str_is_the_parameter_fingerprint() -> None:
    assert str(_make()) == FINGERPRINT


def test_one_row_per_parameter_fingerprint() -> None:
    _make()
    with pytest.raises(IntegrityError), transaction.atomic():
        _make()


def test_full_clean_reports_a_second_row_plainly() -> None:
    _make()
    with pytest.raises(ValidationError) as caught:
        EngineSnapshot(**_values()).full_clean()
    assert "parameter_fingerprint" in caught.value.message_dict


def test_an_empty_snapshot_is_valid() -> None:
    EngineSnapshot(**_values(snapshot={})).full_clean()


@pytest.mark.parametrize(
    "field",
    ["parameter_fingerprint", "config_fingerprint", "config_version", "engine_version"],
)
def test_the_database_refuses_a_blank_label(field: str) -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        _make(**{field: ""})


def test_params_version_may_be_none_but_not_blank() -> None:
    _make(params_version=None)
    with pytest.raises(IntegrityError), transaction.atomic():
        _make(parameter_fingerprint="another", params_version="")


def test_a_snapshot_that_is_not_a_mapping_is_refused() -> None:
    with pytest.raises(ValidationError) as caught:
        EngineSnapshot(**_values(snapshot=[1, 2])).full_clean()
    assert "snapshot" in caught.value.message_dict


def test_delete_is_refused() -> None:
    row = _make()
    with pytest.raises(EngineSnapshotWriteRefused):
        row.delete()
    assert EngineSnapshot.objects.filter(pk=row.pk).exists()


@pytest.mark.parametrize(
    "call",
    [
        lambda qs: qs.update(config_version="2.0.0"),
        lambda qs: qs.delete(),
        lambda qs: qs.bulk_update(list(qs), ["config_version"]),
        lambda qs: qs.bulk_create(
            [EngineSnapshot(**_values(parameter_fingerprint="b"))]
        ),
        lambda qs: qs.bulk_create(
            [EngineSnapshot(**_values())],
            update_conflicts=True,
            update_fields=["snapshot"],
            unique_fields=["parameter_fingerprint"],
        ),
    ],
    ids=["update", "delete", "bulk_update", "bulk_create", "upsert"],
)
def test_bulk_writes_are_refused(call: Any) -> None:
    _make()
    with pytest.raises(EngineSnapshotWriteRefused):
        call(EngineSnapshot.objects.all())
    with pytest.raises(EngineSnapshotWriteRefused):
        call(EngineSnapshot._base_manager.all())


def test_the_refusal_is_fixed_text() -> None:
    assert str(EngineSnapshotWriteRefused()) == EngineSnapshotWriteRefused.message
