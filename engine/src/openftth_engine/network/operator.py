"""Operator packs: an operator's object types, naming rules, lifecycle and network rules.

A pack is a YAML file validated by `OperatorPack`; `operators/schema.json` is generated from
it (`openftth-engine schema --operator`). Packs hold no numbers a solver uses (those stay in
the rulebook); they say how the operator names, records and accepts a network.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

LIFECYCLE_STATES = (
    "planned",
    "in_construction",
    "as_built",
    "in_service",
    "out_of_service",
    "removed",
)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ObjectRule(_Strict):
    label: str = Field(description="What the operator calls this object")
    id_field: str = Field("name", description="Attribute that holds the operator id")
    id_pattern: str | None = Field(None, description="Regular expression the id must match")
    required: list[str] = Field(
        default_factory=list, description="Attributes (props or openftth keys) that must be set"
    )

    @model_validator(mode="after")
    def _pattern_compiles(self) -> ObjectRule:
        if self.id_pattern is not None:
            try:
                re.compile(self.id_pattern)
            except re.error as exc:
                raise ValueError(
                    f"id_pattern {self.id_pattern!r} is not a valid regex: {exc}"
                ) from exc
        return self


class Lifecycle(_Strict):
    initial: str = "planned"
    transitions: dict[str, list[str]] = Field(
        default_factory=lambda: {
            "planned": ["in_construction", "removed"],
            "in_construction": ["as_built", "planned"],
            "as_built": ["in_service", "in_construction"],
            "in_service": ["out_of_service"],
            "out_of_service": ["in_service", "removed"],
            "removed": [],
        },
        description="Allowed next states per state",
    )

    @model_validator(mode="after")
    def _states_known(self) -> Lifecycle:
        known = set(LIFECYCLE_STATES)
        bad = (
            {self.initial}
            | set(self.transitions)
            | {t for ts in self.transitions.values() for t in ts}
        )
        unknown = sorted(bad - known)
        if unknown:
            raise ValueError(
                f"unknown lifecycle states: {unknown}; known: {list(LIFECYCLE_STATES)}"
            )
        return self

    def allows(self, old: str, new: str) -> bool:
        return new in self.transitions.get(old, [])


class NetworkRules(_Strict):
    every_home_reaches_olt: bool = Field(
        True, description="Every home's fibre traces back to an OLT port at the POP"
    )
    unique_ids: bool = Field(True, description="No two objects of one kind share an operator id")
    max_splitter_fill_pct: float = Field(
        100.0, description="Share of a PON's splitter ports that may be used"
    )


class OperatorPack(_Strict):
    format: str = Field("openftth.operator", pattern=r"^openftth\.operator$")
    version: int = 1
    name: str
    description: str = ""
    objects: dict[str, ObjectRule] = Field(
        default_factory=dict, description="Rules per design kind (pop, fdh, fat, feeder, …)"
    )
    lifecycle: Lifecycle = Lifecycle()
    rules: NetworkRules = NetworkRules()


def _operators_dir() -> Path | None:
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "operators"
        if (candidate / "generic.yaml").exists():
            return candidate
    return None


def list_operator_packs() -> list[str]:
    d = _operators_dir()
    return sorted(p.stem for p in d.glob("*.yaml")) if d else []


def load_operator_pack(path_or_name: str | Path) -> OperatorPack:
    """Load a pack from a YAML path, or a shipped one by name such as `generic`."""
    p = Path(path_or_name)
    if not p.exists():
        d = _operators_dir()
        if d is None or not (d / f"{path_or_name}.yaml").exists():
            raise FileNotFoundError(f"no operator pack named {path_or_name!r}")
        p = d / f"{path_or_name}.yaml"
    with p.open("r", encoding="utf-8") as fh:
        return OperatorPack.model_validate(yaml.safe_load(fh) or {})
