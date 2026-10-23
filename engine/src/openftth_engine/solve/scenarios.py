"""Design scenarios: the same twin solved under rulebook variants, priced side by side."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from ..rules import Rulebook

Override = Callable[[Rulebook], Any]

#: Scenario name → rulebook overrides (dotted path → value or function of the current rulebook).
SCENARIOS: dict[str, dict[str, Override | Any]] = {
    "baseline": {},
    "larger-fats": {
        "fat.max_homes": lambda r: min(r.pon.split_ratio, int(round(r.fat.max_homes * 1.5))),
    },
    "shorter-drops": {
        "distances.max_drop_m": lambda r: float(round(r.distances.max_drop_m * 0.75)),
    },
    "lean-spares": {
        "fibers.spare_pct": lambda r: max(5.0, r.fibers.spare_pct / 2),
    },
    # Architectures: the same area with one splitter per PON in the FDH, a two-stage cascade
    # (the second stage in the FATs), or a dedicated fibre per unit.
    "centralised": {"pon.architecture": "centralised", "pon.cascade": None},
    "cascaded": {
        "pon.architecture": "cascaded",
        "pon.cascade": lambda r: [4, r.pon.split_ratio // 4],
    },
    "point-to-point": {"pon.architecture": "p2p", "pon.cascade": None},
    # Route types: every street in a trench (or an existing duct), cable on the building fronts
    # where the walls line the street, on existing poles, or whichever of the two is cheaper.
    "trench-only": {"aerial.enabled": False, "facade.enabled": False},
    "facade": {"aerial.enabled": False, "facade.enabled": True},
    "aerial": {"aerial.enabled": True, "facade.enabled": False},
    "aerial-and-facade": {"aerial.enabled": True, "facade.enabled": True},
}

#: Rulebook sections that change the route graph itself: a variant that changes one of them
#: gets its own twin.
TWIN_SECTIONS = ("aerial", "facade", "surfaces", "routing", "crossings", "highway_to_surface")


def resolve_overrides(rules: Rulebook, overrides: dict[str, Override | Any]) -> dict[str, Any]:
    return {path: (v(rules) if callable(v) else v) for path, v in overrides.items()}


def set_path(data: dict[str, Any], path: str, value: Any) -> Any:
    """Set a dotted path in a nested dict, returning the old value. Raises KeyError when the
    path does not exist (no new keys are invented)."""
    parts = path.split(".")
    cur: Any = data
    for p in parts[:-1]:
        if not isinstance(cur, dict) or p not in cur:
            raise KeyError(path)
        cur = cur[p]
    if not isinstance(cur, dict) or parts[-1] not in cur:
        raise KeyError(path)
    old = cur[parts[-1]]
    cur[parts[-1]] = value
    return old


def parse_value(value: Any) -> Any:
    """Values arrive from agents as strings; accept JSON literals and plain numbers."""
    if not isinstance(value, str):
        return value
    text = value.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def apply_overrides(rules: Rulebook, overrides: dict[str, Any]) -> Rulebook:
    data = rules.model_dump(by_alias=True)
    for path, value in overrides.items():
        set_path(data, path, value)
    return Rulebook.model_validate(data)
