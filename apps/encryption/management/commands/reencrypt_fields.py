"""Re-encrypt every encrypted value with the newest key (ADR 0046).

Rotation: put the new key first in ``FIELD_ENCRYPTION_KEYS``, keep the
old ones after it, run this command, then remove the old keys. It
rewrites each encrypted model field and each allauth MFA secret, row by
row, each row in its own transaction under a row lock, and prints only
counts. A value no listed key decrypts stops the run with an error that
names the model and row id, never the value; rows already rewritten keep
the newest key, so a rerun after the fix resumes.

The stored values do not change, so no Audit Event is recorded.
"""

from typing import Any

from allauth.mfa.models import Authenticator
from django.apps import apps
from django.core.management.base import BaseCommand, CommandError
from django.db import models, transaction

from apps.encryption.fields import EncryptedFieldMixin
from apps.encryption.keys import DecryptionError, rotate_text


def _encrypted_models() -> list[tuple[type[models.Model], list[str]]]:
    found = []
    for model in apps.get_models():
        names = [
            field.name
            for field in model._meta.concrete_fields
            if isinstance(field, EncryptedFieldMixin)
        ]
        if names:
            found.append((model, names))
    return found


def _undecryptable(label: str, pk: object) -> CommandError:
    return CommandError(
        f"A value in {label} row {pk} could not be decrypted with any key in "
        "FIELD_ENCRYPTION_KEYS. Add the key it was encrypted with, then run "
        "this command again."
    )


def _rotated_data(data: dict[str, Any]) -> dict[str, Any]:
    new = dict(data)
    for key in ("secret", "seed"):
        if isinstance(new.get(key), str):
            new[key] = rotate_text(new[key])
    codes = new.get("migrated_codes")
    if isinstance(codes, list):
        new["migrated_codes"] = [
            rotate_text(code) if isinstance(code, str) else code for code in codes
        ]
    return new


class Command(BaseCommand):
    help = (
        "Re-encrypt every encrypted value with the newest key in FIELD_ENCRYPTION_KEYS."
    )

    def handle(self, *args: Any, **options: Any) -> None:
        for model, names in _encrypted_models():
            count = self._reencrypt_model(model, names)
            self.stdout.write(f"{model._meta.label}: {count} rows re-encrypted.")
        count = self._reencrypt_authenticators()
        self.stdout.write(f"{Authenticator._meta.label}: {count} rows re-encrypted.")

    def _reencrypt_model(self, model: type[models.Model], names: list[str]) -> int:
        manager = model._base_manager
        count = 0
        for pk in manager.order_by("pk").values_list("pk", flat=True):
            with transaction.atomic():
                try:
                    values = (
                        manager.select_for_update().filter(pk=pk).values(*names).first()
                    )
                except DecryptionError:
                    raise _undecryptable(model._meta.label, pk) from None
                if values is None:
                    continue
                # update() encrypts each value again with the newest key,
                # without save() and its rules: the values do not change.
                manager.filter(pk=pk).update(**values)
                count += 1
        return count

    def _reencrypt_authenticators(self) -> int:
        manager = Authenticator.objects
        count = 0
        types = [Authenticator.Type.TOTP, Authenticator.Type.RECOVERY_CODES]
        pks = manager.filter(type__in=types).order_by("pk").values_list("pk", flat=True)
        for pk in pks:
            with transaction.atomic():
                row = manager.select_for_update().filter(pk=pk).first()
                if row is None or not isinstance(row.data, dict):
                    continue
                try:
                    data = _rotated_data(row.data)
                except DecryptionError:
                    raise _undecryptable(Authenticator._meta.label, pk) from None
                manager.filter(pk=pk).update(data=data)
                count += 1
        return count
