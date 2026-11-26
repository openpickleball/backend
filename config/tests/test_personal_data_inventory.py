"""Every model and every first-party field is named in the personal data
inventory (issue #115, ADR 0009).

``docs/privacy/personal-data-inventory.md`` must name each installed
model by its label (`` `app_label.ModelName` ``), in its table or its
"Not personal data" list, and each concrete field of a first-party model
(``apps.*``) other than the automatic ``id``: as `` `field` `` on a line
that names the model, or as `` `app_label.ModelName.field` `` anywhere. A
new model or field then fails here until the inventory says whether it
holds personal data.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from django.apps import apps

BASE_DIR = Path(__file__).resolve().parent.parent.parent
INVENTORY = BASE_DIR / "docs" / "privacy" / "personal-data-inventory.md"


@dataclass(frozen=True)
class ModelEntry:
    label: str
    first_party: bool
    fields: tuple[str, ...]


def _names_model(line: str, label: str) -> bool:
    # `label` or `label.field`, never a longer label that starts with it.
    return re.search(rf"`{re.escape(label)}[`.]", line) is not None


def missing_from_inventory(text: str, models: Iterable[ModelEntry]) -> list[str]:
    """What the inventory ``text`` does not name, as labels and field paths."""
    lines = text.splitlines()
    missing: list[str] = []
    for model in models:
        own_lines = [line for line in lines if _names_model(line, model.label)]
        if not own_lines:
            missing.append(model.label)
            continue
        if not model.first_party:
            continue
        for name in model.fields:
            named = any(f"`{name}`" in line for line in own_lines)
            if not named and f"`{model.label}.{name}`" not in text:
                missing.append(f"{model.label}.{name}")
    return missing


def installed_models() -> list[ModelEntry]:
    entries = []
    for model in apps.get_models():
        meta = model._meta
        entries.append(
            ModelEntry(
                label=meta.label,
                first_party=meta.app_config.name.startswith("apps."),
                fields=tuple(
                    field.name
                    for field in meta.concrete_fields
                    if not (field.primary_key and field.name == "id")
                ),
            )
        )
    return entries


def test_the_inventory_names_every_model_and_first_party_field() -> None:
    assert missing_from_inventory(INVENTORY.read_text(), installed_models()) == []


SAMPLE = """\
| `shop.Order` (`buyer`, `placed_at`) | Orders. |
| `shop.Order.notes` | Notes. |
| `shop.OrderLine.qty` | Quantity. |
- `other.Log`: a third-party table.
"""
ORDER = ModelEntry("shop.Order", True, ("buyer", "placed_at", "notes"))
LINE = ModelEntry("shop.OrderLine", True, ("qty",))
LOG = ModelEntry("other.Log", False, ("anything",))


def test_a_sample_that_names_everything_passes() -> None:
    assert missing_from_inventory(SAMPLE, [ORDER, LINE, LOG]) == []


def test_a_new_first_party_field_fails() -> None:
    order = ModelEntry("shop.Order", True, (*ORDER.fields, "made_up_extra"))

    assert missing_from_inventory(SAMPLE, [order, LINE, LOG]) == [
        "shop.Order.made_up_extra"
    ]


def test_a_removed_model_name_fails() -> None:
    text = SAMPLE.replace("- `other.Log`: a third-party table.\n", "")

    assert missing_from_inventory(text, [ORDER, LINE, LOG]) == ["other.Log"]


def test_a_longer_label_does_not_name_a_shorter_one() -> None:
    text = "| `shop.OrderLine.qty` | Quantity. |\n"

    assert missing_from_inventory(text, [ORDER]) == ["shop.Order"]


def test_a_field_named_on_another_model_line_does_not_count() -> None:
    text = SAMPLE.replace("(`buyer`, `placed_at`)", "(`placed_at`)") + (
        "| `shop.OrderLine` (`buyer`) | Not the order's. |\n"
    )

    assert missing_from_inventory(text, [ORDER]) == ["shop.Order.buyer"]


def test_a_third_party_model_needs_only_its_label() -> None:
    assert missing_from_inventory("- `other.Log`.\n", [LOG]) == []


def test_every_installed_model_is_checked() -> None:
    labels = {entry.label for entry in installed_models()}

    assert {"players.Player", "mfa.Authenticator", "sessions.Session"} <= labels
    player = next(e for e in installed_models() if e.label == "players.Player")
    assert player.first_party
    assert "legal_name" in player.fields
    assert "id" not in player.fields
