"""Single source of truth for EggSort weight classifications."""

from __future__ import annotations

from typing import Final


SIZE_ORDER: Final[tuple[str, ...]] = (
    "Peewee",
    "Small",
    "Medium",
    "Large",
    "Extra Large",
    "Jumbo",
)

SIZE_CODES: Final[dict[str, str]] = {
    "Peewee": "PEEWEE",
    "Small": "SMALL",
    "Medium": "MEDIUM",
    "Large": "LARGE",
    "Extra Large": "EXTRA_LARGE",
    "Jumbo": "JUMBO",
}


def classify_egg_size(weight_grams: int | float) -> str:
    """Classify using non-overlapping, ordered boundaries.

    The supplied ranges overlap at 49 g and 56 g. Ordered ranges assign
    49 g to Small and 56 g to Medium, avoiding duplicate classifications.
    """
    weight = float(weight_grams)
    if weight < 42:
        return "Peewee"
    if weight <= 49:
        return "Small"
    if weight <= 56:
        return "Medium"
    if weight <= 63:
        return "Large"
    if weight <= 70:
        return "Extra Large"
    return "Jumbo"


def servo_command(size: str) -> str:
    try:
        return f"SORT:{SIZE_CODES[size]}"
    except KeyError as exc:
        raise ValueError(f"Unsupported egg size: {size}") from exc
