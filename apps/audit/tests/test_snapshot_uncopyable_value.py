"""snapshot() checks a value before copying it.

Review round 5: copy.deepcopy ran before the JSON check, so a value that
cannot be copied escaped as a raw TypeError, and one whose copy error
carries its state leaked that state. The JSON check must run first and
refuse such a value with a ValueError that names only the field.
"""

# The throwaway models are never displayed, so they need no __str__.
# ruff: noqa: DJ008

from collections.abc import Callable
from typing import Any

import pytest
from django.db import models
from django.test.utils import isolate_apps

from apps.audit.services import snapshot

SECRET = "made-up-secret"


class _Uncopyable:
    """A value whose copy error message includes its state."""

    def __init__(self) -> None:
        self.secret = SECRET

    def __deepcopy__(self, memo: dict[int, Any]) -> "_Uncopyable":
        raise TypeError(f"cannot copy {self.secret}")


def _generator_value() -> Any:
    return {"g": (i for i in [1])}


def _uncopyable_value() -> Any:
    return {"held": _Uncopyable()}


@pytest.mark.parametrize("make_value", [_generator_value, _uncopyable_value])
def test_uncopyable_value_is_refused_naming_only_the_field(
    make_value: Callable[[], Any],
) -> None:
    with isolate_apps("apps.audit"):

        class Configured(models.Model):
            data = models.JSONField(default=dict)

        obj = Configured()
        obj.data = make_value()

        with pytest.raises(ValueError, match=r"data.*cannot be snapshotted") as exc:
            snapshot(obj, ["data"])

    assert SECRET not in str(exc.value)
    assert exc.value.__cause__ is None
    assert exc.value.__suppress_context__
