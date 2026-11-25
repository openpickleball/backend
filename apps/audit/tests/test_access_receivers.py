"""Every concrete access model records its removals (#158)."""

from django.apps import apps
from django.db.models.signals import pre_delete

from apps.audit.access import AuditedAccessModel

EXPECTED = {
    "clubs.organizermember",
    "clubs.clubrole",
    "clubs.cluborganizergrant",
    "partners.organizerapplicationgrant",
}


def test_each_concrete_access_model_has_a_removal_receiver() -> None:
    models = {
        model._meta.label_lower: model
        for model in apps.get_models()
        if issubclass(model, AuditedAccessModel)
    }

    assert set(models) == EXPECTED
    for model in models.values():
        assert pre_delete.has_listeners(model)
