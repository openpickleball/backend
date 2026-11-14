"""snapshot() copies each value through the JSON encoder, nothing else.

Review round 6: a value the encoder accepts could still break the separate
deepcopy step. A custom __deepcopy__ could raise a raw error that carries
the value's state, or return something other than what was checked, and a
deeply nested value raised a raw RecursionError. A JSON round trip makes
the check and the copy one step: no __deepcopy__ runs, the result holds
only plain JSON types, and every encoder failure is the field-named
ValueError.
"""

# The throwaway models are never displayed, so they need no __str__.
# ruff: noqa: DJ008

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from django.db import models
from django.test.utils import isolate_apps

from apps.audit.services import snapshot

SECRET = "made-up-secret"


class LeakyDict(dict[str, Any]):
    def __deepcopy__(self, memo: dict[int, Any]) -> "LeakyDict":
        raise TypeError(f"leak {dict(self)}")


class LeakyStr(str):
    __slots__ = ()

    def __deepcopy__(self, memo: dict[int, Any]) -> "LeakyStr":
        raise RuntimeError(f"leak {self}")


class LeakyList(list[Any]):
    def __deepcopy__(self, memo: dict[int, Any]) -> "LeakyList":
        raise RuntimeError(f"leak {list(self)}")


class Swap(str):
    __slots__ = ()

    def __deepcopy__(self, memo: dict[int, Any]) -> object:
        return object()


@contextmanager
def _json_model() -> Iterator[type[models.Model]]:
    with isolate_apps("apps.audit"):

        class Configured(models.Model):
            data = models.JSONField(default=dict)

        yield Configured


def _snapshot_data(value: Any) -> dict[str, Any]:
    with _json_model() as model:
        obj = model()
        obj.data = value  # type: ignore[attr-defined]
        return snapshot(obj, ["data"])


def _assert_plain_json(value: Any) -> None:
    """Every container and scalar is a plain JSON type, not a subclass."""
    if isinstance(value, dict):
        assert type(value) is dict
        for key, item in value.items():
            assert type(key) is str
            _assert_plain_json(item)
    elif isinstance(value, list):
        assert type(value) is list
        for item in value:
            _assert_plain_json(item)
    else:
        assert value is None or type(value) in {str, int, float, bool}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ({"k": LeakyDict(pw=SECRET)}, {"k": {"pw": SECRET}}),
        ({"k": LeakyStr(SECRET)}, {"k": SECRET}),
        ({"k": LeakyList([SECRET])}, {"k": [SECRET]}),
    ],
    ids=["dict-subclass", "str-subclass", "list-subclass"],
)
def test_custom_deepcopy_never_runs(value: Any, expected: dict[str, Any]) -> None:
    result = _snapshot_data(value)

    assert result == {"data": expected}
    _assert_plain_json(result)


def test_stored_copy_is_what_was_checked() -> None:
    result = _snapshot_data({"k": Swap("x")})

    assert result == {"data": {"k": "x"}}
    _assert_plain_json(result)


def test_deeply_nested_value_is_refused_naming_only_the_field() -> None:
    deep: list[Any] = []
    for _ in range(100_000):
        deep = [deep]

    with pytest.raises(ValueError, match=r"data.*cannot be snapshotted") as exc:
        _snapshot_data({"k": deep, "pw": SECRET})

    assert SECRET not in str(exc.value)
    assert exc.value.__cause__ is None
    assert exc.value.__suppress_context__
