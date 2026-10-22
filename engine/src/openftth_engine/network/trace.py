"""Fibre connectivity: the design's connection table as a directed graph.

Every row of `fiber_connection` joins an upstream end to a downstream end. An end is a fibre
in a cable `(cable, tube, fibre)` or a port on an element `(element, side, port)`. A fibre is
the same fibre at both ends of its cable, so splices chain cable to cable. Inside a splitter
the input port feeds every output port of its PON; those links are added here.

The walk upstream from a home's drop fibre ends at an OLT port; the walk downstream from any
cable, closure or cabinet reaches the homes behind it.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any

from ..interchange import Design
from ..rules import Rulebook

End = tuple[str, str, int]  # (element or cable uuid, side or "tube:N", port or fibre)


def _src(row: dict[str, Any]) -> End | None:
    if row.get("source_cable_uuid"):
        return (row["source_cable_uuid"], f"tube:{row['source_tube']}", int(row["source_fiber"]))
    if row.get("source_element_uuid"):
        return (row["source_element_uuid"], str(row.get("source_side")), int(row["source_port"]))
    return None


def _dst(row: dict[str, Any]) -> End | None:
    if row.get("dest_cable_uuid"):
        return (row["dest_cable_uuid"], f"tube:{row['dest_tube']}", int(row["dest_fiber"]))
    if row.get("dest_element_uuid"):
        return (row["dest_element_uuid"], str(row.get("dest_side")), int(row["dest_port"]))
    return None


@dataclass
class Hop:
    end: End
    row: dict[str, Any] | None  # the connection that led here; None for splitter internals


@dataclass
class Connectivity:
    up: dict[End, list[Hop]] = field(default_factory=lambda: defaultdict(list))
    down: dict[End, list[Hop]] = field(default_factory=lambda: defaultdict(list))
    by_element: dict[str, set[End]] = field(default_factory=lambda: defaultdict(set))
    rows_at: dict[str, list[dict[str, Any]]] = field(default_factory=lambda: defaultdict(list))

    def add(self, a: End, b: End, row: dict[str, Any] | None) -> None:
        self.down[a].append(Hop(b, row))
        self.up[b].append(Hop(a, row))
        self.by_element[a[0]].add(a)
        self.by_element[b[0]].add(b)


def build_connectivity(design: Design) -> Connectivity:
    c = Connectivity()
    rows = sorted(design.tables.get("fiber_connection", []), key=lambda r: r["uuid"])
    point_element = {
        p["uuid"]: p.get("element_uuid") for p in design.tables.get("splice_point", [])
    }
    splitter_outputs: dict[tuple[str, str], set[int]] = defaultdict(set)
    for row in rows:
        a, b = _src(row), _dst(row)
        if a is None or b is None:
            continue
        c.add(a, b, row)
        holder = point_element.get(row.get("splice_point_uuid"))
        if holder:
            c.rows_at[holder].append(row)
        if row.get("connection_type") == "splitter":
            splitter_outputs[(a[0], a[1])].add(a[2])
        if a[1] == "ODP":
            # A point-to-point patch point: its "ODP in" port p is its "ODP" port p.
            c.add((a[0], "ODP in", a[2]), a, None)
    # Splitter internals: "<PON> splitter in" port p feeds every output port of that PON.
    for (fdh, pon), ports in sorted(splitter_outputs.items()):
        try:
            p = int(pon.rsplit("-", 1)[1])
        except (IndexError, ValueError):
            continue
        inp: End = (fdh, f"{pon} splitter in", p)
        for port in sorted(ports):
            c.add(inp, (fdh, pon, port), None)
    return c


def _home_start(design: Design, home_uuid: str) -> list[End]:
    """The ends where a home's fibres terminate: its drop fibres, or its indoor FAT ports."""
    home = design.get(home_uuid)
    drop = next((d for d in design.by_kind("drop") if d.openftth.get("home") == home_uuid), None)
    units = int(home.openftth.get("units", 1))
    if drop is not None and not home.openftth.get("mdu"):
        return [(drop.element_uuid, "tube:1", k) for k in range(1, units + 1)]
    fat_ids = home.openftth.get("fat_ids") or [home.openftth.get("fat_id")]
    return [(f, "indoor", k) for f in fat_ids if f for k in range(1, 33)]


def _label(design: Design, uuid: str) -> tuple[str, str]:
    if uuid in design:
        f = design.get(uuid)
        return str(f.props.get("name") or f.props.get("manhole_id") or f.kind), f.kind
    return uuid[:8], "unknown"


def trace_up(design: Design, rules: Rulebook, start: End, conn: Connectivity) -> dict[str, Any]:
    """Walk from a fibre end to the OLT, one hop at a time, with loss per hop."""
    nm = rules.pon.worst_case_nm
    per_km = rules.loss_db.fiber_per_km[nm]
    point_name = {
        sp["uuid"]: _label(design, sp["element_uuid"])[0]
        for sp in design.tables.get("splice_point", [])
        if sp.get("element_uuid")
    }
    steps: list[dict[str, Any]] = []
    total = 0.0
    seen: set[End] = set()
    end = start
    while True:
        if end in seen:
            break
        seen.add(end)
        uuid, side, number = end
        name, kind = _label(design, uuid)
        step: dict[str, Any] = {"element": uuid, "name": name, "kind": kind}
        if side.startswith("tube:"):
            cable = design.get(uuid) if uuid in design else None
            length = float(cable.props.get("length_m", 0.0)) if cable else 0.0
            loss = round(length / 1000.0 * per_km, 3)
            step.update(tube=int(side[5:]), fiber=number, length_m=length, loss_db=loss)
            total += loss
        else:
            step.update(side=side, port=number)
        hops = conn.up.get(end, [])
        if not hops:
            step["end"] = True
            steps.append(step)
            break
        hop = hops[0]
        if hop.row is not None:
            joint = float(hop.row.get("loss_db") or 0.0)
            step["via"] = {
                "connection": hop.row.get("connection_type") or "splice",
                "loss_db": joint,
                "at": point_name.get(hop.row.get("splice_point_uuid")),
            }
            total += joint
        steps.append(step)
        end = hop.end
    steps.reverse()  # OLT first
    first = steps[0] if steps else {}
    reached = first.get("side") == "OLT"
    return {
        "reaches_olt": reached,
        "olt_port": first.get("port") if reached else None,
        "steps": steps,
        "fibre_and_joint_loss_db": round(total, 2),
    }


def trace_home(design: Design, rules: Rulebook, home_uuid: str) -> dict[str, Any]:
    conn = build_connectivity(design)
    starts = [e for e in _home_start(design, home_uuid) if e in conn.up]
    if not starts:
        return {"reaches_olt": False, "steps": [], "reason": "no fibre of this home is connected"}
    out = trace_up(design, rules, starts[0], conn)
    home = design.get(home_uuid)
    out["home"] = {"element": home_uuid, "name": str(home.props.get("name", "home"))}
    out["fibres"] = len(starts)
    out["odn_loss_db"] = home.openftth.get("odn_loss_db")
    return out


def homes_behind(design: Design, element_uuid: str, conn: Connectivity | None = None) -> list[str]:
    """Every home whose fibre passes through `element_uuid` (cable, closure point, FAT, FDH)."""
    conn = conn or build_connectivity(design)
    frontier: deque[End] = deque(sorted(conn.by_element.get(element_uuid, set())))
    # A chamber or closure holds connections rather than ends: start from their downstream ends.
    for row in conn.rows_at.get(element_uuid, []):
        d = _dst(row)
        if d is not None:
            frontier.append(d)
    seen: set[End] = set(frontier)
    reached: set[str] = set()
    while frontier:
        end = frontier.popleft()
        reached.add(end[0])
        for hop in conn.down.get(end, []):
            if hop.end not in seen:
                seen.add(hop.end)
                frontier.append(hop.end)
    homes = []
    drop_home = {d.element_uuid: d.openftth.get("home") for d in design.by_kind("drop")}
    for h in design.by_kind("home"):
        fats = h.openftth.get("fat_ids") or [h.openftth.get("fat_id")]
        drop = next((d for d, hm in drop_home.items() if hm == h.element_uuid), None)
        if drop in reached or (h.openftth.get("mdu") and any(f in reached for f in fats)):
            homes.append(h.element_uuid)
    return sorted(homes)
