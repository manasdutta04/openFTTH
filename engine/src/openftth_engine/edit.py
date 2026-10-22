"""The engineer's own edits: add a site, draw a cable, split or join cables, delete, set an
attribute, reassign a home or a FAT, change a splice, and undo or redo any of them.

Every edit is a tool call with a reason on record, snapped to the street twin, and one version;
nothing here invents a number the engineer did not give or the engine did not compute. Sites
the engineer places are pinned, so a later solver run keeps them; cables and chambers the
engineer draws are kept by the routing solvers too. Each result carries the rule check of the
design as it now stands, so the app can mark what the edit broke.
"""

from __future__ import annotations

import json
from typing import Any

import networkx as nx

from . import pipeline as _pipeline
from .interchange import Feature
from .pipeline import (
    Session,
    _commit,
    _merge,
    _rejected,
    _rerun,
    _to_metric,
    _twin,
    _wgs,
    assign_fibers,
    assign_pon,
    place_chambers,
    route_distribution,
    route_drops,
    route_feeder,
    size_cables,
)
from .results import Decision, ToolResult
from .solve.fibers import FIBERS_PER_TUBE, tube_fiber
from .validate import run_all_checks

#: Tools whose versions undo and redo step through: they only change the design file.
UNDOABLE = (
    "add_site",
    "delete_element",
    "delete_elements",
    "draw_cable",
    "split_cable",
    "join_cables",
    "edit_splice",
    "set_attribute",
    "reassign",
    "move_site",
    "complete_network",
)

SITE_KINDS = ("fat", "fdh", "chamber", "pop")
CABLE_KINDS = ("feeder", "distribution", "drop")
DELETABLE = ("fat", "fdh", "chamber", "pop", "feeder", "distribution", "drop", "crossing")

#: What set_attribute may change, per kind, and how the value is checked.
EDITABLE: dict[str, tuple[str, ...]] = {
    "*": ("name", "status", "notes"),
    "fat": ("port_capacity",),
    "fdh": ("port_capacity",),
    "chamber": ("manhole_type",),
    "feeder": ("fiber_count", "cable_count", "slack_m"),
    "distribution": ("fiber_count", "cable_count", "slack_m"),
    "drop": ("fiber_count",),
}
FIELD_WORDS = {
    "port_capacity": "ports",
    "fiber_count": "fibres",
    "cable_count": "cables",
    "slack_m": "slack (m)",
    "manhole_type": "chamber type",
}
STATUSES = ("planned", "approved", "in_construction", "as_built", "in_service", "retired")


def _label(f: Feature) -> str:
    """What the engineer calls an element: its name, a chamber's id, or a drop's two ends."""
    for key in ("name", "manhole_id", "site_name", "pole_id"):
        if f.props.get(key):
            return str(f.props[key])
    if f.kind == "drop" and f.props.get("from_label") and f.props.get("to_label"):
        return f"drop {f.props['from_label']} → {f.props['to_label']}"
    return f"{f.kind} {f.element_uuid[:8]}"


def _sync_connections(s: Session) -> None:
    """A FAT's or a cabinet's connections are the units on it; the reserve is what is left."""
    for f in s.design.by_kind("fat") + s.design.by_kind("fdh"):
        units = int(f.openftth.get("units", 0))
        f.props["total_connections"] = units
        f.props["reserve_capacity"] = int(f.props.get("port_capacity", 0) or 0) - units


def _reason(tool: str, reason: str) -> str | None:
    return (reason or "").strip() or None


def _checked(s: Session, result: ToolResult) -> ToolResult:
    """Attach the rule check of the design as it now stands (not committed as its own step)."""
    violations, stats = run_all_checks(s.design, s.rules)
    by_rule: dict[str, int] = {}
    for v in violations:
        by_rule[v.check] = by_rule.get(v.check, 0) + 1
    result.data["violations"] = by_rule
    result.data["violating"] = sorted({v.element for v in violations if v.element})[:200]
    if violations:
        result.warnings.append(
            f"{len(violations)} rule violation(s) now: "
            + ", ".join(f"{k} {n}" for k, n in sorted(by_rule.items()))
        )
    return result


def _record_edit(s: Session, result: ToolResult, started: tuple[str | None, Any]) -> ToolResult:
    """Commit an edit and push it on the undo stack."""
    before_version, before_design = started
    return _push(s, _commit(s, result, before_design), before_version)


def _push(s: Session, out: ToolResult, before_version: str | None) -> ToolResult:
    """Put a committed edit on the undo stack; a new edit clears what could be redone."""
    meta = s.project.meta
    if out.version and out.version != before_version:
        stack = list(meta.get("edit_stack") or [])
        stack.append(
            {
                "before": before_version,
                "after": out.version,
                "tool": out.tool,
                "summary": out.summary,
            }
        )
        meta["edit_stack"] = stack[-100:]
        meta["redo_stack"] = []
        s.project._write_meta()
    return out


def _begin(s: Session) -> tuple[str | None, Any]:
    """The version and the design before an edit, for its diff and its undo."""
    s.project.set_stage("solve")
    return s.project.head(), s.design.copy()


def _snap(s: Session, lon: float, lat: float) -> tuple[int, float]:
    twin = _twin(s)
    p = _to_metric(twin, lon, lat)
    node, d = twin.nearest_node(p.x, p.y)
    return int(node), float(d)


def _next_name(s: Session, kind: str, prefix: str, width: int) -> str:
    used = {str(f.props.get("name", "")) for f in s.design.by_kind(kind)}
    n = 1
    while f"{prefix}{n:0{width}d}" in used:
        n += 1
    return f"{prefix}{n:0{width}d}"


# ---- sites ------------------------------------------------------------------------------------


def add_site(s: Session, kind: str, lon: float, lat: float, reason: str) -> ToolResult:
    """Place a FAT, an FDH cabinet, a chamber or a POP at the street node nearest the point.
    The site is pinned: a later clustering or routing run keeps it where the engineer put it."""
    why = _reason("add_site", reason)
    if not why:
        return _rejected("add_site", "a reason is required; every manual edit records why")
    if kind not in SITE_KINDS:
        return _rejected("add_site", f"a site is one of {', '.join(SITE_KINDS)}, not {kind}")
    started = _begin(s)
    twin = _twin(s)
    node, snap = _snap(s, lon, lat)
    if any(int(f.openftth.get("node", -1)) == node for f in s.design.by_kind(kind)):
        return _rejected("add_site", f"a {kind} already stands on that street node")
    r = s.rules
    point = _wgs(twin, twin.node_point(node))
    pin = {"placed_by": "engineer", "placed_reason": why, "node": node}
    if kind == "fat":
        name = _next_name(s, "fat", "FAT-", 3)
        f = Feature(
            element_uuid=s.uid("tb", f"fat:manual:{node}"),
            element_type="tb",
            placement="outdoor",
            geometry=point,
            props={
                "name": name,
                "port_capacity": r.fat.max_homes,
                "total_connections": 0,
                "reserve_capacity": r.fat.max_homes,
                "status": "planned",
            },
            openftth={"kind": "fat", "units": 0, **pin},
        )
    elif kind == "fdh":
        cap = max(r.fdh.capacities)
        name = _next_name(s, "fdh", "FDH-", 2)
        f = Feature(
            element_uuid=s.uid("otb", f"fdh:manual:{node}"),
            element_type="otb",
            placement="outdoor",
            geometry=point,
            props={
                "name": name,
                "port_capacity": cap,
                "total_connections": 0,
                "reserve_capacity": cap,
                "status": "planned",
            },
            openftth={"kind": "fdh", "units": 0, "capacity": cap, "fats": 0, **pin},
        )
    elif kind == "chamber":
        f = Feature(
            element_uuid=s.uid("manhole", f"chamber:manual:{node}"),
            element_type="manhole",
            geometry=point,
            props={
                "manhole_id": f"CH-{node}",
                "manhole_type": "small",
                "dimensions_cm": r.chambers.types["small"],
                "status": "planned",
            },
            openftth={"kind": "chamber", **pin},
        )
        name = f"CH-{node}"
    else:
        # A lone POP is "POP", as the solver names it; more are numbered.
        name = _next_name(s, "pop", "POP-", 1) if s.design.by_kind("pop") else "POP"
        f = Feature(
            element_uuid=s.uid("odf", f"pop:manual:{node}"),
            element_type="odf",
            placement="indoor",
            geometry=point,
            props={"name": name, "site_name": name},
            openftth={"kind": "pop", **pin},
        )
    s.design.add(f)
    result = ToolResult(
        tool="add_site",
        summary=f"{name} added at street node {node}",
        decisions=[
            Decision(
                stage="solve",
                tool="add_site",
                element=f.element_uuid,
                reason=f"{name} placed by the engineer: {why}. Snapped {snap:.0f} m to the nearest street node; pinned, so solver runs keep it.",
                values={"node": node, "snap_m": round(snap, 1)},
                source="user",
            )
        ],
        data={"element": f.element_uuid, "kind": kind, "node": node},
    )
    if kind in ("fat", "fdh"):
        result.warnings.append(
            f"{name} serves nothing yet: reassign homes or FATs to it, or run clustering again (it is pinned)"
        )
    return _record_edit(s, _checked(s, result), started)


def _delete_refusal(s: Session, element_uuid: str, going: set[str]) -> str | None:
    """Why an element cannot be deleted, given everything else deleted with it."""
    if element_uuid not in s.design:
        return f"no element {element_uuid} in the design"
    f = s.design.get(element_uuid)
    kind = f.kind or ""
    if kind not in DELETABLE:
        return f"a {kind} cannot be deleted by hand"
    name = _label(f)
    if kind == "fat":
        served = [
            h
            for h in s.design.by_kind("home")
            if h.openftth.get("fat_id") == element_uuid and h.element_uuid not in going
        ]
        if served:
            return f"{name} still serves {len(served)} home(s); reassign them first"
    if kind == "fdh":
        served = [
            x
            for x in s.design.by_kind("fat")
            if x.openftth.get("fdh_id") == element_uuid and x.element_uuid not in going
        ]
        if served:
            return f"{name} still feeds {len(served)} FAT(s); reassign them first"
    if kind == "pop":
        left = [x for x in s.design.by_kind("pop") if x.element_uuid not in going]
        if not left:
            return "the design needs a POP; move it instead"
    return None


def _cut_connections(s: Session, going: set[str]) -> int:
    """Remove the fibre connections that ran through deleted elements; returns how many."""
    conns = s.design.tables.get("fiber_connection") or []
    keys = (
        "source_element_uuid",
        "dest_element_uuid",
        "source_cable_uuid",
        "dest_cable_uuid",
    )
    keep = [c for c in conns if not any(c.get(k) in going for k in keys)]
    cut = len(conns) - len(keep)
    if cut:
        s.design.set_table("fiber_connection", keep)
        _recount_points(s)
    return cut


def delete_element(s: Session, element_uuid: str, reason: str) -> ToolResult:
    """Take a site or a cable out of the design. A FAT or a cabinet that still serves something
    is refused: reassign what it serves first, so nothing is left without a connection."""
    why = _reason("delete_element", reason)
    if not why:
        return _rejected("delete_element", "a reason is required; every manual edit records why")
    refusal = _delete_refusal(s, element_uuid, {element_uuid})
    if refusal:
        return _rejected("delete_element", refusal)
    f = s.design.get(element_uuid)
    kind = f.kind or ""
    name = _label(f)
    started = _begin(s)
    s.design.remove(element_uuid)
    cut = _cut_connections(s, {element_uuid})
    result = ToolResult(
        tool="delete_element",
        summary=f"{name} deleted"
        + (f"; {cut} fibre connection(s) through it removed" if cut else ""),
        decisions=[
            Decision(
                stage="solve",
                tool="delete_element",
                element=element_uuid,
                reason=f"{name} deleted by the engineer: {why}.",
                values={"kind": kind, "connections_removed": cut},
                source="user",
            )
        ],
        data={"element": element_uuid, "kind": kind},
    )
    if kind in CABLE_KINDS and f.openftth.get("drawn_by") != "engineer":
        result.warnings.append(
            f"{name} was routed by the engine: routing it again brings it back; forbid the street or move a site to change the route for good"
        )
    return _record_edit(s, _checked(s, result), started)


def delete_elements(s: Session, element_uuids: list[str], reason: str) -> ToolResult:
    """Delete several sites and cables as one edit (one undo step). Homes, buildings and other
    things that are never deleted by hand are skipped; anything deletable that cannot go (a FAT
    still serving a home outside the set) refuses the whole edit."""
    why = _reason("delete_elements", reason)
    if not why:
        return _rejected("delete_elements", "a reason is required; every manual edit records why")
    ids = list(dict.fromkeys(element_uuids or []))
    present = [u for u in ids if u in s.design]
    doomed = [u for u in present if (s.design.get(u).kind or "") in DELETABLE]
    skipped = len(ids) - len(doomed)
    if not doomed:
        return _rejected("delete_elements", "nothing in the selection can be deleted by hand")
    going = set(doomed)
    for u in doomed:
        refusal = _delete_refusal(s, u, going)
        if refusal:
            return _rejected("delete_elements", refusal)
    started = _begin(s)
    names = [_label(s.design.get(u)) for u in doomed]
    for u in doomed:
        s.design.remove(u)
    cut = _cut_connections(s, going)
    shown = ", ".join(names[:4]) + (f" and {len(names) - 4} more" if len(names) > 4 else "")
    result = ToolResult(
        tool="delete_elements",
        summary=f"{len(doomed)} element(s) deleted: {shown}"
        + (f"; {cut} fibre connection(s) removed" if cut else ""),
        decisions=[
            Decision(
                stage="solve",
                tool="delete_elements",
                element=u,
                reason=f"{name} deleted by the engineer with {len(doomed) - 1} other element(s): {why}.",
                values={"connections_removed": cut},
                source="user",
            )
            for u, name in zip(doomed, names, strict=True)
        ],
        data={"elements": doomed},
    )
    if skipped:
        result.warnings.append(
            f"{skipped} selected element(s) left as they are (homes, buildings and streets are not deleted by hand)"
        )
    return _record_edit(s, _checked(s, result), started)


# ---- cables -----------------------------------------------------------------------------------


def _cable_feature(
    s: Session,
    uuid: str,
    kind: str,
    nodes: list[int],
    name: str,
    fiber_count: int,
    extra: dict[str, Any],
) -> Feature:
    twin = _twin(s)
    G = twin.graph
    edges = {
        f"{min(u, v)}-{max(u, v)}": (
            f"crossing:{G[u][v]['crossing']}" if G[u][v].get("crossing") else G[u][v]["surface"],
            round(G[u][v]["length_m"], 2),
        )
        for u, v in zip(nodes[:-1], nodes[1:], strict=False)
    }
    length = sum(G[u][v]["length_m"] for u, v in zip(nodes[:-1], nodes[1:], strict=False))
    surfaces: dict[str, float] = {}
    for u, v in zip(nodes[:-1], nodes[1:], strict=False):
        surfaces[G[u][v]["surface"]] = surfaces.get(G[u][v]["surface"], 0.0) + G[u][v]["length_m"]
    slack = 0.0 if kind == "drop" else s.rules.slack.per_closure_m
    return Feature(
        element_uuid=uuid,
        element_type="cable.underground",
        geometry=_wgs(twin, twin.path_line(nodes)),
        props={
            "cable_type": kind,
            "fiber_count": fiber_count,
            "cable_count": 1,
            "from_label": f"N{nodes[0]}",
            "to_label": f"N{nodes[-1]}",
            "length_m": round(length, 1),
            "slack_m": slack,
            "total_length_m": round(length + slack, 1),
            "status": "planned",
            "name": name,
        },
        openftth={
            "kind": kind,
            "edges": edges,
            "surfaces": {k: round(v, 1) for k, v in surfaces.items()},
            "trench_cost": round(
                sum(G[u][v]["cost"] for u, v in zip(nodes[:-1], nodes[1:], strict=False)), 2
            ),
            **extra,
        },
    )


def _cable_nodes(s: Session, f: Feature) -> list[int]:
    """A cable's street nodes in order, from its line: vertices snap to their nodes, repeats
    from curved streets drop out, and gaps are filled along the street graph."""
    twin = _twin(s)
    G = twin.graph
    snapped: list[int] = []
    for lon_, lat_ in f.geometry.coords:
        p = _to_metric(twin, lon_, lat_)
        n = int(twin.nearest_node(p.x, p.y)[0])
        if not snapped or snapped[-1] != n:
            snapped.append(n)
    if len(snapped) < 2:
        return snapped
    path = [snapped[0]]
    for a, b in zip(snapped[:-1], snapped[1:], strict=False):
        if G.has_edge(a, b):
            path.append(b)
            continue
        try:
            path.extend(nx.shortest_path(G, a, b, weight="cost")[1:])
        except nx.NetworkXNoPath:
            return []
    return path


def _point_nodes(s: Session) -> dict[str, int | None]:
    """The street node of every splice point."""
    out: dict[str, int | None] = {}
    for pt in s.design.tables.get("splice_point") or []:
        try:
            node = json.loads(pt.get("config_json") or "{}").get("node")
        except (TypeError, ValueError):
            node = None
        out[str(pt["uuid"])] = int(node) if node is not None else None
    return out


def _splice_point(s: Session, node: int, element_uuid: str) -> str:
    """The engineer's splice point at a street node (made when there is none)."""
    pid = s.uid("splice_point", f"manual:{node}")
    points = list(s.design.tables.get("splice_point") or [])
    if not any(pt["uuid"] == pid for pt in points):
        points.append(
            {
                "uuid": pid,
                "element_uuid": element_uuid,
                "port_count": 0,
                "used_ports": 0,
                "config_json": json.dumps({"tree": "manual", "node": node}, sort_keys=True),
                "notes": "",
            }
        )
        s.design.set_table("splice_point", points)
    return pid


def _recount_points(s: Session) -> None:
    """Splice point port counts follow their connections; a point left with none goes."""
    if "splice_point" not in s.design.tables:
        return
    counts: dict[str, int] = {}
    for c in s.design.tables.get("fiber_connection") or []:
        pid = str(c.get("splice_point_uuid") or "")
        counts[pid] = counts.get(pid, 0) + 1
    points = []
    for pt in s.design.tables["splice_point"]:
        n = counts.get(str(pt["uuid"]), 0)
        if n:
            points.append({**pt, "port_count": n, "used_ports": n})
    s.design.set_table("splice_point", points)


def _splice_row(s: Session, key: str, pid: str, **fields: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "uuid": s.uid("fiber_connection", key),
        "splice_point_uuid": pid,
        "source_element_uuid": None,
        "source_cable_uuid": None,
        "source_tube": None,
        "source_fiber": None,
        "source_port": None,
        "source_side": None,
        "dest_element_uuid": None,
        "dest_cable_uuid": None,
        "dest_tube": None,
        "dest_fiber": None,
        "dest_port": None,
        "dest_side": None,
        "connection_type": "splice",
        "loss_db": s.rules.loss_db.splice,
        "tray_ref": None,
        "notes": None,
    }
    row.update(fields)
    return row


def draw_cable(
    s: Session,
    kind: str,
    points: list[list[float]],
    reason: str,
    fiber_count: int | None = None,
) -> ToolResult:
    """Draw a feeder, distribution or drop cable through the given [lon, lat] points. Each point
    snaps to its street node and the cable follows the cheapest street path between them; it is
    kept by later routing runs."""
    why = _reason("draw_cable", reason)
    if not why:
        return _rejected("draw_cable", "a reason is required; every manual edit records why")
    if kind not in CABLE_KINDS:
        return _rejected("draw_cable", f"a cable is one of {', '.join(CABLE_KINDS)}, not {kind}")
    if not points or len(points) < 2:
        return _rejected("draw_cable", "a cable needs at least two points")
    r = s.rules
    ladder = {"feeder": r.fibers.feeder, "distribution": r.fibers.distribution}.get(kind)
    if fiber_count is not None and ladder and fiber_count not in ladder:
        return _rejected(
            "draw_cable",
            f"{fiber_count} fibres is not on the {kind} ladder ({', '.join(map(str, ladder))})",
        )
    started = _begin(s)
    twin = _twin(s)
    snapped = [_snap(s, float(lon), float(lat)) for lon, lat in points]
    stops = [n for n, _ in snapped]
    nodes: list[int] = [stops[0]]
    for a, b in zip(stops[:-1], stops[1:], strict=False):
        if a == b:
            continue
        try:
            leg = nx.shortest_path(twin.graph, a, b, weight="cost")
        except nx.NetworkXNoPath:
            return _rejected("draw_cable", f"no street path between street nodes {a} and {b}")
        nodes.extend(leg[1:])
    if len(nodes) < 2:
        return _rejected("draw_cable", "the points all snap to the same street node")
    fibers = fiber_count or (min(ladder) if ladder else r.fibers.drop)
    prefix = {"feeder": "FDR-M", "distribution": "DST-M", "drop": "DRP-M"}[kind]
    name = _next_name(s, kind, prefix, 3)
    uuid = s.uid("cable.underground", f"{kind}:manual:{'-'.join(map(str, nodes))}")
    f = _cable_feature(
        s,
        uuid,
        kind,
        nodes,
        name,
        fibers,
        {"drawn_by": "engineer", "placed_reason": why, "downstream_units": 0},
    )
    # The routers follow these edges; they outlive this cable when it becomes tree sections.
    f.openftth["drawn_edges"] = sorted(f.openftth["edges"])
    s.design.add(f)
    worst = max(d for _, d in snapped)
    result = ToolResult(
        tool="draw_cable",
        summary=f"{name}: {kind} cable, {fibers}F, {f.props['length_m']:.0f} m along the streets",
        decisions=[
            Decision(
                stage="solve",
                tool="draw_cable",
                element=uuid,
                reason=f"{name} drawn by the engineer: {why}. It follows the cheapest street path through {len(points)} point(s), each snapped to its street node (at most {worst:.0f} m).",
                values={
                    "length_m": f.props["length_m"],
                    "fiber_count": fibers,
                    "nodes": len(nodes),
                },
                source="user",
            )
        ],
        data={"element": uuid, "kind": kind, "length_m": f.props["length_m"]},
    )
    return _record_edit(s, _checked(s, result), started)


def split_cable(s: Session, element_uuid: str, lon: float, lat: float, reason: str) -> ToolResult:
    """Split a cable in two at the street node nearest the point, with a chamber there when none
    stands yet (a splice needs one)."""
    why = _reason("split_cable", reason)
    if not why:
        return _rejected("split_cable", "a reason is required; every manual edit records why")
    if element_uuid not in s.design:
        return _rejected("split_cable", f"no element {element_uuid} in the design")
    f = s.design.get(element_uuid)
    kind = f.kind or ""
    if kind not in CABLE_KINDS:
        return _rejected("split_cable", f"only a cable can be split, not a {kind}")
    twin = _twin(s)
    nodes = _cable_nodes(s, f)
    if len(nodes) < 2:
        return _rejected("split_cable", "the cable's line does not follow the street graph")
    node, _d = _snap(s, lon, lat)
    if node not in nodes[1:-1]:
        # The nearest node on the cable itself, by straight distance from the point.
        target = _to_metric(twin, lon, lat)
        inner = nodes[1:-1]
        if not inner:
            return _rejected("split_cable", "the cable has no inner street node to split at")
        node = min(inner, key=lambda n: twin.node_point(n).distance(target))
    i = nodes.index(node)
    if i == 0 or i == len(nodes) - 1:
        return _rejected("split_cable", "split at an inner point of the cable, not at its end")
    started = _begin(s)
    name = _label(f)
    extra = {
        k: v for k, v in f.openftth.items() if k not in ("edges", "surfaces", "trench_cost", "kind")
    }
    extra["drawn_by"] = "engineer"
    first = _cable_feature(
        s,
        element_uuid,
        kind,
        nodes[: i + 1],
        f"{name}-A",
        int(f.props.get("fiber_count", 1)),
        extra,
    )
    second_id = s.uid("cable.underground", f"split:{element_uuid}:{node}")
    second = _cable_feature(
        s, second_id, kind, nodes[i:], f"{name}-B", int(f.props.get("fiber_count", 1)), extra
    )
    drawn = set(f.openftth.get("drawn_edges") or f.openftth.get("edges", {}))
    for part in (first, second):
        part.props["cable_count"] = f.props.get("cable_count", 1)
        part.openftth["drawn_edges"] = sorted(drawn & set(part.openftth["edges"]))
    s.design.add(first)
    s.design.add(second)
    chamber_note = ""
    if not any(int(c.openftth.get("node", -1)) == node for c in s.design.by_kind("chamber")):
        r = s.rules
        s.design.add(
            Feature(
                element_uuid=s.uid("manhole", f"chamber:manual:{node}"),
                element_type="manhole",
                geometry=_wgs(twin, twin.node_point(node)),
                props={
                    "manhole_id": f"CH-{node}",
                    "manhole_type": "small",
                    "dimensions_cm": r.chambers.types["small"],
                    "status": "planned",
                },
                openftth={
                    "kind": "chamber",
                    "node": node,
                    "placed_by": "engineer",
                    "placed_reason": why,
                },
            )
        )
        chamber_note = f"; chamber CH-{node} added for the splice"
    spliced = _splice_through(s, element_uuid, second_id, nodes, i, node)
    splice_note = f"; {spliced} fibre(s) spliced straight through" if spliced else ""
    result = ToolResult(
        tool="split_cable",
        summary=f"{name} split at street node {node} into {name}-A and {name}-B{chamber_note}{splice_note}",
        decisions=[
            Decision(
                stage="solve",
                tool="split_cable",
                element=element_uuid,
                reason=f"{name} split by the engineer: {why}.",
                values={"node": node, "parts": [element_uuid, second_id]},
                source="user",
            )
        ],
        data={"element": element_uuid, "parts": [element_uuid, second_id], "node": node},
    )
    return _record_edit(s, _checked(s, result), started)


def _splice_through(s: Session, a: str, b: str, nodes: list[int], i: int, node: int) -> int:
    """After a split at nodes[i]: connections on the far part now name the second cable, and every
    fibre the cable carried is spliced straight through at the new chamber, in the direction
    light ran. Returns how many fibres were spliced."""
    conns = s.design.tables.get("fiber_connection")
    if not conns:
        return 0
    pnode = _point_nodes(s)
    pos: dict[int, int] = {}
    for k, n in enumerate(nodes):
        pos.setdefault(n, k)
    flow: dict[tuple[int, int], str] = {}
    rows = []
    for c in conns:
        c = dict(c)
        at = pnode.get(str(c.get("splice_point_uuid") or ""))
        far = at is not None and pos.get(at, -1) > i
        for side in ("source", "dest"):
            if c.get(f"{side}_cable_uuid") != a:
                continue
            key = (int(c[f"{side}_tube"]), int(c[f"{side}_fiber"]))
            if far:
                c[f"{side}_cable_uuid"] = b
            if side == "dest":
                # Light enters the cable where it is the destination of a connection.
                flow[key] = "ba" if far else "ab"
            else:
                flow.setdefault(key, "ab" if far else "ba")
        rows.append(c)
    if flow:
        chamber = next(
            c.element_uuid
            for c in s.design.by_kind("chamber")
            if int(c.openftth.get("node", -1)) == node
        )
        pid = _splice_point(s, node, chamber)
        for (t, fb), way in sorted(flow.items()):
            up, down = (a, b) if way == "ab" else (b, a)
            rows.append(
                _splice_row(
                    s,
                    f"split:{a}:{node}:{t}:{fb}",
                    pid,
                    source_cable_uuid=up,
                    source_tube=t,
                    source_fiber=fb,
                    dest_cable_uuid=down,
                    dest_tube=t,
                    dest_fiber=fb,
                )
            )
    s.design.set_table("fiber_connection", rows)
    _recount_points(s)
    return len(flow)


def join_cables(s: Session, first_uuid: str, second_uuid: str, reason: str) -> ToolResult:
    """Join two cables of the same kind and size that end at the same street node into one (the
    first keeps its id and name). The straight splices at the joint go; a joint that crosses
    fibres is refused, so no fibre is renumbered silently."""
    why = _reason("join_cables", reason)
    if not why:
        return _rejected("join_cables", "a reason is required; every manual edit records why")
    if first_uuid == second_uuid:
        return _rejected("join_cables", "pick two different cables")
    for u in (first_uuid, second_uuid):
        if u not in s.design:
            return _rejected("join_cables", f"no element {u} in the design")
    a, b = s.design.get(first_uuid), s.design.get(second_uuid)
    kind = a.kind or ""
    if kind not in CABLE_KINDS or (b.kind or "") not in CABLE_KINDS:
        return _rejected("join_cables", "only cables can be joined")
    an, bn = _label(a), _label(b)
    if b.kind != kind:
        return _rejected("join_cables", f"{an} is a {kind} cable and {bn} a {b.kind} cable")
    for key in ("fiber_count", "cable_count"):
        if int(a.props.get(key, 1)) != int(b.props.get(key, 1)):
            return _rejected(
                "join_cables",
                f"{an} and {bn} differ in {FIELD_WORDS[key]} ({a.props.get(key)} and {b.props.get(key)}); set them alike first",
            )
    na, nb = _cable_nodes(s, a), _cable_nodes(s, b)
    if len(na) < 2 or len(nb) < 2:
        return _rejected("join_cables", "a cable's line does not follow the street graph")
    if na[-1] == nb[0]:
        path = na + nb[1:]
    elif na[-1] == nb[-1]:
        path = na + nb[::-1][1:]
    elif na[0] == nb[-1]:
        path = nb + na[1:]
    elif na[0] == nb[0]:
        path = na[::-1] + nb[1:]
    else:
        return _rejected(
            "join_cables",
            f"{an} and {bn} do not meet: join cables that end at the same street node",
        )
    joint = na[-1] if na[-1] in (nb[0], nb[-1]) else na[0]
    if len(set(path)) < len(path):
        return _rejected("join_cables", "joined, the cable would run along the same street twice")
    pnode = _point_nodes(s)
    conns = s.design.tables.get("fiber_connection") or []
    pair = {first_uuid, second_uuid}
    rows, straight = [], 0
    for c in conns:
        at = pnode.get(str(c.get("splice_point_uuid") or ""))
        ends = {c.get("source_cable_uuid"), c.get("dest_cable_uuid")}
        if at == joint and ends == pair:
            if (c["source_tube"], c["source_fiber"]) != (c["dest_tube"], c["dest_fiber"]):
                src = _label(s.design.get(c["source_cable_uuid"]))
                dst = _label(s.design.get(c["dest_cable_uuid"]))
                return _rejected(
                    "join_cables",
                    f"at the joint {src} T{c['source_tube']}:{c['source_fiber']} is spliced to {dst} T{c['dest_tube']}:{c['dest_fiber']}; re-patch it straight first",
                )
            straight += 1
            continue
        c = dict(c)
        for side in ("source", "dest"):
            if c.get(f"{side}_cable_uuid") == second_uuid:
                c[f"{side}_cable_uuid"] = first_uuid
        rows.append(c)
    started = _begin(s)
    extra = {
        k: v for k, v in a.openftth.items() if k not in ("edges", "surfaces", "trench_cost", "kind")
    }
    extra["drawn_by"] = "engineer"
    extra["placed_reason"] = why
    # The two halves of a split come back under the name they had before it.
    halves = {an[-2:], bn[-2:]} == {"-A", "-B"} and an[:-2] == bn[:-2]
    name = an[:-2] if halves else an
    joined = _cable_feature(
        s, first_uuid, kind, path, name, int(a.props.get("fiber_count", 1)), extra
    )
    for key in ("cable_count", "status", "notes"):
        if key in a.props:
            joined.props[key] = a.props[key]
    joined.openftth["drawn_edges"] = sorted(joined.openftth["edges"])
    s.design.remove(second_uuid)
    s.design.add(joined)
    if conns:
        s.design.set_table("fiber_connection", rows)
        _recount_points(s)
    pnode = _point_nodes(s)
    left = [
        c
        for c in s.design.by_kind("chamber")
        if int(c.openftth.get("node", -1)) == joint and not any(n == joint for n in pnode.values())
    ]
    result = ToolResult(
        tool="join_cables",
        summary=f"{an} and {bn} joined at street node {joint} into {name}, {joined.props['length_m']:.0f} m"
        + (f"; {straight} straight splice(s) at the joint removed" if straight else ""),
        decisions=[
            Decision(
                stage="solve",
                tool="join_cables",
                element=first_uuid,
                reason=f"{an} and {bn} joined by the engineer: {why}.",
                values={
                    "joint": joint,
                    "removed": second_uuid,
                    "length_m": joined.props["length_m"],
                    "splices_removed": straight,
                },
                source="user",
            )
        ],
        data={"element": first_uuid, "kind": kind, "removed": second_uuid},
    )
    for c in left:
        result.warnings.append(
            f"{_label(c)} at the joint now holds no splice; delete it if it is not needed"
        )
    return _record_edit(s, _checked(s, result), started)


# ---- splices ----------------------------------------------------------------------------------

SPLICE_TYPES = ("splice", "patch")


def _end_text(s: Session, c: dict[str, Any], side: str) -> str:
    cable = c.get(f"{side}_cable_uuid")
    if cable:
        name = _label(s.design.get(cable)) if cable in s.design else cable[:8]
        return f"{name} T{c[f'{side}_tube']}:{c[f'{side}_fiber']}"
    el = c.get(f"{side}_element_uuid") or ""
    name = _label(s.design.get(el)) if el in s.design else el[:8]
    return f"{name} port {c.get(f'{side}_port')}"


def edit_splice(
    s: Session,
    connection_uuid: str,
    reason: str,
    remove: bool = False,
    connection_type: str | None = None,
    tray_ref: str | None = None,
    notes: str | None = None,
    source_fiber: int | None = None,
    dest_fiber: int | None = None,
) -> ToolResult:
    """Change one row of the splice schedule: re-patch either end to another fibre of the same
    cable (a fibre number counted from 1 across tubes), make it a splice or a patch (its loss
    follows from the rulebook), set the tray or a note, or remove it."""
    why = _reason("edit_splice", reason)
    if not why:
        return _rejected("edit_splice", "a reason is required; every manual edit records why")
    conns = s.design.tables.get("fiber_connection") or []
    row = next((c for c in conns if c.get("uuid") == connection_uuid), None)
    if row is None:
        return _rejected("edit_splice", f"no fibre connection {connection_uuid}")
    before_text = f"{_end_text(s, row, 'source')} → {_end_text(s, row, 'dest')}"
    pid = str(row.get("splice_point_uuid") or "")
    point = next(
        (pt for pt in s.design.tables.get("splice_point") or [] if pt["uuid"] == pid), None
    )
    closure = point.get("element_uuid") if point else None
    where = _label(s.design.get(closure)) if closure and closure in s.design else "the closure"
    new = dict(row)
    changes: list[str] = []
    values: dict[str, Any] = {}
    if remove:
        changes.append("removed")
    else:
        if connection_type is not None and connection_type != row.get("connection_type"):
            if row.get("connection_type") == "splitter":
                return _rejected(
                    "edit_splice", "a splitter's own connections are not edited by hand"
                )
            if connection_type not in SPLICE_TYPES:
                return _rejected("edit_splice", f"a connection is one of {', '.join(SPLICE_TYPES)}")
            loss = (
                s.rules.loss_db.splice if connection_type == "splice" else s.rules.loss_db.connector
            )
            new["connection_type"] = connection_type
            new["loss_db"] = loss
            changes.append(f"now a {connection_type} ({loss:g} dB from the rulebook)")
            values["connection_type"] = connection_type
        for side, number in (("source", source_fiber), ("dest", dest_fiber)):
            if number is None:
                continue
            cable = row.get(f"{side}_cable_uuid")
            if not cable or cable not in s.design:
                return _rejected("edit_splice", f"the {side} end is a port, not a cable fibre")
            try:
                n = int(number)
            except (TypeError, ValueError):
                return _rejected("edit_splice", "a fibre number is a whole number")
            cf = s.design.get(cable)
            capacity = int(cf.props.get("fiber_count", 0)) * int(cf.props.get("cable_count", 1))
            if not 1 <= n <= capacity:
                return _rejected(
                    "edit_splice", f"{_label(cf)} carries fibres 1 to {capacity}, not {n}"
                )
            t, fb = tube_fiber(n, FIBERS_PER_TUBE)
            if (row.get(f"{side}_tube"), row.get(f"{side}_fiber")) == (t, fb):
                continue
            taken = next(
                (
                    c
                    for c in conns
                    if c is not row
                    and c.get(f"{side}_cable_uuid") == cable
                    and (c.get(f"{side}_tube"), c.get(f"{side}_fiber")) == (t, fb)
                ),
                None,
            )
            if taken:
                return _rejected(
                    "edit_splice",
                    f"fibre {n} of {_label(cf)} (T{t}:{fb}) is already used: {_end_text(s, taken, 'source')} → {_end_text(s, taken, 'dest')}",
                )
            new[f"{side}_tube"], new[f"{side}_fiber"] = t, fb
            changes.append(f"{side} end on fibre {n} (T{t}:{fb})")
            values[f"{side}_fiber"] = n
        for key, value in (("tray_ref", tray_ref), ("notes", notes)):
            if value is not None and (value.strip() or None) != row.get(key):
                new[key] = value.strip() or None
                changes.append(
                    f"{'tray' if key == 'tray_ref' else 'note'} {value.strip() or 'cleared'}"
                )
                values[key] = new[key]
        if not changes:
            return _rejected("edit_splice", "nothing to change")
    started = _begin(s)
    rows = [c for c in conns if c is not row]
    if not remove:
        rows.append(new)
    s.design.set_table("fiber_connection", rows)
    if remove:
        _recount_points(s)
    result = ToolResult(
        tool="edit_splice",
        summary=f"Splice in {where} ({before_text}): {'; '.join(changes)}",
        decisions=[
            Decision(
                stage="solve",
                tool="edit_splice",
                element=closure,
                reason=f"Splice {before_text} in {where} changed by the engineer ({'; '.join(changes)}): {why}.",
                values={"connection": connection_uuid, **values, "removed": remove},
                source="user",
            )
        ],
        data={"connection": connection_uuid, "element": closure},
    )
    if remove:
        result.warnings.append(
            "the fibre through this splice is now cut: trace it again or re-patch"
        )
    return _record_edit(s, _checked(s, result), started)


def list_violations(s: Session) -> dict[str, Any]:
    """The rule check of the design as it stands, per element, for the map (nothing is written)."""
    violations, _stats = run_all_checks(s.design, s.rules)
    by_check: dict[str, int] = {}
    for v in violations:
        by_check[v.check] = by_check.get(v.check, 0) + 1
    return {
        "count": len(violations),
        "by_check": by_check,
        "items": [
            {"check": v.check, "element": v.element, "message": v.message, "severity": v.severity}
            for v in violations
        ][:2000],
    }


# ---- attributes and assignments ---------------------------------------------------------------


def set_attribute(s: Session, element_uuid: str, key: str, value: Any, reason: str) -> ToolResult:
    """Change one attribute the engineer owns: a name, a status, a note, a cable's fibre or cable
    count, a cabinet's or FAT's port capacity, a chamber's type."""
    why = _reason("set_attribute", reason)
    if not why:
        return _rejected("set_attribute", "a reason is required; every manual edit records why")
    if element_uuid not in s.design:
        return _rejected("set_attribute", f"no element {element_uuid} in the design")
    f = s.design.get(element_uuid)
    kind = f.kind or ""
    allowed = EDITABLE["*"] + EDITABLE.get(kind, ())
    if key not in allowed:
        return _rejected(
            "set_attribute", f"{key} is not editable on a {kind}; editable: {', '.join(allowed)}"
        )
    r = s.rules
    v: Any = value
    if key in ("fiber_count", "cable_count", "port_capacity"):
        try:
            v = int(value)
        except (TypeError, ValueError):
            return _rejected("set_attribute", f"{key} takes a whole number")
        if v <= 0:
            return _rejected("set_attribute", f"{key} must be positive")
        ladder = {"feeder": r.fibers.feeder, "distribution": r.fibers.distribution}.get(kind)
        if key == "fiber_count" and ladder and v not in ladder:
            return _rejected(
                "set_attribute",
                f"{v} fibres is not on the {kind} ladder ({', '.join(map(str, ladder))})",
            )
        if key == "port_capacity" and kind == "fdh" and v not in r.fdh.capacities:
            return _rejected(
                "set_attribute",
                f"an FDH comes in {', '.join(map(str, r.fdh.capacities))} ports",
            )
    elif key == "slack_m":
        try:
            v = float(value)
        except (TypeError, ValueError):
            return _rejected("set_attribute", "slack_m takes a number of metres")
        if v < 0:
            return _rejected("set_attribute", "slack_m cannot be negative")
    elif key == "status":
        if value not in STATUSES:
            return _rejected("set_attribute", f"status is one of {', '.join(STATUSES)}")
    elif key == "manhole_type":
        if value not in r.chambers.types:
            return _rejected(
                "set_attribute", f"a chamber is one of {', '.join(sorted(r.chambers.types))}"
            )
    else:
        v = str(value).strip()
        if key == "name" and not v:
            return _rejected("set_attribute", "a name cannot be empty")
    old = f.props.get(key)
    if old == v:
        return _rejected("set_attribute", f"{key} is already {v}")
    started = _begin(s)
    f.props[key] = v
    if key == "manhole_type":
        f.props["dimensions_cm"] = r.chambers.types[v]
    if key == "slack_m":
        f.props["total_length_m"] = round(float(f.props.get("length_m", 0.0)) + v, 1)
    if key == "port_capacity" and kind == "fdh":
        f.openftth["capacity"] = v
    f.openftth.setdefault("edited", {})[key] = why
    result = ToolResult(
        tool="set_attribute",
        summary=f"{_label(f)}: {FIELD_WORDS.get(key, key)} {old} → {v}",
        decisions=[
            Decision(
                stage="solve",
                tool="set_attribute",
                element=element_uuid,
                reason=f"{key} of {_label(f)} set to {v} by the engineer (was {old}): {why}.",
                values={"key": key, "old": old, "new": v},
                source="user",
            )
        ],
        data={"element": element_uuid, "key": key, "old": old, "new": v},
    )
    return _record_edit(s, _checked(s, result), started)


def reassign(
    s: Session,
    element_uuid: str | None,
    to_uuid: str,
    reason: str,
    element_uuids: list[str] | None = None,
) -> ToolResult:
    """Give homes to a FAT, or FATs to a cabinet (one, or a whole selection at once); the PONs,
    routes, sizes and fibres that depend on it are worked out again, as one version."""
    why = _reason("reassign", reason)
    if not why:
        return _rejected("reassign", "a reason is required; every manual edit records why")
    ids = list(dict.fromkeys([*(element_uuids or []), *([element_uuid] if element_uuid else [])]))
    if not ids:
        return _rejected("reassign", "say what to reassign")
    for u in [*ids, to_uuid]:
        if u not in s.design:
            return _rejected("reassign", f"no element {u} in the design")
    to = s.design.get(to_uuid)
    want = {"fat": "home", "fdh": "fat"}.get(to.kind or "")
    if want is None:
        return _rejected("reassign", "reassign homes to a FAT, or FATs to an FDH cabinet")
    movers = [s.design.get(u) for u in ids if s.design.get(u).kind == want]
    movers = [f for f in movers if f.openftth.get(f"{to.kind}_id") != to_uuid]
    if not movers:
        return _rejected(
            "reassign",
            f"nothing to move: select {want}s that are not on {_label(to)} yet",
        )
    skipped = len(ids) - len(movers)
    started = _begin(s)
    olds: list[str | None] = []
    for f in movers:
        units = int(f.openftth.get("units", 1))
        if want == "home":
            old = f.openftth.get("fat_id")
            if old and old in s.design:
                prev = s.design.get(old)
                prev.openftth["units"] = max(0, int(prev.openftth.get("units", 0)) - units)
            to.openftth["units"] = int(to.openftth.get("units", 0)) + units
            f.openftth["fat_id"] = to_uuid
            f.openftth["fat_ids"] = [to_uuid]
            f.openftth["fdh_id"] = to.openftth.get("fdh_id")
            if to.openftth.get("pop_id"):
                f.openftth["pop_id"] = to.openftth["pop_id"]
        else:
            old = f.openftth.get("fdh_id")
            if old and old in s.design:
                prev = s.design.get(old)
                prev.openftth["units"] = max(0, int(prev.openftth.get("units", 0)) - units)
                prev.openftth["fats"] = max(0, int(prev.openftth.get("fats", 0)) - 1)
            to.openftth["units"] = int(to.openftth.get("units", 0)) + units
            to.openftth["fats"] = int(to.openftth.get("fats", 0)) + 1
            f.openftth["fdh_id"] = to_uuid
            for h in s.design.by_kind("home"):
                if h.openftth.get("fat_id") == f.element_uuid:
                    h.openftth["fdh_id"] = to_uuid
        olds.append(old)
    warnings: list[str] = []
    if skipped:
        warnings.append(
            f"{skipped} selected element(s) are not {want}s or already there; left as they are"
        )
    if to.kind == "fat":
        cap = int(to.props.get("port_capacity", 0) or 0)
        if cap and int(to.openftth["units"]) > cap:
            warnings.append(
                f"{_label(to)} now serves {to.openftth['units']} units over its {cap} ports"
            )
    _sync_connections(s)
    # Everything downstream of the assignment is worked out again, recorded with this edit.
    s.deferred = []
    try:
        assign_pon(s)
    finally:
        pre, s.deferred = s.deferred, None
    sub = pre + _rerun(s)
    if len(movers) == 1:
        old = olds[0]
        what = (
            f"{_label(movers[0])} moved from {_label(s.design.get(old))} to {_label(to)}"
            if old and old in s.design
            else f"{_label(movers[0])} given to {_label(to)}"
        )
    else:
        what = f"{len(movers)} {'homes' if want == 'home' else 'FATs'} moved to {_label(to)}"
    result = _merge(
        ToolResult(
            tool="reassign",
            summary=what,
            decisions=[
                Decision(
                    stage="solve",
                    tool="reassign",
                    element=f.element_uuid,
                    reason=f"{_label(f)} reassigned to {_label(to)} by the engineer: {why}.",
                    values={"from": old, "to": to_uuid, "units": int(f.openftth.get("units", 1))},
                    source="user",
                )
                for f, old in zip(movers, olds, strict=True)
            ],
            warnings=warnings,
            data={
                "element": movers[0].element_uuid,
                "elements": [f.element_uuid for f in movers],
                "to": to_uuid,
            },
        ),
        sub,
    )
    return _record_edit(s, _checked(s, result), started)


def complete_network(s: Session, reason: str) -> ToolResult:
    """Finish a network the engineer laid out by hand: from the sites they placed and the homes
    and FATs they assigned, route the feeder and distribution along the cables they drew (and
    only where they drew none, the cheapest way), connect every assigned home with its drop,
    place chambers, size the cables and number the fibres. One version, undone in one step.
    Nothing is clustered or placed: every site stays the engineer's."""
    why = _reason("complete_network", reason)
    if not why:
        return _rejected("complete_network", "a reason is required; every manual edit records why")
    d = s.design
    pops, fdhs, fats = d.by_kind("pop"), d.by_kind("fdh"), d.by_kind("fat")
    if not pops:
        return _rejected("complete_network", "place a POP first")
    if not fdhs:
        return _rejected("complete_network", "place an FDH cabinet first")
    if not fats:
        return _rejected("complete_network", "place a FAT first")
    loose = [
        _label(f) for f in fats if f.openftth.get("fdh_id") not in {x.element_uuid for x in fdhs}
    ]
    if loose:
        return _rejected(
            "complete_network",
            f"{', '.join(loose[:3])}{' and more' if len(loose) > 3 else ''} feed(s) no cabinet: reassign them to an FDH first",
        )
    homes = d.by_kind("home")
    served = [h for h in homes if h.openftth.get("fat_id") in {f.element_uuid for f in fats}]
    if not served:
        return _rejected(
            "complete_network", "no home is assigned to a FAT yet: reassign homes first"
        )
    started = _begin(s)
    # Units follow the assignments as they stand (the engine counts them; nobody types them).
    by_fat: dict[str, int] = {}
    for h in served:
        by_fat[h.openftth["fat_id"]] = by_fat.get(h.openftth["fat_id"], 0) + int(
            h.openftth.get("units", 1)
        )
    for f in fats:
        f.openftth["units"] = by_fat.get(f.element_uuid, 0)
    for c in fdhs:
        mine = [f for f in fats if f.openftth.get("fdh_id") == c.element_uuid]
        c.openftth["units"] = sum(int(f.openftth["units"]) for f in mine)
        c.openftth["fats"] = len(mine)
    _sync_connections(s)
    s.deferred = []
    try:
        for step in (
            assign_pon,
            route_feeder,
            route_distribution,
            route_drops,
            place_chambers,
            size_cables,
            assign_fibers,
        ):
            step(s)
    finally:
        sub, s.deferred = s.deferred, None
    unserved = len(homes) - len(served)
    result = _merge(
        ToolResult(
            tool="complete_network",
            summary=f"Network completed from your layout: {len(served)} home(s) on {len(fats)} FAT(s) under {len(fdhs)} cabinet(s)",
            decisions=[
                Decision(
                    stage="solve",
                    tool="complete_network",
                    reason=f"The engineer laid out the sites and assignments by hand ({why}); the engine routed, sized and numbered the network from them, following every cable they drew.",
                    values={"homes": len(served), "fats": len(fats), "fdhs": len(fdhs)},
                    source="user",
                )
            ],
            warnings=[f"{unserved} home(s) have no FAT and are not served"] if unserved else [],
            data={"homes": len(served), "unserved": unserved},
        ),
        sub,
    )
    result.summary = result.summary.replace("; re-ran ", "; ran ")
    return _record_edit(s, _checked(s, result), started)


def move_site(s: Session, element_uuid: str, lon: float, lat: float, reason: str) -> ToolResult:
    """The engine's move_site, on the undo stack like every other edit."""
    before_version = s.project.head()
    out = _pipeline.move_site(s, element_uuid, lon, lat, reason)
    if out.summary.startswith("rejected"):
        return out
    return _push(s, _checked(s, out), before_version)


# ---- undo and redo ----------------------------------------------------------------------------


def _restore(s: Session, version: str) -> None:
    s.design = s.project.design_at(version)
    s.trees = {}
    pops = s.design.by_kind("pop")
    if pops:
        s.cache["pop_node"] = int(pops[0].openftth["node"])


def _unchanged_since(s: Session, version: str | None) -> bool:
    """The design is what it was at `version`: no version since, or only ones that changed
    nothing in it (a rule check, a work order opened)."""
    if not version:
        return False
    if s.project.head() == version:
        return True
    try:
        return s.project.design_at(version).diff(s.design).is_empty()
    except Exception:  # noqa: BLE001 - an unknown version is simply not undoable
        return False


def undo_edit(s: Session) -> ToolResult:
    """Step back over the engineer's last edit. Only when the design has not changed since,
    so a solver run in between is never silently thrown away."""
    meta = s.project.meta
    stack = list(meta.get("edit_stack") or [])
    if not stack:
        return _rejected("undo_edit", "nothing to undo")
    top = stack[-1]
    if not _unchanged_since(s, top["after"]):
        return _rejected(
            "undo_edit",
            "the design changed since that edit; go back through Versions instead",
        )
    if not top.get("before"):
        return _rejected("undo_edit", "that edit was the first version; there is nothing before it")
    before = s.design.copy()
    s.project.set_stage("solve")
    _restore(s, top["before"])
    result = ToolResult(
        tool="undo_edit",
        summary=f"Undone: {top['summary']}",
        decisions=[
            Decision(
                stage="solve",
                tool="undo_edit",
                reason=f"The engineer undid their edit ({top['tool']}: {top['summary']}).",
                values={"undone": top["after"], "back_to": top["before"]},
                source="user",
            )
        ],
        data={"undone": top},
    )
    out = _commit(s, _checked(s, result), before)
    stack.pop()
    # The undo commits a new version with the design of `before`: the edit below now ends there,
    # so the next undo can step over it too.
    if stack and stack[-1]["after"] == top["before"]:
        stack[-1] = {**stack[-1], "after": out.version}
    redo = list(meta.get("redo_stack") or [])
    redo.append({**top, "undo": out.version, "undo_from": top["after"]})
    meta["edit_stack"] = stack
    meta["redo_stack"] = redo[-100:]
    s.project._write_meta()
    return out


def redo_edit(s: Session) -> ToolResult:
    """Apply again the edit undone last, while nothing else has changed since the undo."""
    meta = s.project.meta
    redo = list(meta.get("redo_stack") or [])
    if not redo:
        return _rejected("redo_edit", "nothing to redo")
    top = redo[-1]
    if not _unchanged_since(s, top.get("undo")):
        return _rejected("redo_edit", "the design changed since the undo; nothing to redo")
    before = s.design.copy()
    s.project.set_stage("solve")
    _restore(s, top["after"])
    result = ToolResult(
        tool="redo_edit",
        summary=f"Redone: {top['summary']}",
        decisions=[
            Decision(
                stage="solve",
                tool="redo_edit",
                reason=f"The engineer redid their edit ({top['tool']}: {top['summary']}).",
                values={"redone": top["after"]},
                source="user",
            )
        ],
        data={"redone": top},
    )
    out = _commit(s, _checked(s, result), before)
    redo.pop()
    # The redo brings back the design the next redo was undone from.
    if redo and redo[-1].get("undo") == top.get("undo_from"):
        redo[-1] = {**redo[-1], "undo": out.version}
    stack = list(meta.get("edit_stack") or [])
    stack.append(
        {k: v for k, v in top.items() if k not in ("undo", "undo_from")} | {"after": out.version}
    )
    meta["edit_stack"] = stack
    meta["redo_stack"] = redo
    s.project._write_meta()
    return out


def edit_history(s: Session) -> dict[str, Any]:
    """What undo and redo would do now."""
    meta = s.project.meta
    stack = list(meta.get("edit_stack") or [])
    redo = list(meta.get("redo_stack") or [])
    return {
        "can_undo": bool(stack)
        and bool(stack[-1].get("before"))
        and _unchanged_since(s, stack[-1]["after"]),
        "undo": stack[-1] if stack else None,
        "can_redo": bool(redo) and _unchanged_since(s, redo[-1].get("undo")),
        "redo": redo[-1] if redo else None,
    }
