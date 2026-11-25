"""Public codes on records, and the registry of every code ever issued.

A model whose records have a public identity (a player, club, event,
organizer, match, or results file) inherits ``PublicCodeModel`` and sets
``public_code_type``. See ADR 0008.
"""

from collections.abc import Collection, Iterable
from typing import Any, ClassVar, NoReturn, Self

from django.db import IntegrityError, models, router, transaction
from django.utils import timezone

from apps.public_ids import codes
from apps.public_ids.codes import CodeType

# Attempts to find an unused code before giving up. One collision is rare
# (see ADR 0008), so five in a row means something is wrong.
MAX_ATTEMPTS = 5


class IssuedCodeImmutable(RuntimeError):
    """Raised when code tries to change or delete an IssuedCode."""


class PublicCodeExhausted(RuntimeError):
    """Raised when every attempt to issue a new code collided."""


_IMMUTABLE_MESSAGE = "Issued codes are permanent: they cannot be changed or deleted."


class IssuedCodeQuerySet(models.QuerySet["IssuedCode"]):
    """Refuses bulk changes and deletes before any SQL runs."""

    def update(self, **kwargs: Any) -> NoReturn:
        raise IssuedCodeImmutable(_IMMUTABLE_MESSAGE)

    def delete(self) -> NoReturn:
        raise IssuedCodeImmutable(_IMMUTABLE_MESSAGE)

    def bulk_update(self, *args: Any, **kwargs: Any) -> NoReturn:
        raise IssuedCodeImmutable(_IMMUTABLE_MESSAGE)

    def bulk_create(
        self,
        objs: Iterable["IssuedCode"],
        batch_size: int | None = None,
        ignore_conflicts: bool = False,
        update_conflicts: bool = False,
        update_fields: Collection[str] | None = None,
        unique_fields: Collection[str] | None = None,
    ) -> list["IssuedCode"]:
        # An upsert would update an existing row.
        if update_conflicts:
            raise IssuedCodeImmutable(_IMMUTABLE_MESSAGE)
        return super().bulk_create(
            objs,
            batch_size=batch_size,
            ignore_conflicts=ignore_conflicts,
            update_fields=update_fields,
            unique_fields=unique_fields,
        )


class IssuedCode(models.Model):
    """One code that has been issued. The row stays when its record goes.

    The prefix names the record type, so there is no link to the record.
    The ORM refuses to change or delete a row. There is no database
    trigger: raw SQL can still delete rows (see ADR 0008).
    """

    code = models.CharField(max_length=32, unique=True, editable=False)
    issued_at = models.DateTimeField(default=timezone.now, editable=False)

    objects = IssuedCodeQuerySet.as_manager()

    class Meta:
        verbose_name = "issued code"
        # Django's internal queries use the base manager, so it must be
        # the guarded one too.
        base_manager_name = "objects"

    def __str__(self) -> str:
        return self.code

    def save_base(
        self,
        raw: bool = False,
        force_insert: bool | tuple[models.base.ModelBase, ...] = False,
        force_update: bool = False,
        using: str | None = None,
        update_fields: Iterable[str] | None = None,
    ) -> None:
        if (
            force_update
            or update_fields is not None
            or not self._state.adding
            or self.pk is not None
        ):
            raise IssuedCodeImmutable(_IMMUTABLE_MESSAGE)
        super().save_base(
            raw=raw,
            force_insert=force_insert,
            force_update=force_update,
            using=using,
            update_fields=update_fields,
        )

    def delete(self, *args: Any, **kwargs: Any) -> NoReturn:
        raise IssuedCodeImmutable(_IMMUTABLE_MESSAGE)


def _generate(code_type: CodeType) -> str:
    """Generate a code. A separate function so tests can force collisions."""
    return codes.generate(code_type)


def _issue_code(model: type["PublicCodeModel"], using: str) -> str:
    """Record and return a code that has never been issued.

    A code is a collision if the registry has it, or if a record of
    ``model`` already holds it (a code written outside ``save()`` has no
    registry row).
    """
    code_type = model.public_code_type
    # The model that owns the column: a multi-table child's own manager
    # would not see rows of its parent that are not children.
    owner = model._meta.get_field("public_code").model
    for _ in range(MAX_ATTEMPTS):
        code = _generate(code_type)
        if owner._base_manager.using(using).filter(public_code=code).exists():
            continue
        try:
            # A savepoint, so a collision leaves the caller's transaction usable.
            with transaction.atomic(using=using):
                IssuedCode.objects.using(using).create(code=code)
        except IntegrityError:
            continue
        return code
    raise PublicCodeExhausted(
        f"No unused {code_type.name.lower()} code after {MAX_ATTEMPTS} attempts."
    )


# Marks a loaded record whose public_code was deferred.
_NOT_LOADED = object()


class PublicCodeModel(models.Model):
    """A record with a public code, issued once on create and never changed.

    Concrete subclasses set ``public_code_type``. Create records through
    ``save()`` or ``objects.create()``. Fixtures (``loaddata``),
    ``bulk_create``, ``QuerySet.update``, and raw SQL skip issuance and the
    no-change rule, and must not set codes. The field's default is None, so
    such a write without a code fails on NOT NULL.

    To copy a record, set ``public_code = None``, ``pk = None``, and
    ``_state.adding = True`` before saving: the copy gets a fresh code.
    Saving a deleted record raises, and so does saving a record whose row
    is gone.
    """

    public_code_type: ClassVar[CodeType]

    # default=None with null=False: a write that skips save() fails
    # instead of storing an empty code.
    public_code = models.CharField(
        max_length=32, unique=True, editable=False, default=None
    )

    _loaded_public_code: object
    _loaded_pk: object

    class Meta:
        abstract = True

    def save(self, *args: Any, **kwargs: Any) -> None:
        if args:
            raise TypeError("Pass save() options by keyword.")
        if not self._state.adding:
            self._save_existing(**kwargs)
            return
        if self.public_code is not None:
            raise ValueError("A public code is only ever generated, never set.")
        kwargs["force_insert"] = self._insert_everywhere(kwargs.get("force_insert"))
        using = kwargs.get("using") or router.db_for_write(type(self), instance=self)
        keys = self._key_attnames()
        original_keys = {name: getattr(self, name) for name in keys}
        try:
            with transaction.atomic(using=using):
                self.public_code = _issue_code(type(self), using)
                super().save(**kwargs)
        except BaseException:
            # Nothing was stored: the registry row and the record roll back
            # together, even when the commit itself failed. Leave the
            # instance new, with every key as it was, so a retry issues a
            # fresh code.
            self.public_code = None
            for name, value in original_keys.items():
                setattr(self, name, value)
            self._state.adding = True
            raise
        self._loaded_public_code = self.public_code
        self._loaded_pk = self.pk

    def _insert_everywhere(
        self, force_insert: bool | tuple[models.base.ModelBase, ...] | None
    ) -> tuple[models.base.ModelBase, ...]:
        """Force an INSERT into this model's table and every parent's table.

        A new instance carrying an existing pk must not overwrite that row's
        code. ``force_insert=True`` covers only this model's own table, so a
        multi-table parent would get UPDATE then INSERT.
        """
        tables: list[models.base.ModelBase] = [
            type(self),
            *self._meta.get_parent_list(),
        ]
        if isinstance(force_insert, tuple):
            tables.extend(force_insert)
        return tuple(dict.fromkeys(tables))

    def _key_attnames(self) -> list[str]:
        """Every key attribute a save can set: what a failed create resets.

        That is the pk of this model and of every parent, and every parent
        link at every level. A model with two concrete parents links one of
        them through a field that is not its pk.
        """
        names: list[str] = []
        for model in [type(self), *self._meta.get_parent_list()]:
            fields = [model._meta.pk, *model._meta.parents.values()]
            for field in fields:
                if field is not None and field.attname not in names:
                    names.append(field.attname)
        return names

    def _save_existing(self, **kwargs: Any) -> None:
        """Update the stored row. Never insert, and never change a code."""
        if self.pk is None:
            raise ValueError(
                "This record has no primary key: it was deleted. "
                "Save a new instance to create a record."
            )
        self._refuse_code_change()
        # Only update: if the row is gone (deleted, or its create rolled
        # back), Django raises instead of inserting it with this code.
        kwargs["force_update"] = True
        super().save(**kwargs)

    @classmethod
    def from_db(
        cls,
        db: str | None,
        field_names: Collection[str],
        values: Collection[Any],
        *args: Any,
        **kwargs: Any,
    ) -> Self:
        instance = super().from_db(db, field_names, values, *args, **kwargs)
        # Remember the loaded row and code, so save() can refuse a changed one.
        instance._loaded_public_code = instance.__dict__.get("public_code", _NOT_LOADED)
        instance._loaded_pk = instance.pk
        return instance

    def _refuse_code_change(self) -> None:
        if "public_code" not in self.__dict__:
            # Deferred and never touched: save() does not write it.
            return
        loaded = getattr(self, "_loaded_public_code", _NOT_LOADED)
        if self.pk != getattr(self, "_loaded_pk", _NOT_LOADED):
            # Pointed at another row: compare with the code stored there.
            loaded = _NOT_LOADED
        if loaded is _NOT_LOADED:
            loaded = (
                type(self)
                ._base_manager.using(self._state.db)
                .filter(pk=self.pk)
                .values_list("public_code", flat=True)
                .first()
            )
        if self.public_code != loaded:
            raise ValueError("A public code never changes once issued.")
