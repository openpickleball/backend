"""snapshot() accepts only fields whose value it can store as JSON.

Review round 2: a GenericRelation, a ForeignObject that is not a ForeignKey,
and a GenericForeignKey passed the concrete-field check and returned a
manager or a model instance, and the file, binary, and reverse relation
branches had no tests. The throwaway models below live in an isolated app
registry: no table and no migration exist for them.
"""

# The throwaway models are never displayed, so they need no __str__.
# ruff: noqa: DJ008

from typing import Any

import pytest
from django.contrib.contenttypes.fields import GenericForeignKey, GenericRelation
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.test.utils import isolate_apps

from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditAction
from apps.audit.services import record_event, snapshot


def _refused(instance: models.Model, name: str) -> None:
    with pytest.raises(ValueError, match=rf"{name}.*cannot be snapshotted"):
        snapshot(instance, [name])


@isolate_apps("apps.audit")
def test_file_field_gives_its_stored_name() -> None:
    class Document(models.Model):
        upload = models.FileField()

    assert snapshot(Document(upload="made-up/report.txt"), ["upload"]) == {
        "upload": "made-up/report.txt"
    }
    assert snapshot(Document(), ["upload"]) == {"upload": ""}


@isolate_apps("apps.audit")
def test_binary_field_is_refused() -> None:
    class Blob(models.Model):
        data = models.BinaryField()

    _refused(Blob(data=b"made-up bytes"), "data")


@isolate_apps("apps.audit")
def test_generic_relation_is_refused() -> None:
    class Tag(models.Model):
        content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
        object_id = models.PositiveIntegerField()
        tagged = GenericForeignKey("content_type", "object_id")

    class Note(models.Model):
        tags = GenericRelation(Tag)

    _refused(Note(), "tags")


@isolate_apps("apps.audit")
def test_generic_foreign_key_is_refused() -> None:
    class Tag(models.Model):
        content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
        object_id = models.PositiveIntegerField()
        tagged = GenericForeignKey("content_type", "object_id")

    _refused(Tag(), "tagged")


@isolate_apps("apps.audit")
def test_foreign_object_that_is_not_a_foreign_key_is_refused() -> None:
    class Parent(models.Model):
        pass

    class Child(models.Model):
        parent_ref = models.IntegerField(null=True)
        parent = models.ForeignObject(
            Parent,
            on_delete=models.CASCADE,
            from_fields=["parent_ref"],
            to_fields=["id"],
        )

    _refused(Child(), "parent")


@isolate_apps("apps.audit")
def test_plain_concrete_field_gives_its_value() -> None:
    class Plain(models.Model):
        score = models.DecimalField(max_digits=4, decimal_places=2, null=True)

    assert snapshot(Plain(score=None), ["score"]) == {"score": None}


@pytest.mark.django_db
def test_reverse_relation_is_refused() -> None:
    # The admin's LogEntry has a foreign key to the Account, so the Account
    # model has a reverse relation named logentry.
    _refused(UserFactory.create(), "logentry")


@pytest.mark.django_db
def test_foreign_key_still_gives_its_primary_key() -> None:
    actor = UserFactory.create()
    event = record_event(action=AuditAction.SESSION_END, target=actor, actor=actor)

    result: dict[str, Any] = snapshot(event, ["actor_user", "target_content_type"])

    assert result == {
        "actor_user": actor.pk,
        "target_content_type": event.target_content_type_id,
    }
