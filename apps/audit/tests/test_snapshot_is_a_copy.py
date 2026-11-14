"""snapshot() stores a copy, so a later in-place change cannot alter it.

Review round 4: for JSONField and ArrayField values, snapshot returned the
live list or dict. Changing it in place after taking ``before`` changed
``before`` too, so the recorded diff showed no change.
"""

# The throwaway models are never displayed, so they need no __str__.
# ruff: noqa: DJ008

from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.test.utils import isolate_apps

from apps.audit.services import snapshot


@isolate_apps("apps.audit")
def test_array_field_snapshot_survives_in_place_change() -> None:
    class Labelled(models.Model):
        tags = ArrayField(models.CharField(max_length=20))

    obj = Labelled(tags=["made-up"])
    before = snapshot(obj, ["tags"])

    obj.tags.append("added")

    assert before == {"tags": ["made-up"]}


@isolate_apps("apps.audit")
def test_json_field_snapshot_survives_in_place_change() -> None:
    class Configured(models.Model):
        data = models.JSONField(default=dict)

    obj = Configured(data={"level": 1, "nested": {"items": ["a"]}})
    before = snapshot(obj, ["data"])

    obj.data["level"] = 2
    obj.data["nested"]["items"].append("b")

    assert before == {"data": {"level": 1, "nested": {"items": ["a"]}}}
