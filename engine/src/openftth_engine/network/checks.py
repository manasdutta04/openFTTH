"""Checks a design against an operator pack: ids, required attributes, lifecycle, connectivity."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import asdict, dataclass

from ..interchange import Design
from .operator import LIFECYCLE_STATES, OperatorPack
from .trace import _home_start, build_connectivity


@dataclass
class OperatorViolation:
    rule: str
    element: str | None
    severity: str  # error | warning
    message: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _value(f, key: str):
    if key in f.props and f.props[key] not in (None, ""):
        return f.props[key]
    return f.openftth.get(key)


def check_operator(design: Design, pack: OperatorPack) -> list[OperatorViolation]:
    out: list[OperatorViolation] = []
    for kind, rule in sorted(pack.objects.items()):
        seen: dict[str, list[str]] = defaultdict(list)
        pattern = re.compile(rule.id_pattern) if rule.id_pattern else None
        for f in design.by_kind(kind):
            ident = _value(f, rule.id_field)
            label = f"{rule.label} {ident or f.element_uuid[:8]}"
            if ident in (None, ""):
                out.append(
                    OperatorViolation(
                        "id_missing", f.element_uuid, "error", f"{label} has no {rule.id_field}"
                    )
                )
            else:
                seen[str(ident)].append(f.element_uuid)
                if pattern and not pattern.fullmatch(str(ident)):
                    out.append(
                        OperatorViolation(
                            "id_pattern",
                            f.element_uuid,
                            "error",
                            f"{label}: id does not match {rule.id_pattern}",
                        )
                    )
            for key in rule.required:
                if _value(f, key) in (None, ""):
                    out.append(
                        OperatorViolation(
                            "required", f.element_uuid, "error", f"{label} is missing {key}"
                        )
                    )
        if pack.rules.unique_ids:
            for ident, ids in sorted(seen.items()):
                if len(ids) > 1:
                    out.extend(
                        OperatorViolation(
                            "unique_id", u, "error", f"{len(ids)} {rule.label}s share id {ident}"
                        )
                        for u in ids
                    )
    for f in design:
        state = f.openftth.get("lifecycle")
        if state is not None and state not in LIFECYCLE_STATES:
            out.append(
                OperatorViolation(
                    "lifecycle", f.element_uuid, "error", f"unknown lifecycle state {state!r}"
                )
            )
    if pack.rules.every_home_reaches_olt and design.tables.get("fiber_connection"):
        conn = build_connectivity(design)
        for h in design.by_kind("home"):
            starts = [e for e in _home_start(design, h.element_uuid) if e in conn.up]
            ok = False
            for end in starts[:1]:
                cur, seen = end, set()
                while cur not in seen:
                    seen.add(cur)
                    hops = conn.up.get(cur, [])
                    if not hops:
                        ok = cur[1] == "OLT"
                        break
                    cur = hops[0].end
            if not ok:
                out.append(
                    OperatorViolation(
                        "home_reaches_olt",
                        h.element_uuid,
                        "error",
                        f"{h.props.get('name', 'home')} has no fibre path to an OLT port",
                    )
                )
    if pack.rules.max_splitter_fill_pct < 100:
        for fdh in design.by_kind("fdh"):
            pons = int(fdh.openftth.get("pons", 0)) or 1
            cap = int(fdh.props.get("port_capacity", 0))
            used = int(fdh.openftth.get("units", 0))
            if cap and used / cap * 100 > pack.rules.max_splitter_fill_pct:
                out.append(
                    OperatorViolation(
                        "splitter_fill",
                        fdh.element_uuid,
                        "warning",
                        f"{fdh.props.get('name')} uses {used} of {cap} ports over {pons} PON(s), "
                        f"above {pack.rules.max_splitter_fill_pct:g}%",
                    )
                )
    return out
