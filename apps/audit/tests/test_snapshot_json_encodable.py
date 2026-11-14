"""snapshot() refuses a plain field whose value the JSON encoder cannot store.

Review round 3: a concrete non-relational field can still hold a value that
DjangoJSONEncoder rejects (a date range, an array of bytes), and
record_event then failed with TypeError at insert. snapshot must refuse it
up front, naming the field and never its value.
"""

# The throwaway models are never displayed, so they need no __str__.
# ruff: noqa: DJ008

import datetime

import pytest
from django.contrib.postgres.fields import ArrayField, DateRangeField
from django.db import models
from django.db.backends.postgresql.psycopg_any import DateRange
from django.test.utils import isolate_apps

from apps.audit.services import snapshot


def _assert_refused(instance: models.Model, name: str, raw: str) -> None:
    with pytest.raises(ValueError, match=rf"{name}.*cannot be snapshotted") as excinfo:
        snapshot(instance, [name])

    assert raw not in str(excinfo.value)
    # Raised "from None", so the encoder's message (with the value) is not
    # chained onto it.
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__


@isolate_apps("apps.audit")
def test_date_range_field_is_refused() -> None:
    class Season(models.Model):
        dates = DateRangeField()

    season = Season(
        dates=DateRange(datetime.date(2026, 1, 1), datetime.date(2026, 2, 1))
    )

    _assert_refused(season, "dates", "2026-01-01")


@isolate_apps("apps.audit")
def test_array_of_bytes_is_refused() -> None:
    class Packet(models.Model):
        chunks = ArrayField(models.BinaryField())

    _assert_refused(Packet(chunks=[b"made-up-bytes"]), "chunks", "made-up-bytes")


@isolate_apps("apps.audit")
def test_array_of_text_is_still_accepted() -> None:
    class Labels(models.Model):
        names = ArrayField(models.CharField(max_length=20))

    assert snapshot(Labels(names=["made-up", "labels"]), ["names"]) == {
        "names": ["made-up", "labels"]
    }
