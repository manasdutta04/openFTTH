"""The design tools, and the headless pipeline that runs them in order.

Each public function here is one agent-callable tool: it mutates the design through the
project (which commits), and returns a `ToolResult`. `design_all` chains them for the CLI.
"""

from __future__ import annotations

import json
import math
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import geopandas as gpd
import networkx as nx
import numpy as np
import shapely
from pyproj import Transformer
from shapely.geometry import LineString, Point, mapping

from .deliver import build_boq, render_map
from .deliver.boq import boq_to_csv
from .ingest.inputs import Inputs, load_inputs, profile_for
from .ingest.profiles import ImportProfile
from .interchange import Design, Feature, Metadata, stable_uuid
from .network import check_operator, homes_behind, load_operator_pack, trace_home
from .network.operator import OperatorPack
from .project import Project
from .results import Decision, ToolResult
from .rules import Rulebook
from .solve.cluster import WORK_LIMIT, Demand, capacitated_sites, repair_reach
from .solve.pops import split_catchments
from .solve.routing import RoutedTree, route_tree
from .twin import Twin, build_twin
from .validate import run_all_checks
from .validate.checks import violation_summary

# FDHs are clustered with a reach shorter than max_dist_m because the routed Steiner tree path
# to a FAT is longer than the shortest path used during clustering.
FDH_REACH_FACTOR = 0.6


@dataclass
class Session:
    """Everything a sequence of tool calls shares for one project."""

    project: Project
    rules: Rulebook
    design: Design
    twin: Twin | None = None
    trees: dict[str, RoutedTree] = field(default_factory=dict)  # "feeder" | fdh uuid → tree
    cache: dict[str, Any] = field(default_factory=dict)
    # While a steering tool re-runs solvers, their commits are collected here instead of
    # written, so the engineer's change lands as one version.
    deferred: list[ToolResult] | None = None

    @property
    def pid(self) -> str:
        return self.project.project_id

    def uid(self, element_type: str, key: str) -> str:
        return stable_uuid(self.pid, element_type, key)


def open_session(
    project_dir: str | Path, rules: Rulebook, project_id: str | None = None
) -> Session:
    project = Project(project_dir, project_id=project_id)
    design = (
        project.load_design() if any(project.bundle_dir.glob("*.geojson")) else Design(Metadata())
    )
    return Session(project=project, rules=rules, design=design)


# ---- helpers ------------------------------------------------------------------------------


def _pinned(s: Session, kind: str) -> list[Feature]:
    """Sites of `kind` the engineer placed by hand (move_site). Solvers keep them where they
    stand, with their ids; only an outdoor site can be pinned."""
    return [
        f
        for f in s.design.by_kind(kind)
        if f.openftth.get("placed_by") == "engineer" and not f.openftth.get("mdu")
    ]


def _pin_decision(tool: str, pinned: list[Feature], what: str) -> Decision:
    names = ", ".join(str(f.props.get("name") or f.element_uuid[:8]) for f in pinned)
    return Decision(
        stage="solve",
        tool=tool,
        reason=f"Kept {names} where the engineer placed {'it' if len(pinned) == 1 else 'them'}; "
        f"the {what} around {'it' if len(pinned) == 1 else 'them'} were placed by the solver. "
        "Reasons on record: "
        + "; ".join(str(f.openftth.get("placed_reason", "")) for f in pinned)
        + ".",
        values={"pinned": [f.element_uuid for f in pinned]},
        source="user",
    )


_TO_WGS: dict[int, tuple[Any, Transformer]] = {}


def _wgs(twin: Twin, geom) -> Any:
    """A twin geometry (metric CRS) in WGS 84. The transformer is made once per CRS: building a
    GeoSeries per element cost about a millisecond each, minutes on a large area."""
    crs = twin.crs
    hit = _TO_WGS.get(id(crs))
    if hit is None or hit[0] is not crs:
        hit = (crs, Transformer.from_crs(crs, 4326, always_xy=True))
        _TO_WGS[id(crs)] = hit
    tr = hit[1]
    return shapely.transform(geom, lambda xy: np.column_stack(tr.transform(xy[:, 0], xy[:, 1])))


# Where an object's data comes from, by the tool that first wrote it.
TOOL_SOURCE = {
    "import_osm": "osm",
    "import_data": "import",
    "build_twin": "osm",
    "move_site": "user",
    "exclude_building": "user",
    "forbid_street": "user",
    "add_site": "user",
    "draw_cable": "user",
    "split_cable": "user",
}


# Tools that record or change the network's state but never create design objects.
NETWORK_TOOLS = {
    "open_work_order",
    "post_work_order",
    "set_lifecycle",
    "set_operator_pack",
    "set_rule",
}


def _pack(s: Session) -> OperatorPack:
    name = s.project.meta.get("operator", "generic")
    if s.cache.get("operator_pack_name") != name:
        s.cache["operator_pack"] = load_operator_pack(name)
        s.cache["operator_pack_name"] = name
    return s.cache["operator_pack"]


def _stamp(s: Session, tool: str, before: Design | None) -> None:
    """Give every object a lifecycle state and provenance. An object a solver rebuilt with the
    same id keeps what it had; a new one starts in the pack's initial state."""
    initial = _pack(s).lifecycle.initial
    for f in s.design:
        if "lifecycle" in f.openftth:
            continue
        prev = None
        if before is not None and f.element_uuid in before:
            prev = before.get(f.element_uuid)
        if prev is not None and "lifecycle" in prev.openftth:
            f.openftth["lifecycle"] = prev.openftth["lifecycle"]
            if "provenance" in prev.openftth:
                f.openftth.setdefault("provenance", prev.openftth["provenance"])
            continue
        f.openftth["lifecycle"] = initial
        if tool in NETWORK_TOOLS:
            # Made before provenance was recorded: say where such objects come from, not who.
            src = "osm" if f.kind in ("building", "home", "service_area") else "solver"
            prov = {"source": src, "created_by": "unrecorded"}
        else:
            prov = {"source": TOOL_SOURCE.get(tool, "solver"), "created_by": tool}
        f.openftth.setdefault("provenance", prov)


def _clear_solved(s: Session, kind: str) -> None:
    """Remove what a solver made of `kind` before it makes it again; what the engineer drew
    (cables, chambers) stays."""
    for f in s.design.by_kind(kind):
        if f.openftth.get("drawn_by") != "engineer" and f.openftth.get("placed_by") != "engineer":
            s.design.remove(f.element_uuid)


#: How much cheaper a street edge the engineer drew a cable along is for the routing trees: low
#: enough that a tree follows the drawn cables wherever they lead, while edges priced at zero
#: would let the approximation wander.
DRAWN_PREFERENCE = 0.01


def _take_drawn(s: Session, kind: str) -> tuple[set[str], list[Feature]]:
    """The street edges the engineer drew cables of `kind` along, and those cables, taken out of
    the design: the router lays its sections over the same edges, so they would be doubled."""
    edges: set[str] = set()
    taken: list[Feature] = []
    for f in sorted(s.design.by_kind(kind), key=lambda f: f.element_uuid):
        mine = f.openftth.get("drawn_edges")
        if mine is None and f.openftth.get("drawn_by") == "engineer":
            mine = list(f.openftth.get("edges", {}))
        if mine:
            edges.update(mine)
            taken.append(f)
            s.design.remove(f.element_uuid)
    return edges, taken


def _favouring(G: nx.Graph, drawn: set[str]) -> tuple[nx.Graph, str]:
    """The street graph to route on, and the edge weight to use: with drawn edges, a copy where
    they cost a fraction, so the tree follows them (cable costs still come from `cost`)."""
    if not drawn:
        return G, "cost"
    H = G.copy()
    for u, v, d in H.edges(data=True):
        key = f"{min(u, v)}-{max(u, v)}"
        d["preference"] = d["cost"] * (DRAWN_PREFERENCE if key in drawn else 1.0)
    return H, "preference"


def _settle_drawn(s: Session, kind: str, drawn: set[str], taken: list[Feature]) -> list[str]:
    """After routing `kind`: mark which section edges the engineer drew, and keep every drawn
    stretch the trees did not use as a cable of its own (a spare, say): nothing the engineer
    drew is lost, and nothing is laid twice."""
    if not drawn:
        return []
    used: set[str] = set()
    for c in s.design.by_kind(kind):
        mine = sorted(k for k in c.openftth.get("edges", {}) if k in drawn)
        used.update(c.openftth.get("edges", {}))
        if mine:
            c.openftth["drawn_edges"] = mine
            if len(mine) == len(c.openftth["edges"]):
                c.openftth["drawn_by"] = "engineer"
    kept = 0
    for f in taken:
        mine = set(f.openftth.get("drawn_edges") or f.openftth.get("edges", {}))
        if not mine & used:
            s.design.add(f)
            continue
        kept += _keep_unused(s, kind, f, sorted(mine - used))
    if kept:
        return [
            f"{kept} stretch(es) of the {kind} you drew serve no site of the tree; kept as drawn"
        ]
    return []


def _surfaces(twin: Twin, pairs: list[tuple[int, int]]) -> dict[str, float]:
    out: dict[str, float] = {}
    for u, v in pairs:
        out[twin.graph[u][v]["surface"]] = out.get(twin.graph[u][v]["surface"], 0.0) + float(
            twin.graph[u][v]["length_m"]
        )
    return {k: round(m, 1) for k, m in sorted(out.items())}


def _keep_unused(s: Session, kind: str, f: Feature, edges: list[str]) -> int:
    """The drawn edges of `f` no tree uses, as cables of their own (one per unbroken run)."""
    twin = _twin(s)
    G = nx.Graph()
    for key in edges:
        a, b = (int(x) for x in key.split("-"))
        if twin.graph.has_edge(a, b):
            G.add_edge(a, b)
    n = 0
    for comp in sorted(nx.connected_components(G), key=min):
        sub = G.subgraph(comp)
        ends = sorted(v for v in sub if sub.degree(v) == 1) or [min(comp)]
        nodes = [ends[0]] + [v for _, v in nx.dfs_edges(sub, ends[0])]
        if len(nodes) < 2:
            continue
        n += 1
        pairs = list(zip(nodes[:-1], nodes[1:], strict=False))
        keys = {f"{min(u, v)}-{max(u, v)}": (u, v) for u, v in pairs}
        length = sum(twin.graph[u][v]["length_m"] for u, v in pairs)
        slack = s.rules.slack.per_closure_m
        piece = f.model_copy(deep=True)
        piece.element_uuid = s.uid("cable.underground", f"kept:{f.element_uuid}:{nodes[0]}")
        piece.geometry = _wgs(twin, twin.path_line(nodes))
        piece.props.update(
            {
                "name": f"{f.props.get('name', kind)}-{n}",
                "from_label": f"N{nodes[0]}",
                "to_label": f"N{nodes[-1]}",
                "length_m": round(length, 1),
                "slack_m": slack,
                "total_length_m": round(length + slack, 1),
            }
        )
        piece.openftth.update(
            {
                "edges": {
                    k: (
                        f"crossing:{twin.graph[u][v]['crossing']}"
                        if twin.graph[u][v].get("crossing")
                        else twin.graph[u][v]["surface"],
                        round(twin.graph[u][v]["length_m"], 2),
                    )
                    for k, (u, v) in keys.items()
                },
                "surfaces": _surfaces(twin, pairs),
                "drawn_edges": sorted(keys),
                "drawn_by": "engineer",
                "downstream_units": 0,
                "trench_cost": round(sum(twin.graph[u][v]["cost"] for u, v in pairs), 2),
            }
        )
        for k in ("fdh_id", "pop_id", "fiber_start", "fiber_end", "fibers_used"):
            piece.openftth.pop(k, None)
        s.design.add(piece)
    return n


def _commit(s: Session, result: ToolResult, before: Design | None = None) -> ToolResult:
    _stamp(s, result.tool, before)
    if before is not None:
        result.changes = before.diff(s.design).summary()
    if s.deferred is not None:
        s.deferred.append(result)
        return result
    s.project.commit(result, s.design)
    return result


# The engineer's own inputs that live in the project meta next to the area and OSM paths and
# survive a re-import: buildings taken out of the design and streets that must not be dug up.
STEERING_INPUTS = ("excluded_buildings", "no_dig")


def _steering(s: Session, key: str) -> list[dict[str, Any]]:
    inputs = s.project.meta.setdefault("inputs", {})
    return list(inputs.get(key) or [])


def _excluded_osm_ids(s: Session) -> set[str]:
    return {str(x["osm_id"]) for x in _steering(s, "excluded_buildings")}


def _no_dig_ids(s: Session) -> set[str]:
    return {str(x["osm_id"]) for x in _steering(s, "no_dig")}


def _zones(s: Session) -> gpd.GeoDataFrame:
    """Every zone (WGS 84) with its `kind` (no_dig or keep_out) and `reason`: the zone elements
    of the design, or for a project imported before zones were elements, the imported ones."""
    feats = s.design.by_kind("zone")
    if feats:
        return gpd.GeoDataFrame(
            {
                "kind": [str(f.openftth.get("zone", "no_dig")) for f in feats],
                "reason": [str(f.props.get("reason", "")) for f in feats],
                "id": [f.element_uuid for f in feats],
            },
            geometry=[f.geometry for f in feats],
            crs=4326,
        )
    cached = s.cache.get("no_dig_zones")
    if cached is not None and not cached.empty:
        out = cached.copy()
        if "kind" not in out.columns:
            out["kind"] = "no_dig"
        return out
    return gpd.GeoDataFrame({"kind": [], "reason": []}, geometry=[], crs=4326)


def _keep_out(s: Session, twin: Twin) -> tuple[set[int], Any]:
    """The street nodes inside keep-out zones (no equipment there) and the zones' union in the
    twin's CRS (None without any)."""
    zones = _zones(s)
    zones = zones[zones["kind"] == "keep_out"]
    if zones.empty:
        return set(), None
    polys = zones.to_crs(twin.crs).geometry.union_all()
    nodes = list(twin.graph.nodes)
    pts = shapely.points(np.asarray([twin.xy[n] for n in nodes], dtype=float))
    inside = shapely.contains(polys, pts)
    return {int(n) for n, hit in zip(nodes, inside, strict=True) if hit}, polys


def _nearest_allowed(twin: Twin, x: float, y: float, blocked: set[int]) -> tuple[int, float]:
    """The nearest street node to (x, y) outside the keep-out zones."""
    node, d = twin.nearest_node(x, y)
    if node not in blocked:
        return node, d
    assert twin._tree_nodes is not None
    k = min(len(twin._tree_nodes), 256)
    dists, idxs = twin.kdtree.query([x, y], k=k)
    for dd, i in zip(dists, idxs, strict=False):
        n = int(twin._tree_nodes[i])
        if n not in blocked:
            return n, float(dd)
    return node, d


def _finish_twin(s: Session, twin: Twin) -> Twin:
    """Apply the engineer's no-dig streets and the no-dig and keep-out zones to a fresh twin."""
    no_dig = _no_dig_ids(s)
    if no_dig:
        twin.forbid_ways(no_dig)
    else:
        twin.streets["no_dig"] = False
    zones = _zones(s)
    if not zones.empty:
        edges, length = twin.forbid_zones(zones)
        twin.decisions.append(
            Decision(
                stage="twin",
                tool="build_twin",
                reason=f"{edges} street edge(s), {length:.0f} m, lie in {len(zones)} no-dig or "
                "keep-out zone(s) and left the route graph; existing ducts through them stay usable.",
                values={"edges": edges, "length_m": round(length, 1), "zones": len(zones)},
                source="user",
            )
        )
    twin.resnap_homes()
    return twin


def _write_streets(s: Session, twin: Twin) -> None:
    """The twin's street layer for the renderer, with the no-dig flag per way."""
    cols = ["highway", "surface", "cost_per_m", "osm_id", "no_dig", "geometry"]
    streets = twin.streets[cols].to_crs(4326)
    streets.to_file(s.project.twin_dir / "streets.geojson", driver="GeoJSON")


# ---- Ingest / Twin ------------------------------------------------------------------------


PROFILE_FILE = "import.yaml"


def _filter_excluded(s: Session, homes: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Homes in buildings the engineer excluded do not enter the design."""
    excluded = _excluded_osm_ids(s)
    if not excluded or homes.empty:
        return homes
    col = "building_id" if "building_id" in homes.columns else "osm_id"
    return homes[~homes[col].astype(str).isin(excluded)].copy()


def _load_profile(s: Session) -> ImportProfile | None:
    """The project's import profile: the saved one, or the classic area + OSM pair."""
    inputs = s.project.meta.get("inputs") or {}
    saved = inputs.get("profile")
    if saved and Path(saved).exists():
        return ImportProfile.load(saved)
    if inputs.get("area"):
        return profile_for(inputs["area"], inputs.get("osm"))
    return None


def _levels(value: Any) -> int | None:
    """A building's levels as a whole number, when the data has a usable one."""
    try:
        v = int(float(str(value).split(";")[0]))
    except (TypeError, ValueError):
        return None
    return v if 0 < v < 200 else None


def _riser(s: Session, h: Feature, buildings: dict[str, Feature]) -> tuple[int, float]:
    """(floors, riser metres) for a home with several units; (0, 0) for a house."""
    units = int(h.openftth.get("units", 1))
    if units <= 1:
        return 0, 0.0
    b = buildings.get(str(h.openftth.get("building", "")))
    floors = s.rules.inside.floors(units, (b.props.get("levels") if b else None))
    return floors, round(floors * s.rules.inside.floor_height_m, 1)


def import_area_and_osm(s: Session, area_path: str | Path, osm_path: str | Path) -> ToolResult:
    """The classic start: an area polygon and an OpenStreetMap extract."""
    return import_data(s, profile_for(area_path, osm_path), tool="import_osm")


def import_data(
    s: Session, profile: ImportProfile | str | Path, tool: str = "import_data"
) -> ToolResult:
    """Import a project's data from any sources an import profile names: the area, streets,
    buildings, an address register, existing ducts, poles and manholes, no-dig zones, and/or an
    OpenStreetMap extract. The profile is saved with the project so every later rebuild reads
    exactly the same data."""
    s.project.set_stage("ingest")
    if not isinstance(profile, ImportProfile):
        profile = ImportProfile.load(profile)
    before = s.design.copy()
    inp = load_inputs(profile, s.rules)
    homes_wgs = _filter_excluded(s, inp.homes)
    excluded = {x["osm_id"]: x for x in _steering(s, "excluded_buildings")}

    for kind in ("building", "service_area", "existing_pole", "existing_manhole", "existing_duct"):
        s.design.remove_where(kind=kind)
    s.design.remove_where(kind="zone", origin="import")
    for i, z in enumerate(inp.no_dig.itertuples(index=False)):
        s.design.add(
            Feature(
                element_uuid=s.uid("service_area", f"zone:import:{i}"),
                element_type="service_area",
                geometry=z.geometry,
                props={
                    "name": f"{'Keep-out' if z.kind == 'keep_out' else 'No-dig'} zone {i + 1}",
                    "reason": z.reason,
                },
                openftth={"kind": "zone", "zone": z.kind, "origin": "import"},
            )
        )
    area = inp.area
    area_series = gpd.GeoSeries([area], crs=4326)
    area_m2 = float(area_series.to_crs(area_series.estimate_utm_crs()).area.iloc[0])
    area_name = (
        Path(profile.by_role("area")[0].path).stem if profile.by_role("area") else profile.name
    )
    s.design.add(
        Feature(
            element_uuid=s.uid("service_area", "area"),
            element_type="service_area",
            geometry=area,
            props={"name": area_name, "area_m2": round(area_m2, 1)},
            openftth={"kind": "service_area"},
        )
    )
    for b in inp.buildings.itertuples(index=False):
        extra: dict[str, Any] = {}
        if str(b.osm_id) in excluded:
            extra = {"excluded": True, "excluded_reason": excluded[str(b.osm_id)]["reason"]}
        s.design.add(
            Feature(
                element_uuid=s.uid("building", b.osm_id),
                element_type="building",
                geometry=b.geometry,
                props={
                    "building_type": str(b.building or "yes"),
                    "street": b.street if isinstance(b.street, str) else None,
                    "street_number": b.housenumber if isinstance(b.housenumber, str) else None,
                    **({"levels": _levels(b.levels)} if _levels(b.levels) else {}),
                },
                openftth={"kind": "building", "osm_id": b.osm_id, **extra},
            )
        )
    # Existing infrastructure is in service already: it is recorded, not designed.
    existing = [
        (inp.poles, "pole", "existing_pole", "owner"),
        (inp.manholes, "manhole", "existing_manhole", "type"),
    ]
    for gdf, etype, kind, attr in existing:
        for r in gdf.itertuples(index=False):
            value = getattr(r, attr)
            s.design.add(
                Feature(
                    element_uuid=s.uid(etype, r.osm_id),
                    element_type=etype,
                    geometry=r.geometry,
                    props={
                        "name": str(r.osm_id).split(":")[-1],
                        attr: None if value in (None, "") else str(value),
                    },
                    openftth={
                        "kind": kind,
                        "source_id": r.osm_id,
                        "lifecycle": "in_service",
                        "provenance": {"source": "import", "created_by": tool},
                    },
                )
            )
    ducts = inp.streets[inp.streets["highway"] == "existing_duct"]
    for r in ducts.itertuples(index=False):
        s.design.add(
            Feature(
                element_uuid=s.uid("duct.pe", r.osm_id),
                element_type="duct.pe",
                geometry=r.geometry,
                props={
                    "name": str(r.osm_id).split(":")[-1],
                    "owner": r.name if isinstance(r.name, str) else None,
                },
                openftth={
                    "kind": "existing_duct",
                    "source_id": r.osm_id,
                    "lifecycle": "in_service",
                    "provenance": {"source": "import", "created_by": tool},
                },
            )
        )
    s.cache.update(
        {
            "inputs": inp,
            "streets": inp.streets,
            "homes_wgs": homes_wgs,
            "area": area,
            "no_dig_zones": inp.no_dig,
            "barriers": inp.barriers,
        }
    )
    saved = profile.save(s.project.dir / PROFILE_FILE)
    previous = s.project.meta.get("inputs") or {}
    osm = profile.by_role("osm")
    s.project.meta["inputs"] = {
        "profile": str(saved),
        "area": profile.by_role("area")[0].path if profile.by_role("area") else None,
        "osm": osm[0].path if osm else None,
        **{k: previous[k] for k in STEERING_INPUTS if previous.get(k)},
    }
    n_streets = int((inp.streets["highway"] != "existing_duct").sum())
    units = int(homes_wgs["units"].sum()) if not homes_wgs.empty else 0
    where = (
        "the address register" if inp.counts.get("addresses") else "building footprints (estimated)"
    )
    extras = [
        f"{v} {k.replace('_', ' ')}"
        for k, v in inp.counts.items()
        if k in ("existing_ducts", "poles", "manholes", "no_dig_zones") and v
    ]
    summary = (
        f"{n_streets} street segments, {len(inp.buildings)} buildings, {len(homes_wgs)} homes "
        f"({units} units) from {where}" + (f"; existing: {', '.join(extras)}" if extras else "")
    )
    result = ToolResult(
        tool=tool,
        summary=summary,
        decisions=inp.decisions,
        data={
            "streets": n_streets,
            "buildings": len(inp.buildings),
            "homes": len(homes_wgs),
            "units": units,
            "counts": inp.counts,
            "profile": str(saved),
        },
    )
    return _commit(s, result, before)


def _raw_twin(s: Session) -> Twin:
    """The street graph from the session's imported inputs, under the session's rulebook."""
    inp = s.cache.get("inputs")
    return build_twin(
        s.cache["streets"],
        s.cache["homes_wgs"],
        s.rules,
        s.cache.get("barriers"),
        getattr(inp, "buildings", None),
        getattr(inp, "poles", None),
    )


def build_twin_tool(s: Session) -> ToolResult:
    s.project.set_stage("twin")
    before = s.design.copy()
    if "streets" not in s.cache:
        _twin(s)
    twin = _finish_twin(s, _raw_twin(s))
    s.twin = twin
    # Homes become `to` features (one per building, with a unit count). A home that is
    # already in the design keeps what the solvers put on it (its FAT, PON, drop and path
    # lengths): rebuilding the twin refreshes the street snap, it does not undo the design.
    previous = {h.element_uuid: h.openftth for h in s.design.by_kind("home")}
    s.design.remove_where(kind="home")
    for h in twin.homes.itertuples(index=False):
        hid = s.uid("to", f"home:{h.osm_id}")
        s.design.add(
            Feature(
                element_uuid=hid,
                element_type="to",
                placement="indoor",
                geometry=_wgs(twin, h.geometry),
                props={"name": f"home {h.osm_id}", "port_capacity": int(h.units)},
                openftth={
                    **previous.get(hid, {}),
                    "kind": "home",
                    "units": int(h.units),
                    "home_kind": h.kind,
                    "node": int(h.node),
                    "offset_m": round(float(h.offset_m), 1),
                    "building": s.uid("building", getattr(h, "building_id", None) or h.osm_id),
                },
            )
        )
    # Twin streets (openFTTH-specific layer) for the renderer.
    _write_streets(s, twin)
    result = ToolResult(
        tool="build_twin",
        summary=f"street graph {twin.graph.number_of_nodes()} nodes / {twin.graph.number_of_edges()} edges in {twin.crs.to_string()}, {len(twin.homes)} homes snapped",
        decisions=list(twin.decisions),
        data={
            "nodes": twin.graph.number_of_nodes(),
            "edges": twin.graph.number_of_edges(),
            "crs": twin.crs.to_string(),
        },
    )
    return _commit(s, result, before)


# ---- Solve --------------------------------------------------------------------------------


def set_pop(
    s: Session,
    lon: float | None = None,
    lat: float | None = None,
    count: int | None = None,
    sites: list[list[float]] | None = None,
) -> ToolResult:
    """Place the POP, or several when the area holds more units than one POP serves
    (`pops.max_units`), `count` asks for, or `sites` (the engineer's [lon, lat] list) gives."""
    r = s.rules
    homes = s.design.by_kind("home")
    total = sum(int(h.openftth.get("units", 1)) for h in homes)
    given = [list(p) for p in (sites or [])]
    wanted = count or max(1, -(-total // r.pops.max_units))
    if len(given) > 1 or len(_pinned(s, "pop")) > 1 or (not given and wanted > 1 and lon is None):
        return _set_pops(s, given, wanted)
    for h in homes:
        h.openftth.pop("pop_id", None)
    if given:
        lon, lat = given[0]
    return _set_single_pop(s, lon, lat)


def _set_pops(s: Session, given: list[list[float]], wanted: int) -> ToolResult:
    """Several POPs and the catchment of each: the engineer's sites (or pinned POPs) when there
    are any, otherwise sites from a weighted k-means; homes split by a capacitated min-cost flow."""
    s.project.set_stage("solve")
    twin = _twin(s)
    r = s.rules
    before = s.design.copy()
    homes = sorted(s.design.by_kind("home"), key=lambda h: h.element_uuid)
    hxy = np.asarray([twin.xy[int(h.openftth["node"])] for h in homes], dtype=float)
    hunits = np.asarray([int(h.openftth.get("units", 1)) for h in homes], dtype=int)
    pinned = _pinned(s, "pop")
    decisions: list[Decision] = []
    fixed_nodes: list[int] = []
    keep_ids: list[str | None] = []
    if given:
        for lon, lat in given:
            p = gpd.GeoSeries([Point(lon, lat)], crs=4326).to_crs(twin.crs).iloc[0]
            node, _d = twin.nearest_node(p.x, p.y)
            fixed_nodes.append(int(node))
            keep_ids.append(None)
        decisions.append(
            Decision(
                stage="solve",
                tool="set_pop",
                reason=f"{len(given)} POP sites given by the engineer, each snapped to the nearest street node.",
                values={"pops": len(given)},
                source="user",
            )
        )
    elif pinned:
        fixed_nodes = [int(p.openftth["node"]) for p in pinned]
        keep_ids = [p.element_uuid for p in pinned]
        decisions.append(_pin_decision("set_pop", pinned, "catchments"))
    split = split_catchments(
        hxy,
        hunits,
        wanted,
        r.pops.max_units,
        fixed=twin.xy[fixed_nodes] if fixed_nodes else None,
    )
    blocked, _zones_m = _keep_out(s, twin)
    nodes = fixed_nodes or [int(_nearest_allowed(twin, x, y, blocked)[0]) for x, y in split.centres]
    if not fixed_nodes:
        decisions.append(
            Decision(
                stage="solve",
                tool="set_pop",
                reason=f"{sum(split.units)} units are more than one POP serves ({r.pops.max_units} units), so the area gets "
                f"{len(nodes)} POPs, placed by a weighted k-means over the homes and snapped to the street.",
                values={"pops": len(nodes), "max_units": r.pops.max_units},
                source="rulebook",
            )
        )
    decisions.append(
        Decision(
            stage="solve",
            tool="set_pop",
            reason="Each home belongs to one POP: a min-cost flow keeps every POP within "
            f"{max(r.pops.max_units, -(-sum(split.units) // len(nodes)))} units and homes as close to their POP "
            "as the capacities allow (straight-line distance).",
            values={"units": split.units},
            source="solver",
        )
    )
    s.design.remove_where(kind="pop")
    ids: list[str] = []
    for i, node in enumerate(nodes):
        pid = keep_ids[i] if i < len(keep_ids) and keep_ids[i] else s.uid("odf", f"pop:{i + 1}")
        old = next((p for p in pinned if p.element_uuid == pid), None)
        s.design.add(
            Feature(
                element_uuid=pid,
                element_type="odf",
                placement="indoor",
                geometry=_wgs(twin, twin.node_point(node)),
                props={"name": f"POP-{i + 1}", "site_name": f"POP-{i + 1}"},
                openftth={
                    **(old.openftth if old else {}),
                    "kind": "pop",
                    "node": int(node),
                    "units": int(split.units[i]),
                    "homes": int(sum(1 for o in split.owner if o == i)),
                },
            )
        )
        ids.append(pid)
    for h, o in zip(homes, split.owner, strict=False):
        h.openftth["pop_id"] = ids[o]
    s.cache["pop_node"] = int(nodes[0])
    return _commit(
        s,
        ToolResult(
            tool="set_pop",
            summary=f"{len(nodes)} POPs: " + ", ".join(f"{u} units" for u in split.units),
            decisions=decisions,
            data={"pops": len(nodes), "nodes": [int(n) for n in nodes], "units": split.units},
        ),
        before,
    )


def _set_single_pop(s: Session, lon: float | None = None, lat: float | None = None) -> ToolResult:
    s.project.set_stage("solve")
    twin = _twin(s)
    pinned = _pinned(s, "pop")
    if pinned:
        # The engineer placed the POP; only their own move_site moves it again.
        pop = pinned[0]
        node = int(pop.openftth["node"])
        s.cache["pop_node"] = node
        return _commit(
            s,
            ToolResult(
                tool="set_pop",
                summary=f"POP kept at street node {node} (placed by the engineer)",
                decisions=[_pin_decision("set_pop", pinned, "routes")],
                data={"node": node, "pinned": True},
            ),
            s.design.copy(),
        )
    before = s.design.copy()
    decisions: list[Decision] = []
    if lon is None or lat is None:
        area = s.cache.get("area") or s.design.by_kind("service_area")[0].geometry
        c = gpd.GeoSeries([area.centroid], crs=4326).to_crs(twin.crs).iloc[0]
        node, _ = _nearest_allowed(twin, c.x, c.y, _keep_out(s, twin)[0])
        decisions.append(
            Decision(
                stage="solve",
                tool="set_pop",
                reason="No POP given; placed at the street node nearest the area centroid.",
                source="assumption",
            )
        )
    else:
        p = gpd.GeoSeries([Point(lon, lat)], crs=4326).to_crs(twin.crs).iloc[0]
        node, dist = twin.nearest_node(p.x, p.y)
        decisions.append(
            Decision(
                stage="solve",
                tool="set_pop",
                reason=f"POP snapped {dist:.0f} m to the nearest street node.",
                values={"snap_m": round(dist, 1)},
                source="user",
            )
        )
    s.design.remove_where(kind="pop")
    s.design.add(
        Feature(
            element_uuid=s.uid("odf", "pop"),
            element_type="odf",
            placement="indoor",
            geometry=_wgs(twin, twin.node_point(node)),
            props={"name": "POP", "site_name": "POP"},
            openftth={"kind": "pop", "node": int(node)},
        )
    )
    s.cache["pop_node"] = int(node)
    return _commit(
        s,
        ToolResult(
            tool="set_pop",
            summary=f"POP at street node {node}",
            decisions=decisions,
            data={"node": int(node)},
        ),
        before,
    )


def cluster_homes(s: Session, time_limit_s: float = WORK_LIMIT) -> ToolResult:
    """Place FATs: capacitated clustering of homes within max_drop_m street distance."""
    s.project.set_stage("solve")
    twin = _twin(s)
    r = s.rules
    before = s.design.copy()
    homes = s.design.by_kind("home")
    cap = r.fat.max_homes
    pinned = _pinned(s, "fat")
    pinned_at = {int(f.openftth["node"]): f for f in pinned}
    pinned_ids = {f.element_uuid for f in pinned}
    s.design.remove_where(kind="fat")
    fat_no = 0

    def new_fat(
        node: int, units: int, key: str, placement: str, geometry, fid: str | None = None
    ) -> str:
        nonlocal fat_no
        fat_no += 1
        if fid is None:
            fid = s.uid("tb", key)
            # A pinned FAT keeps the id it got at its old node; a new FAT there needs another.
            if fid in pinned_ids:
                fid = s.uid("tb", f"{key}:after-pin")
        s.design.add(
            Feature(
                element_uuid=fid,
                element_type="tb",
                placement=placement,
                geometry=geometry,
                props={
                    "name": f"FAT-{fat_no:03d}",
                    "port_capacity": cap,
                    "total_connections": units,
                    "reserve_capacity": cap - units,
                },
                openftth={
                    "kind": "fat",
                    "node": int(node),
                    "units": units,
                    "mdu": placement == "indoor",
                },
            )
        )
        return fid

    # Large MDUs (more units than one FAT) get dedicated indoor FATs in the building.
    mdu_homes = [h for h in homes if int(h.openftth["units"]) > cap]
    n_mdu_fats = 0
    for h in mdu_homes:
        remaining = int(h.openftth["units"])
        ids = []
        k = 0
        while remaining > 0:
            chunk = min(cap, remaining)
            ids.append(
                new_fat(
                    int(h.openftth["node"]),
                    chunk,
                    f"fat:mdu:{h.element_uuid}:{k}",
                    "indoor",
                    h.geometry,
                )
            )
            remaining -= chunk
            k += 1
        n_mdu_fats += len(ids)
        h.openftth["fat_id"] = ids[0]
        h.openftth["fat_ids"] = ids
        h.openftth["mdu"] = True
        h.openftth["drop_m"] = 0.0

    regular = [h for h in homes if int(h.openftth["units"]) <= cap]
    # FATs stand at intersections or in front of a home (wall/pole), not at arbitrary vertices.
    blocked, _zones_m = _keep_out(s, twin)
    fat_candidates = (
        {n for n, deg in twin.graph.degree if deg >= 3}
        | {int(h.openftth["node"]) for h in regular}
        | set(pinned_at)
    ) - blocked
    demands = [
        Demand(
            key=h.element_uuid,
            node=int(h.openftth["node"]),
            units=int(h.openftth["units"]),
            offset_m=float(h.openftth["offset_m"]),
        )
        for h in regular
    ]
    # Only the along-street part of a drop depends on the FAT choice, and its trench is shared
    # with the distribution cable, so distance is priced as drop cable only.
    per_m = r.units.drop_per_m
    sites, stats = capacitated_sites(
        _streets_only(twin),
        twin.xy,
        demands,
        capacity=cap,
        max_dist_m=r.distances.max_drop_m,
        candidate_nodes=fat_candidates,
        min_units=r.fat.min_homes,
        short_unit_cost=per_m * r.distances.max_drop_m,
        open_cost=r.units.fat + r.labour.fat_install,
        per_m_cost=per_m,
        time_limit_s=time_limit_s,
        fixed_nodes=set(pinned_at),
    )
    home_by_id = {h.element_uuid: h for h in regular}
    for site in sites:
        keep = pinned_at.get(site.node) if site.slot == 0 else None
        fid = new_fat(
            site.node,
            site.units,
            f"fat:{site.node}:{site.slot}",
            "outdoor",
            _wgs(twin, twin.node_point(site.node)),
            keep.element_uuid if keep else None,
        )
        if keep is not None:
            s.design.get(fid).openftth.update(
                placed_by="engineer", placed_reason=keep.openftth.get("placed_reason", "")
            )
        s.design.get(fid).openftth["homes"] = len(site.members)
        for m in site.members:
            h = home_by_id[m.key]
            h.openftth["fat_id"] = fid
            h.openftth["drop_m"] = round(site.distances_m[m.key], 1)
    units = sum(int(h.openftth["units"]) for h in homes)
    decisions = [
        Decision(
            stage="solve",
            tool="cluster_homes",
            reason=f"FATs placed by capacitated clustering (CP-SAT, {stats.cells} cell(s), status {','.join(stats.status)}): each home within {r.distances.max_drop_m:g} m street distance of its FAT, ≤ {cap} units per FAT, minimising FAT count × ({r.units.fat + r.labour.fat_install:g}) + drop metres × {per_m:.2f}.",
            values={
                "fats": len(sites),
                "units": units,
                "candidates": stats.candidates,
                "objective": None if math.isnan(stats.objective) else round(stats.objective, 2),
            },
        ),
        Decision(
            stage="solve",
            tool="cluster_homes",
            reason=f"Drop distance priced as drop cable only (the street trench is shared with the distribution cable). FATs with fewer than fat.min_homes ({r.fat.min_homes}) units pay one max-length drop per missing unit, which discourages many tiny FATs.",
            values={"per_m": round(per_m, 2)},
            source="assumption",
        ),
    ]
    if mdu_homes:
        decisions.append(
            Decision(
                stage="solve",
                tool="cluster_homes",
                reason=f"{len(mdu_homes)} building(s) have more units than one FAT ({cap}); each gets dedicated indoor FATs ({n_mdu_fats} in total) instead of street FATs.",
                values={"mdu_buildings": len(mdu_homes), "mdu_fats": n_mdu_fats},
                source="assumption",
            )
        )
    if pinned:
        decisions.append(_pin_decision("cluster_homes", pinned, "other FATs"))
    warnings = (
        [f"{stats.fallback_cells} cell(s) hit the time limit; greedy fallback used"]
        if stats.fallback_cells
        else []
    )
    warnings += [
        f"{fat.props['name']} was placed by the engineer and now serves no home"
        for fat in s.design.by_kind("fat")
        if fat.openftth.get("placed_by") == "engineer" and not fat.openftth.get("homes")
    ]
    total_fats = len(sites) + n_mdu_fats
    return _commit(
        s,
        ToolResult(
            tool="cluster_homes",
            summary=f"{total_fats} FATs ({n_mdu_fats} indoor MDU) for {len(homes)} homes ({units} units), avg {units / max(1, total_fats):.1f} units/FAT",
            decisions=decisions,
            warnings=warnings,
            data={"fats": total_fats},
        ),
        before,
    )


def _pops(s: Session) -> list[Feature]:
    """The POPs, in name order (POP, or POP-1, POP-2, …)."""
    return sorted(s.design.by_kind("pop"), key=lambda p: str(p.props.get("name", "")))


def _settle_fat_pops(s: Session) -> None:
    """With several POPs, a FAT belongs to the POP that most of its units belong to, and its
    homes follow it: one FAT is never fed from two POPs."""
    pops = _pops(s)
    if len(pops) < 2:
        for f in s.design.by_kind("fat"):
            f.openftth.pop("pop_id", None)
        return
    homes = s.design.by_kind("home")
    for fat in sorted(s.design.by_kind("fat"), key=lambda f: f.element_uuid):
        mine = [
            h
            for h in homes
            if fat.element_uuid in (h.openftth.get("fat_ids") or [h.openftth.get("fat_id")])
        ]
        votes: dict[str, int] = {}
        for h in mine:
            pid = h.openftth.get("pop_id")
            if pid:
                votes[pid] = votes.get(pid, 0) + int(h.openftth.get("units", 1))
        if not votes:
            continue
        pid = max(sorted(votes), key=lambda k: votes[k])
        fat.openftth["pop_id"] = pid
        for h in mine:
            h.openftth["pop_id"] = pid


def _feeder_tree(s: Session, fdh: Feature | None) -> RoutedTree | None:
    """The feeder tree that feeds `fdh`: its POP's, or the only one."""
    pid = fdh.openftth.get("pop_id") if fdh else None
    return s.trees.get(f"feeder:{pid}") if pid else s.trees.get("feeder")


def place_fdh(s: Session, time_limit_s: float = WORK_LIMIT) -> ToolResult:
    """Place FDHs (splitter cabinets) over the FATs, capacity in homes served."""
    s.project.set_stage("solve")
    twin = _twin(s)
    r = s.rules
    before = s.design.copy()
    _settle_fat_pops(s)
    fats = s.design.by_kind("fat")
    pinned = _pinned(s, "fdh")
    pinned_at = {int(f.openftth["node"]): f for f in pinned}
    demands = [
        Demand(key=f.element_uuid, node=int(f.openftth["node"]), units=int(f.openftth["units"]))
        for f in fats
    ]
    cap_max = max(r.fdh.capacities)
    capacity = int(cap_max * r.fdh.max_fill_pct / 100.0)
    # Cabinets sit at intersections: candidate nodes with degree ≥ 3 (fallback: all nodes).
    blocked, _zones_m = _keep_out(s, twin)
    candidates = (
        ({n for n, d in twin.graph.degree if d >= 3} or set(twin.graph.nodes)) | set(pinned_at)
    ) - blocked
    # Distance is priced as distribution cable only: the trench along the street is needed for
    # the drops anyway, so it must not push the solver towards many small cabinets.
    per_m = r.units.cable_base_per_m + 24 * r.units.cable_per_fiber_m
    # With several POPs each catchment gets its own cabinets, so no cabinet straddles two.
    pops = _pops(s)
    fat_pop = {f.element_uuid: f.openftth.get("pop_id") for f in fats}
    if len(pops) > 1:
        pop_xy = {p.element_uuid: twin.xy[int(p.openftth["node"])] for p in pops}

        def nearest_pop(node: int) -> str:
            return min(sorted(pop_xy), key=lambda k: float(np.hypot(*(pop_xy[k] - twin.xy[node]))))

        groups = [
            (p.element_uuid, [d for d in demands if fat_pop[d.key] == p.element_uuid]) for p in pops
        ]
        pin_pop = {n: nearest_pop(n) for n in pinned_at}
    else:
        groups = [(None, demands)]
        pin_pop = {n: None for n in pinned_at}
    # Cabinets serve FATs along streets only: a bore is the feeder's to take, where routing
    # weighs its price; reach measured through one would not match the distribution route.
    streets = _streets_only(twin)
    sites = []
    site_pop: dict[int, str | None] = {}
    statuses: list[str] = []
    moved = stranded = 0
    for pid, group in groups:
        pins = {n for n, p in pin_pop.items() if p == pid}
        g_sites, g_stats = capacitated_sites(
            streets,
            twin.xy,
            group,
            capacity=capacity,
            max_dist_m=r.distances.max_dist_m * FDH_REACH_FACTOR,
            open_cost=r.units.fdh_288 + r.labour.fdh_install,
            per_m_cost=per_m,
            candidate_nodes=candidates,
            time_limit_s=time_limit_s,
            k_nearest=1000,
            fixed_nodes=pins,
        )
        # The clustering prices distance to the nearest candidate, so a FAT can end up on a
        # cabinet within reach as the crow flies but not along the street: check and move those.
        g_sites, g_moved, g_stranded = repair_reach(
            streets, g_sites, r.distances.max_dist_m, capacity, keep_nodes=pins
        )
        for site in g_sites:
            site_pop[id(site)] = pid
        sites += g_sites
        statuses += g_stats.status
        moved += g_moved
        stranded += g_stranded
    s.design.remove_where(kind="fdh")
    fat_by_id = {f.element_uuid: f for f in fats}
    for i, site in enumerate(sites):
        chosen = next(
            (c for c in sorted(r.fdh.capacities) if site.units <= c * r.fdh.max_fill_pct / 100.0),
            cap_max,
        )
        keep = pinned_at.get(site.node) if site.slot == 0 else None
        if keep is not None:
            fid = keep.element_uuid
        else:
            fid = s.uid("otb", f"fdh:{site.node}")
            # A pinned cabinet keeps the id it got at its old node; a new one there needs another.
            if fid in {f.element_uuid for f in pinned}:
                fid = s.uid("otb", f"fdh:{site.node}:after-pin")
        s.design.add(
            Feature(
                element_uuid=fid,
                element_type="otb",
                placement="outdoor",
                geometry=_wgs(twin, twin.node_point(site.node)),
                props={
                    "name": f"FDH-{i + 1:02d}",
                    "port_capacity": chosen,
                    "total_connections": site.units,
                    "reserve_capacity": chosen - site.units,
                },
                openftth={
                    "kind": "fdh",
                    "node": int(site.node),
                    "units": site.units,
                    "capacity": chosen,
                    "fats": len(site.members),
                    **({"pop_id": site_pop[id(site)]} if site_pop.get(id(site)) else {}),
                    **(
                        {
                            "placed_by": "engineer",
                            "placed_reason": keep.openftth.get("placed_reason", ""),
                        }
                        if keep is not None
                        else {}
                    ),
                },
            )
        )
        for m in site.members:
            fat_by_id[m.key].openftth["fdh_id"] = fid
            fat_by_id[m.key].openftth["distribution_est_m"] = round(site.distances_m[m.key], 1)
    for h in s.design.by_kind("home"):
        fat = h.openftth.get("fat_id")
        if fat and fat in fat_by_id:
            h.openftth["fdh_id"] = fat_by_id[fat].openftth.get("fdh_id")
    decisions = [
        Decision(
            stage="solve",
            tool="place_fdh",
            reason=f"FDHs placed by capacitated clustering of FATs (CP-SAT, status {','.join(statuses)}) at street intersections, ≤ {capacity} units per cabinet ({r.fdh.max_fill_pct:g}% of {cap_max}), each FAT within {r.distances.max_dist_m:g} m; cabinet size then shrunk to the smallest that fits.",
            values={
                "fdhs": len(sites),
                "capacity_used": capacity,
                "fats_moved": moved,
                "fats_stranded": stranded,
            },
        ),
        Decision(
            stage="solve",
            tool="place_fdh",
            reason="Distribution distance priced as a 24F cable only; the street trench is needed for the drops regardless of where cabinets stand.",
            values={"per_m": round(per_m, 2)},
            source="assumption",
        ),
        Decision(
            stage="solve",
            tool="place_fdh",
            reason=f"FATs were clustered within {FDH_REACH_FACTOR:.0%} of max_dist_m ({r.distances.max_dist_m:g} m) so that the routed tree path, which is longer than the shortest path, still fits.",
            values={"reach_m": round(r.distances.max_dist_m * FDH_REACH_FACTOR, 1)},
            source="assumption",
        ),
    ]
    if pinned:
        decisions.append(_pin_decision("place_fdh", pinned, "other cabinets"))
    idle = [
        str(f.props["name"])
        for f in s.design.by_kind("fdh")
        if f.openftth.get("placed_by") == "engineer" and not f.openftth.get("units")
    ]
    return _commit(
        s,
        ToolResult(
            tool="place_fdh",
            summary=f"{len(sites)} FDH(s): " + ", ".join(f"{x.units} units" for x in sites),
            decisions=decisions,
            warnings=[f"{n} was placed by the engineer and now serves no FAT" for n in idle],
            data={"fdhs": len(sites)},
        ),
        before,
    )


def _extra_splices(r: Rulebook) -> int:
    """Splices a path has beyond FAT, FDH in and FDH out: the FAT splitter's in a cascade."""
    return 1 if r.pon.arch() == "cascaded" else 0


def _fibres_of(r: Rulebook, fat: Feature) -> int:
    """Distribution fibres a FAT needs: one per FAT splitter when the split is cascaded, one per
    unit otherwise."""
    if r.pon.arch() == "cascaded":
        return int(fat.openftth.get("splitters") or r.fat_splitters(int(fat.openftth["units"])))
    return int(fat.openftth["units"])


def assign_pon(s: Session) -> ToolResult:
    """Group the units under each FDH into PONs: whole FATs into 1:N splitters (centralised),
    FAT splitters under first-stage splitters (cascaded), or no PONs at all (point-to-point)."""
    s.project.set_stage("solve")
    before = s.design.copy()
    r = s.rules
    arch = r.pon.arch()
    homes = s.design.by_kind("home")
    fats = {f.element_uuid: f for f in s.design.by_kind("fat")}
    stages = r.cascade_stages()
    n_pon = 0
    n_split = 0
    n_units = 0
    for fdh in s.design.by_kind("fdh"):
        my_fats = sorted(
            (f for f in fats.values() if f.openftth.get("fdh_id") == fdh.element_uuid),
            key=lambda f: f.openftth.get("distribution_est_m", 0.0),
        )
        if arch == "p2p":
            units = sum(int(f.openftth["units"]) for f in my_fats)
            for fat in my_fats:
                fat.openftth.pop("pon_id", None)
                fat.openftth.pop("splitters", None)
            for h in homes:
                if h.openftth.get("fdh_id") == fdh.element_uuid:
                    h.openftth.pop("pon_id", None)
            # One feeder fibre per unit: the FDH is a patch point, not a splitter cabinet.
            fdh.openftth["pons"] = units
            fdh.openftth["p2p"] = True
            n_units += units
            continue
        cap = stages[0] if arch == "cascaded" else r.pon.split_ratio
        pon_idx = 0
        load = 0
        pon_id = f"{fdh.props['name']}-PON-{pon_idx + 1:02d}"
        for fat in my_fats:
            fat_homes = [
                h
                for h in homes
                if fat.element_uuid in (h.openftth.get("fat_ids") or [h.openftth.get("fat_id")])
            ]
            units = int(fat.openftth["units"])
            need = r.fat_splitters(units) if arch == "cascaded" else units
            if load and load + need > cap:
                pon_idx += 1
                load = 0
                pon_id = f"{fdh.props['name']}-PON-{pon_idx + 1:02d}"
            fat.openftth["pon_id"] = pon_id
            if arch == "cascaded":
                fat.openftth["splitters"] = need
                n_split += need
            else:
                fat.openftth.pop("splitters", None)
            for h in fat_homes:
                h.openftth["pon_id"] = pon_id
            load += need
        fdh.openftth["pons"] = pon_idx + 1 if my_fats else 0
        fdh.openftth.pop("p2p", None)
        n_pon += fdh.openftth["pons"]
    if arch == "p2p":
        reason = (
            "Point-to-point: no splitters; every unit gets a dedicated fibre from the POP, patched "
            "through its FDH, so the feeder carries one fibre per unit."
        )
        summary = f"point-to-point: {n_units} dedicated fibres, no splitters"
        values: dict[str, Any] = {"pons": 0, "dedicated_fibres": n_units}
    elif arch == "cascaded":
        reason = (
            f"Cascaded split: each FAT gets 1:{stages[1]} splitters for its units, and whole FATs' "
            f"splitters are packed under 1:{stages[0]} splitters in the FDH in order of distance "
            f"(one PON per FDH splitter, 1:{r.pon.split_ratio} in all); the distribution cable "
            "carries one fibre per FAT splitter."
        )
        summary = f"{n_pon} PONs (1:{stages[0]} in the FDH × 1:{stages[1]} in the FAT, {n_split} FAT splitters)"
        values = {"pons": n_pon, "fat_splitters": n_split}
    else:
        reason = f"Centralised split: whole FATs are packed into 1:{r.pon.split_ratio} PONs per FDH in order of distance, never splitting a FAT across two splitters (one 1:{r.pon.split_ratio} splitter per PON in the FDH)."
        summary = f"{n_pon} PONs (1:{r.pon.split_ratio})"
        values = {"pons": n_pon}
    decisions = [
        Decision(stage="solve", tool="assign_pon", reason=reason, values=values, source="rulebook")
    ]
    return _commit(
        s,
        ToolResult(tool="assign_pon", summary=summary, decisions=decisions, data=values),
        before,
    )


def route_feeder(s: Session) -> ToolResult:
    s.project.set_stage("solve")
    twin = _twin(s)
    r = s.rules
    before = s.design.copy()
    pops = _pops(s)
    fdhs = s.design.by_kind("fdh")
    multi = len(pops) > 1
    for key in [k for k in s.trees if k.startswith("feeder")]:
        del s.trees[key]
    drawn, taken = _take_drawn(s, "feeder")
    _clear_solved(s, "feeder")
    graph, weight = _favouring(twin.graph, drawn)
    n = 0
    length = 0.0
    unreachable = 0
    per_pop: list[str] = []
    for pop in pops:
        mine = [f for f in fdhs if not multi or f.openftth.get("pop_id") == pop.element_uuid]
        terminals: dict[int, int] = {}
        for f in mine:
            node = int(f.openftth["node"])
            terminals[node] = terminals.get(node, 0) + int(f.openftth.get("pons", 1))
        tree = route_tree(graph, int(pop.openftth["node"]), terminals, weight=weight)
        s.trees[f"feeder:{pop.element_uuid}" if multi else "feeder"] = tree
        n += _emit_cable_sections(
            s,
            tree,
            "feeder",
            r.fibers.feeder,
            prefix=f"{pop.props['name']}-FDR" if multi else "FDR",
            pop_id=pop.element_uuid if multi else None,
        )
        for f in mine:
            f.openftth["feeder_path_m"] = round(
                _path_length(twin, tree, int(f.openftth["node"])), 1
            )
        length += tree.total_length_m()
        unreachable += len(tree.unreachable)
        per_pop.append(
            f"{pop.props['name']} {tree.total_length_m():.0f} m to {len(terminals)} FDH(s)"
        )
    if pops:
        s.cache["pop_node"] = int(pops[0].openftth["node"])
    drawn_warnings = _settle_drawn(s, "feeder", drawn, taken)
    crossing_decisions = _sync_crossings(s, "route_feeder")
    decisions = [
        Decision(
            stage="solve",
            tool="route_feeder",
            reason="Feeder routed POP → FDHs as a min-cost Steiner tree over the street graph (trench cost per metre by surface); fibre count per section = downstream PONs + spare, rounded up the feeder ladder."
            + (" Each POP feeds only the cabinets in its own catchment." if multi else "")
            + (
                " The tree follows the feeder the engineer drew wherever it leads." if drawn else ""
            ),
            values={
                "sections": n,
                "length_m": round(length, 1),
                "unreachable_fdhs": unreachable,
                **({"pops": len(pops)} if multi else {}),
            },
        )
    ] + crossing_decisions
    terminals_total = len({int(f.openftth["node"]) for f in fdhs})
    summary = (
        f"feeder {length:.0f} m in {n} sections: " + "; ".join(per_pop)
        if multi
        else f"feeder {length:.0f} m in {n} sections to {terminals_total} FDH(s)"
    )
    return _commit(
        s,
        ToolResult(
            tool="route_feeder",
            summary=summary,
            decisions=decisions,
            warnings=([f"{unreachable} FDH(s) unreachable from their POP"] if unreachable else [])
            + drawn_warnings,
            data={"length_m": round(length, 1)},
        ),
        before,
    )


def route_distribution(s: Session) -> ToolResult:
    s.project.set_stage("solve")
    twin = _twin(s)
    r = s.rules
    before = s.design.copy()
    drawn, taken = _take_drawn(s, "distribution")
    _clear_solved(s, "distribution")
    graph, weight = _favouring(twin.graph, drawn)
    total = 0.0
    n = 0
    grafted = 0
    homes_by_fat: dict[str, list[Feature]] = {}
    for h in s.design.by_kind("home"):
        homes_by_fat.setdefault(h.openftth.get("fat_id", ""), []).append(h)
    for fdh in s.design.by_kind("fdh"):
        fats = [f for f in s.design.by_kind("fat") if f.openftth.get("fdh_id") == fdh.element_uuid]
        # Several FATs can share a node (an apartment building's dedicated FATs all sit at
        # its street node): the section upstream must carry all of them.
        terminals: dict[int, int] = {}
        for f in fats:
            node = int(f.openftth["node"])
            terminals[node] = terminals.get(node, 0) + _fibres_of(r, f)
        tree = route_tree(
            graph,
            int(fdh.openftth["node"]),
            terminals,
            weight=weight,
            max_path_m=r.distances.max_dist_m,
        )
        grafted += len(tree.grafted)
        s.trees[fdh.element_uuid] = tree
        n += _emit_cable_sections(
            s,
            tree,
            "distribution",
            r.fibers.distribution,
            prefix=f"{fdh.props['name']}-DST",
            fdh_id=fdh.element_uuid,
        )
        total += tree.total_length_m()
        for f in fats:
            dist_m = _path_length(twin, tree, int(f.openftth["node"]))
            f.openftth["distribution_path_m"] = round(dist_m, 1)
            for h in homes_by_fat.get(f.element_uuid, []):
                h.openftth["distribution_path_m"] = round(dist_m, 1)
    decisions = [
        Decision(
            stage="solve",
            tool="route_distribution",
            reason="Per FDH, distribution routed FDH → FATs as a min-cost Steiner tree; fibre count per section = downstream units (one fibre per home in a centralised-split design) + spare, rounded up the distribution ladder."
            + (
                " The trees follow the distribution cables the engineer drew wherever they lead."
                if drawn
                else ""
            ),
            values={"sections": n, "length_m": round(total, 1)},
        )
    ]
    drawn_warnings = _settle_drawn(s, "distribution", drawn, taken)
    if grafted:
        decisions.append(
            Decision(
                stage="solve",
                tool="route_distribution",
                reason=f"{grafted} FAT(s) were further than max_dist_m ({r.distances.max_dist_m:g} m) along the "
                "cheapest tree, though within it along the street; their cable follows the shortest street "
                "path from the FDH instead, at some extra trench cost.",
                values={"grafted": grafted},
                source="solver",
            )
        )
    decisions += _sync_crossings(s, "route_distribution")
    return _commit(
        s,
        ToolResult(
            tool="route_distribution",
            summary=f"distribution {total:.0f} m in {n} sections",
            decisions=decisions,
            warnings=drawn_warnings,
            data={"length_m": round(total, 1)},
        ),
        before,
    )


def _streets_only(twin: Twin) -> nx.Graph:
    """The route graph without bores: a home reaches its FAT along streets, never under a
    motorway, railway or river. Only distribution and feeder cables are worth a bore."""
    return nx.subgraph_view(
        twin.graph, filter_edge=lambda u, v: not twin.graph[u][v].get("crossing")
    )


def _drop_path(twin: Twin, home_node: int, fat_node: int) -> list[int]:
    """A drop's street path, along streets only when there is one."""
    if home_node == fat_node:
        return [home_node]
    try:
        return nx.shortest_path(_streets_only(twin), home_node, fat_node, weight="length_m")
    except nx.NetworkXNoPath:
        return nx.shortest_path(twin.graph, home_node, fat_node, weight="length_m")


BARRIER_LABEL = {
    "motorway": "the motorway",
    "railway": "the railway",
    "water": "the river or canal",
    "unmapped": "an obstacle the map does not show",
}


def _sync_crossings(s: Session, tool: str) -> list[Decision]:
    """Put a `crossing` route element on every barrier crossing the cables use, and say for each
    new one what it costs against going round. Returns the decisions for crossings `tool` added."""
    twin = _twin(s)
    used: dict[tuple[int, int], str] = {}
    for c in s.design.by_type("cable.underground"):
        if c.openftth.get("kind") == "drop":
            continue
        for edge, (surface, _metres) in c.openftth.get("edges", {}).items():
            if str(surface).startswith("crossing:"):
                a, b = (int(x) for x in edge.split("-"))
                used[(a, b)] = str(surface).split(":", 1)[1]
    before = {f.openftth.get("edge") for f in s.design.by_kind("crossing")}
    s.design.remove_where(kind="crossing")
    plain = nx.subgraph_view(
        twin.graph, filter_edge=lambda u, v: not twin.graph[u][v].get("crossing")
    )
    decisions: list[Decision] = []
    for (a, b), barrier in sorted(used.items()):
        if not twin.graph.has_edge(a, b):
            continue
        d = twin.graph[a][b]
        key = f"{a}-{b}"
        cost = float(d["cost"])
        try:
            detour_cost, path = nx.single_source_dijkstra(plain, a, b, weight="cost")
            detour_m = sum(
                plain[u][v]["length_m"] for u, v in zip(path[:-1], path[1:], strict=False)
            )
        except nx.NetworkXNoPath:
            detour_cost, detour_m = math.inf, math.inf
        label = BARRIER_LABEL.get(barrier, barrier)
        s.design.add(
            Feature(
                element_uuid=s.uid("route", f"crossing:{key}"),
                element_type="route",
                geometry=_wgs(twin, twin.path_line([a, b])),
                props={
                    "name": f"crossing under {label}",
                    "method": "directional drilling",
                    "length_m": round(float(d["length_m"]), 1),
                    "status": "planned",
                },
                openftth={
                    "kind": "crossing",
                    "barrier": barrier,
                    "edge": key,
                    "cost": round(cost, 2),
                    **({} if math.isinf(detour_m) else {"detour_m": round(detour_m, 1)}),
                    **({} if math.isinf(detour_cost) else {"detour_cost": round(detour_cost, 2)}),
                },
            )
        )
        if key in before:
            continue
        price = getattr(s.rules.crossings, barrier)
        cur = s.rules.currency
        go_round = (
            "there is no way round on the streets"
            if math.isinf(detour_cost)
            else f"going round would be {detour_m:,.0f} m of trench costing {detour_cost:,.0f} {cur}"
        )
        decisions.append(
            Decision(
                stage="solve",
                tool=tool,
                reason=f"Cables cross under {label} with a {d['length_m']:.0f} m directional drill "
                f"costing {cost:,.0f} {cur} ({price.fixed:,.0f} for the permit and pits + "
                f"{price.per_m:,.0f} per metre); {go_round}.",
                values={
                    "barrier": barrier,
                    "length_m": round(float(d["length_m"]), 1),
                    "cost": round(cost, 2),
                    "detour_m": None if math.isinf(detour_m) else round(detour_m, 1),
                    "detour_cost": None if math.isinf(detour_cost) else round(detour_cost, 2),
                },
                source="solver",
            )
        )
    return decisions


def _recover_home_links(s: Session) -> int:
    """Give homes their FAT links back when an earlier twin rebuild dropped them: a regular
    home from the drop that still names it, an apartment building from the dedicated indoor
    FATs standing at its street node. Returns how many homes were repaired."""
    homes = [h for h in s.design.by_kind("home") if "fat_id" not in h.openftth]
    fats = s.design.by_kind("fat")
    if not homes or not fats:
        return 0
    fixed = 0
    for d in s.design.by_kind("drop"):
        hid = d.openftth.get("home")
        if hid in s.design and d.openftth.get("fat_id") in s.design:
            h = s.design.get(hid)
            if "fat_id" not in h.openftth:
                h.openftth["fat_id"] = d.openftth["fat_id"]
                fixed += 1
    mdu_at: dict[int, list[str]] = {}
    for f in fats:
        if f.openftth.get("mdu"):
            mdu_at.setdefault(int(f.openftth["node"]), []).append(f.element_uuid)
    for h in homes:
        ids = mdu_at.get(int(h.openftth["node"]))
        if "fat_id" not in h.openftth and ids:
            h.openftth.update({"fat_id": ids[0], "fat_ids": ids, "mdu": True, "drop_m": 0.0})
            fixed += 1
    return fixed


def route_drops(s: Session) -> ToolResult:
    s.project.set_stage("solve")
    twin = _twin(s)
    r = s.rules
    before = s.design.copy()
    repaired = _recover_home_links(s)
    _clear_solved(s, "drop")
    fats = {f.element_uuid: f for f in s.design.by_kind("fat")}
    buildings = {b.element_uuid: b for b in s.design.by_kind("building")}
    total = 0.0
    n = 0
    too_long = 0
    risers = 0
    for h in s.design.by_kind("home"):
        fat = fats.get(h.openftth.get("fat_id", ""))
        if fat is None:
            continue
        # A building with several flats: its riser is part of every flat's path, with one more
        # splice in the floor box.
        floors, riser_m = _riser(s, h, buildings)
        if riser_m:
            h.openftth["floors"], h.openftth["riser_m"] = floors, riser_m
            risers += 1
        else:
            h.openftth.pop("floors", None)
            h.openftth.pop("riser_m", None)
        floor_splice = 1 if riser_m else 0
        if h.openftth.get("mdu"):
            # Dedicated indoor FATs: no street drop; the lead-in is part of the distribution path.
            fdh = next(
                (
                    x
                    for x in s.design.by_kind("fdh")
                    if x.element_uuid == fat.openftth.get("fdh_id")
                ),
                None,
            )
            dist_m = float(fat.openftth.get("distribution_path_m", 0.0)) + float(
                h.openftth.get("offset_m", 0.0)
            )
            feed_m = float(fdh.openftth.get("feeder_path_m", 0.0)) if fdh else 0.0
            h.openftth["path_to_pop_m"] = round(dist_m + feed_m + riser_m, 1)
            tree = s.trees.get(fat.openftth.get("fdh_id", ""))
            closures = _closures_on_path(tree, int(fat.openftth["node"])) if tree else 0
            feeder_tree = _feeder_tree(s, fdh)
            closures += (
                _closures_on_path(feeder_tree, int(fdh.openftth["node"]))
                if (feeder_tree and fdh)
                else 0
            )
            h.openftth["splices_on_path"] = 3 + closures + _extra_splices(s.rules) + floor_splice
            continue
        hn, fn = int(h.openftth["node"]), int(fat.openftth["node"])
        path = _drop_path(twin, hn, fn)
        home_xy = gpd.GeoSeries([h.geometry], crs=4326).to_crs(twin.crs).iloc[0]
        coords = [(home_xy.x, home_xy.y)] + [tuple(twin.xy[p]) for p in path]
        line = LineString(coords) if len(coords) > 1 else LineString([coords[0], coords[0]])
        length = float(line.length)
        total += length
        n += 1
        if length > r.distances.max_drop_m:
            too_long += 1
        h.openftth["drop_m"] = round(length, 1)
        # Path to POP for the power budget: drop + distribution path + feeder path.
        fdh = next(
            (x for x in s.design.by_kind("fdh") if x.element_uuid == fat.openftth.get("fdh_id")),
            None,
        )
        dist_m = float(fat.openftth.get("distribution_path_m", 0.0))
        feed_m = float(fdh.openftth.get("feeder_path_m", 0.0)) if fdh else 0.0
        h.openftth["path_to_pop_m"] = round(length + dist_m + feed_m + riser_m, 1)
        # Splices on the path: FAT, FDH (×2: distribution in, feeder in) + one per closure passed.
        tree = s.trees.get(fat.openftth.get("fdh_id", ""))
        closures = _closures_on_path(tree, fn) if tree else 0
        feeder_tree = _feeder_tree(s, fdh)
        closures += (
            _closures_on_path(feeder_tree, int(fdh.openftth["node"]))
            if (feeder_tree and fdh)
            else 0
        )
        h.openftth["splices_on_path"] = 3 + closures + _extra_splices(s.rules) + floor_splice
        s.design.add(
            Feature(
                element_uuid=s.uid("cable.underground", f"drop:{h.element_uuid}"),
                element_type="cable.underground",
                geometry=_wgs(twin, line),
                props={
                    "cable_type": "drop",
                    "fiber_count": r.fibers.drop,
                    "from_label": fat.props["name"],
                    "to_label": h.props["name"],
                    "length_m": round(length, 1),
                    "total_length_m": round(length, 1),
                },
                openftth={
                    "kind": "drop",
                    "home": h.element_uuid,
                    "fat_id": fat.element_uuid,
                    "offset_m": float(h.openftth.get("offset_m", 0.0)),
                },
            )
        )
    decisions = [
        Decision(
            stage="solve",
            tool="route_drops",
            reason="Each home connected to its FAT by the shortest street path plus the straight run from the street to the building.",
            values={"drops": n, "length_m": round(total, 1), "over_max_drop": too_long},
        )
    ]
    return _commit(
        s,
        ToolResult(
            tool="route_drops",
            summary=f"{n} drops, {total:.0f} m total, avg {total / max(1, n):.0f} m",
            decisions=decisions,
            warnings=([f"{too_long} drops exceed max_drop_m"] if too_long else [])
            + (
                [
                    f"{repaired} home(s) had lost their FAT link and got it back from the drops on record"
                ]
                if repaired
                else []
            ),
            data={"drops": n},
        ),
        before,
    )


def place_chambers(s: Session) -> ToolResult:
    """Chambers at branch points of the cable trees (small) and at FDHs (large); a closure in
    each branch chamber sized to the largest passing cable."""
    s.project.set_stage("solve")
    twin = _twin(s)
    r = s.rules
    before = s.design.copy()
    _clear_solved(s, "chamber")
    fdh_nodes = {int(f.openftth["node"]) for f in s.design.by_kind("fdh")}
    fat_nodes = {int(f.openftth["node"]) for f in s.design.by_kind("fat")}
    pop_nodes = {int(p.openftth["node"]) for p in s.design.by_kind("pop")}
    branch: dict[int, int] = {}  # node → max downstream fibres passing
    carried: dict[int, set[str]] = {}  # node → surfaces of the tree edges meeting there
    for tree in s.trees.values():
        for u, v in tree.tree.edges:
            if twin.graph.has_edge(u, v):
                surface = str(twin.graph[u][v].get("surface", ""))
                carried.setdefault(int(u), set()).add(surface)
                carried.setdefault(int(v), set()).add(surface)
    for key, tree in s.trees.items():
        deg = dict(tree.tree.degree)
        for node, d in deg.items():
            if d >= 3 or (node in fat_nodes and not key.startswith("feeder")):
                fibres = max(
                    (u for (p, c), u in tree.downstream_units.items() if p == node or c == node),
                    default=0,
                )
                branch[node] = max(branch.get(node, 0), fibres)
    n_small = n_large = n_mounted = 0
    for node in sorted(set(branch) | fdh_nodes):
        if node in pop_nodes:
            continue
        large = node in fdh_nodes
        # A branch point where every cable meeting it is on poles or walls: the closure hangs
        # there and nothing is dug.
        here = carried.get(node, set())
        mount = (
            ("pole" if "aerial" in here else "wall")
            if here and not large and here <= {"aerial", "facade"}
            else None
        )
        size = next(
            (sz for sz in sorted(r.closures.sizes) if sz >= branch.get(node, 0)),
            max(r.closures.sizes),
        )
        s.design.add(
            Feature(
                element_uuid=s.uid("manhole", f"chamber:{node}"),
                element_type="manhole",
                geometry=_wgs(twin, twin.node_point(node)),
                props={
                    "manhole_id": f"CH-{node}",
                    "manhole_type": "large" if large else "small",
                    "dimensions_cm": r.chambers.types["large" if large else "small"],
                    "status": "planned",
                },
                openftth={
                    "kind": "chamber",
                    "node": int(node),
                    "closure": not large and node not in fat_nodes,
                    "size": size,
                    **({"mount": mount} if mount else {}),
                },
            )
        )
        n_large += large
        n_small += not large and not mount
        n_mounted += bool(mount)
    decisions = [
        Decision(
            stage="solve",
            tool="place_chambers",
            reason="Chambers placed at every branch point of the feeder/distribution trees and at FAT sites (small) and at FDHs (large); branch chambers get a splice closure sized to the largest cable passing through."
            + (
                f" {n_mounted} branch point(s) lie on poles or walls only: their closure is mounted there and no chamber is dug."
                if n_mounted
                else ""
            ),
            values={"small": n_small, "large": n_large, "mounted": n_mounted},
            source="assumption",
        )
    ]
    return _commit(
        s,
        ToolResult(
            tool="place_chambers",
            summary=f"{n_small} small + {n_large} large chambers",
            decisions=decisions,
            data={"small": n_small, "large": n_large},
        ),
        before,
    )


# ---- Validate / Deliver -------------------------------------------------------------------


def validate_design(s: Session) -> ToolResult:
    s.project.set_stage("validate")
    before = s.design.copy()
    violations, stats = run_all_checks(s.design, s.rules)
    summary = violation_summary(violations)
    warnings_summary = violation_summary(violations, "warning")
    errors = [v for v in violations if v.severity == "error"]
    data = {
        "violations": [v.__dict__ for v in violations[:200]],
        "summary": summary,
        "warnings_summary": warnings_summary,
        "power_budget": stats,
        # The check commits its own version; this is the design it looked at.
        "design_version": s.project.head(),
    }
    (s.project.deliver_dir / "validation.json").write_text(_json(data), encoding="utf-8")
    decisions = [
        Decision(
            stage="validate",
            tool="check_rules",
            reason=f"ODN loss per home = fibre ({s.rules.pon.worst_case_nm} nm) + {s.rules.connectors_on_path()} connectors × {s.rules.loss_db.connector} + splices × {s.rules.loss_db.splice} + splitter {s.rules.splitter_loss():.1f} dB + margin {s.rules.loss_db.margin} dB, checked against {'the point-to-point optics budget ' + format(s.rules.budget_db(), 'g') + ' dB' if s.rules.pon.arch() == 'p2p' else 'class ' + str(s.rules.pon.pon_class) + ' budget ' + format(s.rules.budget_db(), 'g') + ' dB and differential ' + format(s.rules.pon.max_differential_db, 'g') + ' dB per PON'}.",
            values=stats,
            source="rulebook",
        )
    ]
    msg = "no violations" if not errors else ", ".join(f"{k}: {v}" for k, v in summary.items())
    nwarn = len(violations) - len(errors)
    if nwarn:
        msg += f"; {nwarn} warning(s) for the engineer to accept"
    return _commit(
        s,
        ToolResult(
            tool="check_rules",
            summary=f"{msg}; worst ODN loss {stats.get('worst_db')} dB of {s.rules.budget_db()} dB",
            decisions=decisions,
            warnings=[f"{v.severity}: {v.message}" for v in violations[:10]],
            data=data,
        ),
        before,
    )


# ---- phasing ----------------------------------------------------------------------------


def plan_phases(s: Session, phases: int | None = None, order: str | None = None) -> ToolResult:
    """Group the cabinet areas into build phases of about equal units, densest areas (or the
    nearest the POP) first, and stamp every element with the phase that builds it: a feeder
    section, a chamber or a crossing goes in the earliest phase that needs it."""
    s.project.set_stage("solve")
    r = s.rules
    n = int(phases or r.phasing.phases)
    how = order or r.phasing.order
    if n < 1 or how not in ("density", "distance"):
        return _rejected(
            "plan_phases", "phases is a whole number from 1, order is density or distance"
        )
    before = s.design.copy()
    fdhs = s.design.by_kind("fdh")
    if not fdhs:
        return _rejected("plan_phases", "place the cabinets first")
    dist_m: dict[str, float] = {}
    for c in s.design.by_kind("distribution"):
        k = str(c.openftth.get("fdh_id", ""))
        dist_m[k] = dist_m.get(k, 0.0) + float(c.props.get("length_m", 0.0))

    def density(f: Feature) -> float:
        return int(f.openftth.get("units", 0)) / max(1.0, dist_m.get(f.element_uuid, 0.0))

    if how == "density":
        ranked = sorted(fdhs, key=lambda f: (-density(f), f.element_uuid))
    else:
        ranked = sorted(
            fdhs, key=lambda f: (float(f.openftth.get("feeder_path_m", 0.0)), f.element_uuid)
        )
    total = sum(int(f.openftth.get("units", 0)) for f in ranked) or 1
    phase_of: dict[str, int] = {}
    done = 0
    for f in ranked:
        phase_of[f.element_uuid] = min(n, 1 + int(done * n / total))
        done += int(f.openftth.get("units", 0))
    for f in fdhs:
        f.openftth["phase"] = phase_of[f.element_uuid]
    fat_phase: dict[str, int] = {}
    for f in s.design.by_kind("fat"):
        fat_phase[f.element_uuid] = phase_of.get(str(f.openftth.get("fdh_id", "")), 1)
        f.openftth["phase"] = fat_phase[f.element_uuid]
    home_phase: dict[str, int] = {}
    for h in s.design.by_kind("home"):
        # A building with dedicated FATs under two cabinets is connected in the earlier phase.
        ids = h.openftth.get("fat_ids") or [h.openftth.get("fat_id")]
        home_phase[h.element_uuid] = min((fat_phase.get(str(i), n) for i in ids), default=1)
        h.openftth["phase"] = home_phase[h.element_uuid]
    for d in s.design.by_kind("drop"):
        d.openftth["phase"] = home_phase.get(str(d.openftth.get("home", "")), 1)
    for c in s.design.by_kind("distribution"):
        c.openftth["phase"] = phase_of.get(str(c.openftth.get("fdh_id", "")), 1)
    # Feeder: walk up from each cabinet to its POP; a section takes the earliest phase below it.
    feeders = s.design.by_kind("feeder")
    up = {str(c.props.get("to_label")): c for c in feeders}
    for c in feeders:
        c.openftth["phase"] = n
    for f in sorted(fdhs, key=lambda f: f.openftth["phase"]):
        node = f"N{f.openftth['node']}"
        seen: set[str] = set()
        while node in up and node not in seen:
            seen.add(node)
            c = up[node]
            c.openftth["phase"] = min(int(c.openftth["phase"]), int(f.openftth["phase"]))
            node = str(c.props.get("from_label"))
    # Chambers, crossings and POPs: the earliest phase among the cables at them.
    node_phase: dict[int, int] = {}
    edge_phase: dict[str, int] = {}
    for c in feeders + s.design.by_kind("distribution"):
        ph = int(c.openftth["phase"])
        for key in c.openftth.get("edges", {}):
            edge_phase[key] = min(edge_phase.get(key, n), ph)
            for x in key.split("-"):
                node_phase[int(x)] = min(node_phase.get(int(x), n), ph)
    for ch in s.design.by_kind("chamber"):
        ch.openftth["phase"] = node_phase.get(int(ch.openftth.get("node", -1)), 1)
    for x in s.design.by_kind("crossing"):
        x.openftth["phase"] = edge_phase.get(str(x.openftth.get("edge", "")), 1)
    for p in s.design.by_kind("pop"):
        p.openftth["phase"] = 1
    per: dict[int, dict[str, int]] = {}
    for f in fdhs:
        k = int(f.openftth["phase"])
        per.setdefault(k, {"fdhs": 0, "units": 0})
        per[k]["fdhs"] += 1
        per[k]["units"] += int(f.openftth.get("units", 0))
    lines = "; ".join(
        f"phase {k}: {v['fdhs']} cabinet(s), {v['units']} units" for k, v in sorted(per.items())
    )
    result = ToolResult(
        tool="plan_phases",
        summary=f"{n} build phase(s) by {how}: {lines}",
        decisions=[
            Decision(
                stage="solve",
                tool="plan_phases",
                reason=f"Cabinet areas grouped into {n} phase(s) of about equal units, "
                + (
                    "the most units per metre of distribution cable first (the best business case)"
                    if how == "density"
                    else "the nearest the POP first"
                )
                + "; a feeder section, chamber or crossing is built in the earliest phase that needs it.",
                values={
                    "phases": n,
                    "order": how,
                    "per_phase": {str(k): v for k, v in per.items()},
                },
                source="rulebook",
            )
        ],
        data={"phases": n, "order": how, "per_phase": per},
    )
    return _commit(s, result, before)


def _phase_designs(design: Design) -> dict[int, Design]:
    """The design split by build phase (elements without a phase are not built by any)."""
    out: dict[int, Design] = {}
    for f in design:
        ph = f.openftth.get("phase")
        if ph is None:
            continue
        out.setdefault(int(ph), Design(design.metadata.model_copy())).add(f)
    return out


def deliver(s: Session, title: str | None = None) -> ToolResult:
    s.project.set_stage("deliver")
    twin = _twin(s)
    boq = build_boq(s.design, s.rules)
    d = s.project.deliver_dir
    (d / "boq.json").write_text(boq.model_dump_json(indent=2) + "\n", encoding="utf-8")
    (d / "boq.csv").write_text(boq_to_csv(boq), encoding="utf-8")
    phases = []
    dug: set[str] = set()  # a trench shared by two phases is dug in the earlier one
    for k, part in sorted(_phase_designs(s.design).items()):
        pb = build_boq(part, s.rules, dug)
        (d / f"boq_phase_{k}.csv").write_text(boq_to_csv(pb), encoding="utf-8")
        phases.append(
            {
                "phase": k,
                "units": pb.units_served,
                "fdhs": len(part.by_kind("fdh")),
                "total": pb.grand_total,
                "cost_per_unit": pb.cost_per_unit,
                "totals": pb.totals,
            }
        )
    for old in d.glob("boq_phase_*.csv"):
        if int(old.stem.rsplit("_", 1)[1]) not in {p["phase"] for p in phases}:
            old.unlink()
    if phases:
        # The app reads the phases with the BOQ itself.
        with_phases = {**json.loads(boq.model_dump_json()), "phases": phases}
        (d / "boq.json").write_text(_json(with_phases), encoding="utf-8")
    png = render_map(s.design, twin, d / "map.png", title or f"openFTTH — {s.pid} ({s.rules.name})")
    summary = {
        "project": s.pid,
        "rulebook": s.rules.name,
        "homes": len(s.design.by_kind("home")),
        "units": boq.units_served,
        "fats": len(s.design.by_kind("fat")),
        "fdhs": len(s.design.by_kind("fdh")),
        "pons": sum(int(f.openftth.get("pons", 0)) for f in s.design.by_kind("fdh")),
        "feeder_m": round(
            sum(float(c.props.get("length_m", 0)) for c in s.design.by_kind("feeder")), 1
        ),
        "distribution_m": round(
            sum(float(c.props.get("length_m", 0)) for c in s.design.by_kind("distribution")), 1
        ),
        "drop_m": round(
            sum(float(c.props.get("length_m", 0)) for c in s.design.by_kind("drop")), 1
        ),
        "boq_total": boq.grand_total,
        "currency": boq.currency,
        "cost_per_unit": boq.cost_per_unit,
        "units_connected": boq.units_connected,
        "cost_per_connection": boq.cost_per_connection,
        "take_rate": s.rules.fibers.take_rate,
        "totals": boq.totals,
        **({"phases": phases} if phases else {}),
    }
    (d / "summary.json").write_text(_json(summary), encoding="utf-8")
    decisions = [
        Decision(
            stage="deliver",
            tool="boq",
            reason="BOQ built from the design: trench metres by surface (shared by all cables on an edge), cable metres by fibre count, passives counted, labour per splice/test, crossings as permits.",
            values={"grand_total": boq.grand_total, "cost_per_unit": boq.cost_per_unit},
            source="rulebook",
        )
    ]
    result = ToolResult(
        tool="boq",
        summary=f"BOQ {boq.grand_total:,.0f} {boq.currency} for {boq.units_served} units ({boq.cost_per_unit:,.0f}/unit); map at {png.name}",
        decisions=decisions,
        data=summary,
    )
    s.project.commit(result, s.design)
    return result


# ---- the whole pipeline -------------------------------------------------------------------


def design_all(
    area_path: str | Path,
    osm_path: str | Path,
    rules: Rulebook,
    out_dir: str | Path,
    project_id: str | None = None,
    pop: tuple[float, float] | None = None,
    time_limit_s: float = WORK_LIMIT,
    rulebook_path: str | Path | None = None,
    profile: str | Path | None = None,
) -> list[ToolResult]:
    """The whole pipeline headless, from an area and an OSM extract or from an import profile."""
    s = open_session(out_dir, rules, project_id=project_id or Path(out_dir).name)
    if rulebook_path is not None:
        shutil.copyfile(rulebook_path, s.project.dir / "rulebook.yaml")
    first = (
        import_data(s, profile)
        if profile is not None
        else import_area_and_osm(s, area_path, osm_path)
    )
    if first.summary.startswith("rejected"):
        raise ValueError(first.summary)
    results = [
        first,
        build_twin_tool(s),
        set_pop(s, *(pop or (None, None))),
        cluster_homes(s, time_limit_s),
        place_fdh(s, time_limit_s),
        assign_pon(s),
        route_feeder(s),
        route_distribution(s),
        route_drops(s),
        place_chambers(s),
        size_cables(s),
        assign_fibers(s),
        validate_design(s),
        *([plan_phases(s)] if rules.phasing.phases > 1 else []),
        deliver(s),
        splice_schedule(s),
        export_gpkg(s),
    ]
    return results


# ---- internals ----------------------------------------------------------------------------


def _twin(s: Session) -> Twin:
    """The twin, rebuilt from the project's recorded inputs when this session has none."""
    if s.twin is None:
        profile = _load_profile(s)
        if profile is None:
            raise RuntimeError("twin not built; import the project's data first")
        inp: Inputs = load_inputs(profile, s.rules)
        homes_wgs = _filter_excluded(s, inp.homes)
        s.cache.update(
            {
                "inputs": inp,
                "streets": inp.streets,
                "homes_wgs": homes_wgs,
                "area": inp.area,
                "no_dig_zones": inp.no_dig,
                "barriers": inp.barriers,
            }
        )
        s.twin = _finish_twin(
            s,
            build_twin(inp.streets, homes_wgs, s.rules, inp.barriers, inp.buildings, inp.poles),
        )
        pops = s.design.by_kind("pop")
        if pops:
            s.cache["pop_node"] = int(pops[0].openftth["node"])
    return s.twin


def _emit_cable_sections(
    s: Session,
    tree: RoutedTree,
    kind: str,
    ladder: list[int],
    prefix: str,
    fdh_id: str | None = None,
    pop_id: str | None = None,
) -> int:
    twin = _twin(s)
    r = s.rules
    n = 0
    for i, sec in enumerate(tree.sections):
        cable_count, fibers = r.ladder_cables(ladder, sec.downstream_units)
        edges = {
            f"{min(u, v)}-{max(u, v)}": (
                f"crossing:{twin.graph[u][v]['crossing']}"
                if twin.graph[u][v].get("crossing")
                else twin.graph[u][v]["surface"],
                round(twin.graph[u][v]["length_m"], 2),
            )
            for u, v in zip(sec.nodes[:-1], sec.nodes[1:], strict=False)
        }
        crossings = sum(
            1
            for u, v in zip(sec.nodes[:-1], sec.nodes[1:], strict=False)
            if twin.graph[u][v]["highway"] in ("primary", "secondary", "trunk")
        )
        slack = r.slack.per_closure_m
        s.design.add(
            Feature(
                element_uuid=s.uid(
                    "cable.underground", f"{kind}:{prefix}:{sec.upstream}-{sec.downstream}"
                ),
                element_type="cable.underground",
                geometry=_wgs(twin, twin.path_line(sec.nodes)),
                props={
                    "cable_type": kind,
                    "fiber_count": fibers,
                    "cable_count": cable_count,
                    "from_label": f"N{sec.upstream}",
                    "to_label": f"N{sec.downstream}",
                    "length_m": round(sec.length_m, 1),
                    "slack_m": slack,
                    "total_length_m": round(sec.length_m + slack, 1),
                    "status": "planned",
                    "name": f"{prefix}-{i + 1:03d}",
                },
                openftth={
                    "kind": kind,
                    "downstream_units": sec.downstream_units,
                    "edges": edges,
                    "surfaces": {k: round(v, 1) for k, v in sec.surfaces.items()},
                    "road_crossings": crossings,
                    "fdh_id": fdh_id,
                    **({"pop_id": pop_id} if pop_id else {}),
                    "trench_cost": round(sec.cost, 2),
                },
            )
        )
        n += 1
    return n


def _path_length(twin: Twin, tree: RoutedTree, node: int) -> float:
    path = tree.path_to_root(node)
    return sum(twin.graph[u][v]["length_m"] for u, v in zip(path[:-1], path[1:], strict=False))


def _closures_on_path(tree: RoutedTree | None, node: int) -> int:
    if tree is None or node not in tree.parent:
        return 0
    deg = dict(tree.tree.degree)
    return sum(1 for n in tree.path_to_root(node)[1:-1] if deg.get(n, 0) >= 3)


def _json(obj: Any) -> str:
    import json

    return json.dumps(obj, indent=2, sort_keys=True, default=str) + "\n"


__all__ = [
    "Session",
    "open_session",
    "import_area_and_osm",
    "build_twin_tool",
    "set_pop",
    "cluster_homes",
    "place_fdh",
    "assign_pon",
    "route_feeder",
    "route_distribution",
    "route_drops",
    "place_chambers",
    "validate_design",
    "deliver",
    "set_rule",
    "size_cables",
    "assign_fibers",
    "splice_schedule",
    "export_gpkg",
    "generate_scenarios",
    "apply_scenario",
    "design_all",
    "mapping",
]


# ---- Rules --------------------------------------------------------------------------------


def set_rule(s: Session, path: str, value: Any) -> ToolResult:
    """Change one rulebook value (dotted path such as `fat.max_homes`), validated against the
    schema and written to the project's rulebook.yaml. Solvers must be re-run afterwards."""
    import yaml
    from pydantic import ValidationError

    from .solve.scenarios import parse_value, set_path

    s.project.set_stage("rules")
    parsed = parse_value(value)
    data = s.rules.model_dump(by_alias=True)
    try:
        old = set_path(data, path, parsed)
        new_rules = Rulebook.model_validate(data)
    except KeyError:
        return ToolResult(
            tool="set_rule",
            summary=f"rejected: no rule at {path!r}",
            warnings=[f"unknown rule path {path!r}; use get_rulebook to see the paths"],
        )
    except ValidationError as exc:
        msgs = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())
        return ToolResult(tool="set_rule", summary=f"rejected: {msgs}", warnings=[msgs])
    s.rules = new_rules
    (s.project.dir / "rulebook.yaml").write_text(
        yaml.safe_dump(new_rules.model_dump(by_alias=True), sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    from .solve.scenarios import TWIN_SECTIONS

    if path.split(".")[0] in TWIN_SECTIONS and s.twin is not None:
        # Route options and prices live on the street graph: re-price it (same nodes).
        if "streets" in s.cache:
            s.twin = _finish_twin(s, _raw_twin(s))
        else:
            s.twin = None
    decisions = [
        Decision(
            stage="rules",
            tool="set_rule",
            reason=f"User set {path} from {old!r} to {parsed!r}; the rulebook re-validated.",
            values={"path": path, "old": old, "new": parsed},
            source="user",
        )
    ]
    result = ToolResult(
        tool="set_rule",
        summary=f"{path}: {old!r} -> {parsed!r}",
        decisions=decisions,
        warnings=(
            ["re-run the solvers (cluster_homes ... place_chambers) for the change to take effect"]
            if s.design.by_kind("fat")
            else []
        ),
        data={"path": path, "old": old, "new": parsed},
    )
    s.project.commit(result, s.design)
    return result


# ---- Solve: sizing and fibre assignment --------------------------------------------------


def size_cables(s: Session) -> ToolResult:
    """Re-size every feeder/distribution section from its downstream demand and the rulebook
    ladders (parallel cables when one is not enough), with tubes, slack and total length."""
    from .solve.fibers import FIBERS_PER_TUBE

    s.project.set_stage("solve")
    r = s.rules
    before = s.design.copy()
    n = 0
    fibers_total = 0.0
    for kind, ladder in (("feeder", r.fibers.feeder), ("distribution", r.fibers.distribution)):
        for c in s.design.by_kind(kind):
            demand = int(c.openftth.get("downstream_units", 0))
            count, fibers = r.ladder_cables(ladder, demand)
            length = float(c.props.get("length_m", 0.0))
            c.props.update(
                {
                    "fiber_count": fibers,
                    "cable_count": count,
                    "tube_count": -(-fibers // FIBERS_PER_TUBE),
                    "fibers_per_tube": FIBERS_PER_TUBE,
                    "total_fibers": fibers * count,
                    "slack_m": r.slack.per_closure_m,
                    "total_length_m": round(length + r.slack.per_closure_m, 1),
                    "color_standard": s.design.metadata.color_standard,
                }
            )
            fibers_total += fibers * count * length
            n += 1
    for d in s.design.by_kind("drop"):
        home_id = d.openftth.get("home")
        home = s.design.get(home_id) if home_id in s.design else None
        units = int(home.openftth.get("units", 1)) if home else 1
        d.props["fiber_count"] = max(
            r.fibers.drop, next((x for x in (2, 4, 12, 24, 48) if x >= units), 48)
        )
    decisions = [
        Decision(
            stage="solve",
            tool="size_cables",
            reason=(
                f"Each section carries downstream units + {r.fibers.spare_pct:g}% spare rounded up "
                f"the ladder (feeder {r.fibers.feeder}, distribution {r.fibers.distribution}); "
                "demand above the largest cable becomes parallel cables of equal size; "
                f"{FIBERS_PER_TUBE} fibres per tube; {r.slack.per_closure_m:g} m slack per section."
            ),
            values={"sections": n, "fibre_metres": round(fibers_total)},
            source="rulebook",
        )
    ]
    return _commit(
        s,
        ToolResult(
            tool="size_cables",
            summary=f"{n} sections sized, {fibers_total / 1000:,.0f} fibre-km",
            decisions=decisions,
            data={"sections": n},
        ),
        before,
    )


def _pass_through(fdh, fdh_node, my_fats, tf, connect, point_for) -> None:
    """Pass-through splices at every inner node of an FDH's tree: incoming section -> each
    outgoing section, fibre for fibre."""
    from .solve.fibers import tube_fiber

    for node, outs in tf.outgoing.items():
        in_sec = tf.incoming.get(node)
        if not in_sec or not outs:
            continue
        fallback = next(
            (f.element_uuid for f in my_fats if int(f.openftth["node"]) == node),
            fdh.element_uuid,
        )
        for out_sec in outs:
            lo, hi = tf.section_range.get(out_sec, (0, -1))
            for g in range(lo, hi + 1):
                ti, fi = tube_fiber(tf.local(in_sec, g))
                to, fo = tube_fiber(tf.local(out_sec, g))
                connect(
                    point_for(fdh.element_uuid, node, fallback),
                    f"{fdh.element_uuid}:thru:{node}:{g}",
                    source_cable_uuid=in_sec,
                    source_tube=ti,
                    source_fiber=fi,
                    dest_cable_uuid=out_sec,
                    dest_tube=to,
                    dest_fiber=fo,
                )


def _cascade_fibres(
    s, fdh, my_fats, homes_by_fat, drops_by_home, tf, connect, point_for, units_of
) -> int:
    """Cascaded fibres under one FDH: a first-stage splitter output per FAT splitter, carried
    on its own distribution fibre to the FAT, where the FAT splitter's outputs feed the drops.
    Returns the units served."""
    from .solve.fibers import tube_fiber

    r = s.rules
    s1, s2 = r.cascade_stages()
    stage1, stage2 = r.loss_db.splitter[s1], r.loss_db.splitter[s2]
    fdh_node = int(fdh.openftth["node"])
    # First-stage ports: PON order as assign_pon packed the FAT splitters.
    split_port: dict[tuple[str, int], tuple[str, int]] = {}
    pon_fill: dict[str, int] = {}
    for f in sorted(
        my_fats, key=lambda f: (f.openftth.get("distribution_est_m", 0.0), f.element_uuid)
    ):
        pon = f.openftth.get("pon_id", "")
        for j in range(1, int(f.openftth.get("splitters", 0)) + 1):
            pon_fill[pon] = pon_fill.get(pon, 0) + 1
            split_port[(f.element_uuid, j)] = (pon, pon_fill[pon])
    served = 0
    for node, (lo, _hi) in sorted(tf.terminal_range.items()):
        g = lo
        in_sec = tf.incoming.get(node)
        for f in sorted(
            (f for f in my_fats if int(f.openftth["node"]) == node), key=lambda f: f.element_uuid
        ):
            f_lo = g
            slots = [
                (h, k)
                for h in sorted(homes_by_fat.get(f.element_uuid, []), key=lambda h: h.element_uuid)
                for k in range(1, units_of(h, f) + 1)
            ]
            for j in range(1, int(f.openftth.get("splitters", 0)) + 1):
                side = f"{f.props['name']}-SPL-{j:02d}"
                pon, port = split_port[(f.element_uuid, j)]
                fat_in = {
                    "dest_element_uuid": f.element_uuid,
                    "dest_port": j,
                    "dest_side": f"{side} splitter in",
                }
                out_sec = _section_carrying(tf, fdh_node, g)
                if out_sec:
                    t, fb = tube_fiber(tf.local(out_sec, g))
                    connect(
                        point_for(fdh.element_uuid, fdh_node, fdh.element_uuid),
                        f"{fdh.element_uuid}:fdh:{g}",
                        source_element_uuid=fdh.element_uuid,
                        source_port=port,
                        source_side=pon,
                        dest_cable_uuid=out_sec,
                        dest_tube=t,
                        dest_fiber=fb,
                        connection_type="splitter",
                        loss_db=stage1,
                    )
                if in_sec:
                    t, fb = tube_fiber(tf.local(in_sec, g))
                    connect(
                        point_for(fdh.element_uuid, node, f.element_uuid),
                        f"{fdh.element_uuid}:fat:{g}",
                        source_cable_uuid=in_sec,
                        source_tube=t,
                        source_fiber=fb,
                        **fat_in,
                    )
                elif node == fdh_node:
                    # FAT standing at the FDH: first-stage output patched to the FAT splitter.
                    connect(
                        point_for(fdh.element_uuid, node, fdh.element_uuid),
                        f"{fdh.element_uuid}:fat:{g}",
                        source_element_uuid=fdh.element_uuid,
                        source_port=port,
                        source_side=pon,
                        connection_type="splitter",
                        loss_db=stage1,
                        **fat_in,
                    )
                for k, (h, unit) in enumerate(slots[(j - 1) * s2 : j * s2], start=1):
                    drop = drops_by_home.get(h.element_uuid)
                    if drop is not None and not h.openftth.get("mdu"):
                        dest = {
                            "dest_cable_uuid": drop.element_uuid,
                            "dest_tube": 1,
                            "dest_fiber": unit,
                        }
                    else:
                        dest = {
                            "dest_element_uuid": f.element_uuid,
                            "dest_port": unit,
                            "dest_side": "indoor",
                        }
                    connect(
                        point_for(fdh.element_uuid, node, f.element_uuid),
                        f"{f.element_uuid}:spl:{j}:{k}",
                        source_element_uuid=f.element_uuid,
                        source_port=k,
                        source_side=side,
                        connection_type="splitter",
                        loss_db=stage2,
                        **dest,
                    )
                    if unit == 1:
                        h.openftth.update({"fat_splitter": j, "splitter_port": k})
                g += 1
            for h in {h.element_uuid: h for h, _ in slots}.values():
                h.openftth.pop("fiber_start", None)
                h.openftth.pop("fiber_end", None)
            served += len(slots)
            f.openftth.update({"fiber_start": f_lo, "fiber_end": g - 1})
    return served


def assign_fibers(s: Session) -> ToolResult:
    """Give every unit its fibre: splitter port in the FDH, fibre/tube on each distribution
    section, drop fibre at the home; feeder fibre per PON. Writes the splice_point and
    fiber_connection side tables."""
    from .solve.fibers import Section as FSection
    from .solve.fibers import assign_tree_fibers, tube_fiber

    s.project.set_stage("solve")
    r = s.rules
    before = s.design.copy()
    connections: list[dict[str, Any]] = []
    points: dict[str, dict[str, Any]] = {}
    chambers_at = {int(c.openftth["node"]): c for c in s.design.by_kind("chamber")}
    fats = s.design.by_kind("fat")
    homes = s.design.by_kind("home")
    drops_by_home = {d.openftth.get("home"): d for d in s.design.by_kind("drop")}
    homes_by_fat: dict[str, list[Feature]] = {}
    for h in homes:
        for fid in h.openftth.get("fat_ids") or [h.openftth.get("fat_id")]:
            if fid:
                homes_by_fat.setdefault(fid, []).append(h)
    splice_loss = r.loss_db.splice

    def point_for(tree: str, node: int, fallback_uuid: str) -> str:
        pid = s.uid("splice_point", f"{tree}:{node}")
        if pid not in points:
            el = chambers_at.get(node)
            points[pid] = {
                "uuid": pid,
                "element_uuid": el.element_uuid if el else fallback_uuid,
                "port_count": 0,
                "used_ports": 0,
                "config_json": json.dumps({"tree": tree, "node": node}, sort_keys=True),
                "notes": "",
            }
        return pid

    def connect(pid: str, key: str, **fields: Any) -> None:
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
            "loss_db": splice_loss,
            "tray_ref": None,
            "notes": None,
        }
        row.update(fields)
        connections.append(row)
        points[pid]["port_count"] += 1
        points[pid]["used_ports"] += 1

    def units_of(h: Feature, f: Feature) -> int:
        return int(f.openftth["units"]) if h.openftth.get("mdu") else int(h.openftth["units"])

    # ---- distribution: one tree per FDH ---------------------------------------------------
    arch = r.pon.arch()
    units_total = 0
    for fdh in s.design.by_kind("fdh"):
        fdh_node = int(fdh.openftth["node"])
        secs = [
            c
            for c in s.design.by_kind("distribution")
            if c.openftth.get("fdh_id") == fdh.element_uuid
        ]
        fsecs = [
            FSection(c.element_uuid, _node_of(c.props["from_label"]), _node_of(c.props["to_label"]))
            for c in secs
        ]
        my_fats = sorted(
            (f for f in fats if f.openftth.get("fdh_id") == fdh.element_uuid),
            key=lambda f: f.element_uuid,
        )
        demand_at: dict[int, int] = {}
        for f in my_fats:
            node = int(f.openftth["node"])
            demand_at[node] = demand_at.get(node, 0) + _fibres_of(r, f)
        tf = assign_tree_fibers(fdh_node, fsecs, demand_at)
        for c in secs:
            lo, hi = tf.section_range.get(c.element_uuid, (0, -1))
            c.openftth["fiber_start"], c.openftth["fiber_end"] = lo, hi
            c.openftth["fibers_used"] = max(0, hi - lo + 1)
            if c.openftth["fibers_used"] > int(c.props.get("fiber_count", 0)) * int(
                c.props.get("cable_count", 1)
            ):
                c.openftth["undersized"] = True
        if arch == "cascaded":
            units_total += _cascade_fibres(
                s, fdh, my_fats, homes_by_fat, drops_by_home, tf, connect, point_for, units_of
            )
            _pass_through(fdh, fdh_node, my_fats, tf, connect, point_for)
            continue
        # Splitter ports: PON order as assign_pon used (FATs by distribution distance), homes by
        # id. Point-to-point has no PONs: every unit has its own patch port in the FDH.
        port_of: dict[tuple[str, str, int], tuple[str, int]] = {}
        pon_fill: dict[str, int] = {}
        for f in sorted(
            my_fats, key=lambda f: (f.openftth.get("distribution_est_m", 0.0), f.element_uuid)
        ):
            pon = "ODP" if arch == "p2p" else f.openftth.get("pon_id", "")
            for h in sorted(homes_by_fat.get(f.element_uuid, []), key=lambda h: h.element_uuid):
                for k in range(1, units_of(h, f) + 1):
                    pon_fill[pon] = pon_fill.get(pon, 0) + 1
                    port_of[(f.element_uuid, h.element_uuid, k)] = (pon, pon_fill[pon])
        at_fdh = (
            {"connection_type": "patch", "loss_db": r.loss_db.connector}
            if arch == "p2p"
            else {"connection_type": "splitter", "loss_db": r.splitter_loss()}
        )
        # Terminal ranges -> FATs -> homes -> units.
        for node, (lo, _hi) in sorted(tf.terminal_range.items()):
            g = lo
            node_fats = sorted(
                (f for f in my_fats if int(f.openftth["node"]) == node),
                key=lambda f: f.element_uuid,
            )
            in_sec = tf.incoming.get(node)
            for f in node_fats:
                f_lo = g
                for h in sorted(homes_by_fat.get(f.element_uuid, []), key=lambda h: h.element_uuid):
                    h_lo = g
                    k_units = units_of(h, f)
                    for k in range(1, k_units + 1):
                        pon, port = port_of[(f.element_uuid, h.element_uuid, k)]
                        drop = drops_by_home.get(h.element_uuid)
                        if drop is not None and not h.openftth.get("mdu"):
                            dest = {
                                "dest_cable_uuid": drop.element_uuid,
                                "dest_tube": 1,
                                "dest_fiber": k,
                            }
                        else:
                            dest = {
                                "dest_element_uuid": f.element_uuid,
                                "dest_port": k,
                                "dest_side": "indoor",
                            }
                        out_sec = _section_carrying(tf, fdh_node, g)
                        if out_sec:
                            t, fb = tube_fiber(tf.local(out_sec, g))
                            connect(
                                point_for(fdh.element_uuid, fdh_node, fdh.element_uuid),
                                f"{fdh.element_uuid}:fdh:{g}",
                                source_element_uuid=fdh.element_uuid,
                                source_port=port,
                                source_side=pon,
                                dest_cable_uuid=out_sec,
                                dest_tube=t,
                                dest_fiber=fb,
                                **at_fdh,
                            )
                        if in_sec:
                            t, fb = tube_fiber(tf.local(in_sec, g))
                            connect(
                                point_for(fdh.element_uuid, node, f.element_uuid),
                                f"{fdh.element_uuid}:fat:{g}",
                                source_cable_uuid=in_sec,
                                source_tube=t,
                                source_fiber=fb,
                                **dest,
                            )
                        elif node == fdh_node:
                            # FAT standing at the FDH: splitter port straight to the drop.
                            connect(
                                point_for(fdh.element_uuid, node, fdh.element_uuid),
                                f"{fdh.element_uuid}:fat:{g}",
                                source_element_uuid=fdh.element_uuid,
                                source_port=port,
                                source_side=pon,
                                **at_fdh,
                                **dest,
                            )
                        g += 1
                    if not h.openftth.get("mdu") or h.openftth.get("fat_id") == f.element_uuid:
                        h.openftth.update(
                            {
                                "fiber_start": h_lo,
                                "fiber_end": g - 1,
                                ("patch_port" if arch == "p2p" else "splitter_port"): port_of[
                                    (f.element_uuid, h.element_uuid, 1)
                                ][1],
                            }
                        )
                    units_total += k_units
                f.openftth.update({"fiber_start": f_lo, "fiber_end": g - 1})
        _pass_through(fdh, fdh_node, my_fats, tf, connect, point_for)

    # ---- feeder: one fibre per PON ---------------------------------------------------------
    pops = _pops(s)
    multi = len(pops) > 1
    for pop in pops:
        # One fibre plan per POP; with several, ids carry the POP so their numbers never collide.
        fk = f"feeder:{pop.element_uuid}" if multi else "feeder"
        pop_node = int(pop.openftth["node"])
        secs = [
            c
            for c in s.design.by_kind("feeder")
            if not multi or c.openftth.get("pop_id") == pop.element_uuid
        ]
        fsecs = [
            FSection(c.element_uuid, _node_of(c.props["from_label"]), _node_of(c.props["to_label"]))
            for c in secs
        ]
        fdhs = [
            f
            for f in s.design.by_kind("fdh")
            if not multi or f.openftth.get("pop_id") == pop.element_uuid
        ]
        demand_at = {}
        for f in fdhs:
            node = int(f.openftth["node"])
            demand_at[node] = demand_at.get(node, 0) + int(f.openftth.get("pons", 0))
        tf = assign_tree_fibers(pop_node, fsecs, demand_at)
        for c in secs:
            lo, hi = tf.section_range.get(c.element_uuid, (0, -1))
            c.openftth["fiber_start"], c.openftth["fiber_end"] = lo, hi
            c.openftth["fibers_used"] = max(0, hi - lo + 1)
        for node, (lo, _hi) in sorted(tf.terminal_range.items()):
            g = lo
            in_sec = tf.incoming.get(node)
            for f in sorted(
                (f for f in fdhs if int(f.openftth["node"]) == node), key=lambda f: f.element_uuid
            ):
                f_lo = g
                for p in range(1, int(f.openftth.get("pons", 0)) + 1):
                    out_sec = _section_carrying(tf, pop_node, g)
                    if out_sec:
                        t, fb = tube_fiber(tf.local(out_sec, g))
                        connect(
                            point_for(fk, pop_node, pop.element_uuid),
                            f"{fk}:pop:{g}",
                            source_element_uuid=pop.element_uuid,
                            source_port=g,
                            source_side="OLT",
                            dest_cable_uuid=out_sec,
                            dest_tube=t,
                            dest_fiber=fb,
                            connection_type="patch",
                            loss_db=r.loss_db.connector,
                        )
                    in_side = (
                        "ODP in"
                        if r.pon.arch() == "p2p"
                        else f"{f.props['name']}-PON-{p:02d} splitter in"
                    )
                    if in_sec:
                        t, fb = tube_fiber(tf.local(in_sec, g))
                        connect(
                            point_for(fk, node, f.element_uuid),
                            f"{fk}:fdh:{g}",
                            source_cable_uuid=in_sec,
                            source_tube=t,
                            source_fiber=fb,
                            dest_element_uuid=f.element_uuid,
                            dest_port=p,
                            dest_side=in_side,
                        )
                    elif node == pop_node:
                        # FDH standing at the POP: OLT port patched straight to the splitter.
                        connect(
                            point_for(fk, node, pop.element_uuid),
                            f"{fk}:fdh:{g}",
                            source_element_uuid=pop.element_uuid,
                            source_port=g,
                            source_side="OLT",
                            dest_element_uuid=f.element_uuid,
                            dest_port=p,
                            dest_side=in_side,
                            connection_type="patch",
                            loss_db=r.loss_db.connector,
                        )
                    g += 1
                f.openftth.update({"feeder_fiber_start": f_lo, "feeder_fiber_end": g - 1})
        for node, outs in tf.outgoing.items():
            in_sec = tf.incoming.get(node)
            if not in_sec or not outs:
                continue
            fallback = next(
                (f.element_uuid for f in fdhs if int(f.openftth["node"]) == node),
                pop.element_uuid,
            )
            for out_sec in outs:
                lo, hi = tf.section_range.get(out_sec, (0, -1))
                for g in range(lo, hi + 1):
                    ti, fi = tube_fiber(tf.local(in_sec, g))
                    to, fo = tube_fiber(tf.local(out_sec, g))
                    connect(
                        point_for(fk, node, fallback),
                        f"{fk}:thru:{node}:{g}",
                        source_cable_uuid=in_sec,
                        source_tube=ti,
                        source_fiber=fi,
                        dest_cable_uuid=out_sec,
                        dest_tube=to,
                        dest_fiber=fo,
                    )

    s.design.set_table("splice_point", list(points.values()))
    s.design.set_table("fiber_connection", connections)
    undersized = sum(1 for c in s.design.by_kind("distribution") if c.openftth.get("undersized"))
    decisions = [
        Decision(
            stage="solve",
            tool="assign_fibers",
            reason=(
                "Fibres numbered depth-first from each FDH so every section carries a contiguous "
                "range; each unit gets a splitter port (PON order as assigned), a fibre/tube on every "
                "section of its path and a drop fibre; one feeder fibre per PON from the OLT. "
                "Splices recorded per closure as source->destination tube/fibre with the rulebook's loss."
            ),
            values={
                "units": units_total,
                "connections": len(connections),
                "splice_points": len(points),
            },
            source="solver",
        )
    ]
    warnings = (
        [f"{undersized} distribution section(s) carry more fibres than sized for; run size_cables"]
        if undersized
        else []
    )
    return _commit(
        s,
        ToolResult(
            tool="assign_fibers",
            summary=(
                f"{units_total} units assigned, {len(connections)} connections "
                f"in {len(points)} splice points"
            ),
            decisions=decisions,
            warnings=warnings,
            data={"connections": len(connections), "splice_points": len(points)},
        ),
        before,
    )


def _node_of(label: str) -> int:
    return int(str(label).lstrip("N"))


def _section_carrying(tf: Any, node: int, g: int) -> str | None:
    for out in tf.outgoing.get(node, []):
        lo, hi = tf.section_range.get(out, (0, -1))
        if lo <= g <= hi:
            return out
    return None


# ---- Deliver: splice schedule, GeoPackage, scenarios ---------------------------------------


def splice_schedule(s: Session) -> ToolResult:
    from .deliver.splice import splice_schedule_csv, splice_schedule_rows, splice_summary

    s.project.set_stage("deliver")
    rows = splice_schedule_rows(s.design)
    d = s.project.deliver_dir
    (d / "splice_schedule.csv").write_text(splice_schedule_csv(rows), encoding="utf-8")
    summary = splice_summary(rows)
    (d / "splice_schedule.json").write_text(_json(summary), encoding="utf-8")
    result = ToolResult(
        tool="splice_schedule",
        summary=(
            f"{summary['connections']} connections in {summary['closures']} closures "
            "-> splice_schedule.csv"
        ),
        decisions=[
            Decision(
                stage="deliver",
                tool="splice_schedule",
                reason=(
                    "Schedule lists every connection per closure as cable/tube/fibre with "
                    "TIA-598-C colours, from the fiber_connection table."
                ),
                values=summary,
                source="solver",
            )
        ],
        data=summary,
    )
    s.project.commit(result, s.design)
    return result


def _fingerprint(design: Design) -> str:
    """A short id of the design's content: the same design always prints the same id, whatever
    was committed in between."""
    import hashlib

    h = hashlib.sha256()
    for f in sorted(design, key=lambda f: f.element_uuid):
        h.update(f.element_uuid.encode())
        h.update(f.geometry.wkb if f.geometry is not None else b"")
        h.update(json.dumps([f.props, f.openftth], sort_keys=True, default=str).encode())
    return h.hexdigest()[:10]


def plan_sheets(s: Session, scale: int = 1000, paper: str = "A3") -> ToolResult:
    """The printable plan set (plans.pdf: a key sheet and one sheet per grid cell with network)
    and the whole design as a CAD drawing (plans.dxf), in the twin's projected CRS."""
    from .deliver.sheets import PAPER_MM
    from .deliver.sheets import plan_sheets as _sheets

    s.project.set_stage("deliver")
    if paper not in PAPER_MM:
        return _rejected("plan_sheets", f"paper must be one of {', '.join(PAPER_MM)}")
    if not 100 <= int(scale) <= 100000:
        return _rejected("plan_sheets", "scale must be between 1:100 and 1:100000")
    twin = _twin(s)
    wo = _open_work_order(s) or next(reversed(_work_orders(s)), None)
    info = {
        "project": s.pid,
        "rulebook": s.rules.name,
        "version": _fingerprint(s.design),
        "work_order": f"{wo['id']} {wo['title']}" if wo else "",
        "status": "Draft: for review" if not wo or wo["state"] == "open" else "Posted",
    }
    d = s.project.deliver_dir
    try:
        out = _sheets(s.design, twin, d / "plans.pdf", d / "plans.dxf", info, int(scale), paper)
    except ValueError as exc:
        return _rejected("plan_sheets", str(exc))
    reason = (
        f"{out['sheets']} sheet(s) on {paper} at 1:{out['scale']:,} (a key plan at "
        f"1:{out['key_scale']:,} and a {out['grid'][0]}×{out['grid'][1]} grid with the empty cells "
        f"left out); {out['labels_placed']} labels placed"
        + (
            f", {out['labels_dropped']} left out where they would overlap another"
            if out["labels_dropped"]
            else ""
        )
        + f". The DXF holds the whole design in {out['crs']} metres, one layer per element kind."
    )
    result = ToolResult(
        tool="plan_sheets",
        summary=f"plans.pdf ({out['sheets']} sheets, {paper} 1:{out['scale']:,}) and plans.dxf",
        decisions=[
            Decision(
                stage="deliver", tool="plan_sheets", reason=reason, values=out, source="solver"
            )
        ],
        data=out,
    )
    s.project.commit(result, s.design)
    return result


def export_gpkg(s: Session) -> ToolResult:
    from .deliver.gpkg import export_gpkg as _export

    s.project.set_stage("deliver")
    info = _export(s.design, s.project.deliver_dir / "design.gpkg")
    result = ToolResult(
        tool="export_gpkg",
        summary=(
            f"design.gpkg with {len(info['layers'])} layers and {len(info['tables'])} tables "
            f"({info['bytes'] / 1e6:.1f} MB)"
        ),
        decisions=[
            Decision(
                stage="deliver",
                tool="export_gpkg",
                reason=(
                    "One GeoPackage layer per element type/placement in EPSG:4326, openFTTH "
                    "attributes as extra_json, side tables as attribute tables."
                ),
                values={"layers": info["layers"], "tables": info["tables"]},
                source="solver",
            )
        ],
        data=info,
    )
    s.project.commit(result, s.design)
    return result


SOLVE_CHAIN = (
    "cluster_homes",
    "place_fdh",
    "assign_pon",
    "route_feeder",
    "route_distribution",
    "route_drops",
    "place_chambers",
    "size_cables",
)
SOLVE_KINDS = ("fat", "fdh", "feeder", "distribution", "drop", "chamber")


def _run_chain(s: Session, time_limit_s: float) -> list[ToolResult]:
    out: list[ToolResult] = []
    for name in SOLVE_CHAIN:
        fn = globals()[name]
        out.append(fn(s, time_limit_s) if name in ("cluster_homes", "place_fdh") else fn(s))
    return out


def generate_scenarios(
    s: Session, n: int = 3, time_limit_s: float = 10.0, names: list[str] | None = None
) -> ToolResult:
    """Solve the current twin under rulebook variants and price each; nothing is applied.
    `names` picks the variants (e.g. the architectures); otherwise the first `n`."""
    import tempfile

    from .solve.scenarios import SCENARIOS, TWIN_SECTIONS, apply_overrides, resolve_overrides

    _twin(s)
    s.project.set_stage("solve")
    unknown = [x for x in names or [] if x not in SCENARIOS]
    if unknown:
        return _rejected(
            "generate_scenarios", f"no scenario {', '.join(unknown)}; known: {', '.join(SCENARIOS)}"
        )
    names = list(names) if names else list(SCENARIOS)[: max(1, n)]
    table: list[dict[str, Any]] = []
    for name in names:
        overrides = resolve_overrides(s.rules, SCENARIOS[name])
        variant = apply_overrides(s.rules, overrides)
        tmp = Path(tempfile.mkdtemp(prefix=f"openftth-scn-{name}-"))
        try:
            scratch = Session(
                project=Project(tmp, project_id=f"{s.pid}-{name}"),
                rules=variant,
                design=s.design.copy(),
                twin=s.twin,
                trees={},
                cache=dict(s.cache),
            )
            if any(getattr(variant, k) != getattr(s.rules, k) for k in TWIN_SECTIONS):
                # Route options change the street graph's prices: the variant gets its own twin
                # (same nodes, so every site and snapped home stays valid).
                scratch.twin = _finish_twin(scratch, _raw_twin(scratch))
            for kind in SOLVE_KINDS:
                scratch.design.remove_where(kind=kind)
            scratch.design.tables = {}
            results = _run_chain(scratch, time_limit_s)
            boq = build_boq(scratch.design, variant)
            trench = sum(
                line.qty
                for line in boq.lines
                if line.category == "civil" and line.item.startswith("trench")
            )
            violations, stats = run_all_checks(scratch.design, variant)
            civil = {line.item: line.qty for line in boq.lines if line.category == "civil"}
            table.append(
                {
                    "name": name,
                    "overrides": overrides,
                    "fats": len(scratch.design.by_kind("fat")),
                    "fdhs": len(scratch.design.by_kind("fdh")),
                    "trench_m": round(trench, 1),
                    "duct_m": round(civil.get("trench in existing_duct", 0.0), 1),
                    "facade_m": round(civil.get("facade cabling", 0.0), 1),
                    "aerial_m": round(civil.get("aerial span on existing poles", 0.0), 1),
                    "boq_total": boq.grand_total,
                    "cost_per_unit": boq.cost_per_unit,
                    "currency": boq.currency,
                    "violations": violation_summary(violations),
                    "worst_odn_db": stats.get("worst_db"),
                    "fallbacks": sum(
                        1 for r in results if any("fallback" in w for w in r.warnings)
                    ),
                }
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    (s.project.deliver_dir / "scenarios.json").write_text(_json(table), encoding="utf-8")
    best = min(table, key=lambda t: t["boq_total"])
    decisions = [
        Decision(
            stage="solve",
            tool="generate_scenarios",
            reason=(
                "Each scenario re-ran the placement and routing solvers on the same twin with the "
                "listed rulebook overrides and priced the result; the design was not changed. "
                "Use apply_scenario to adopt one."
            ),
            values={
                "scenarios": [
                    {
                        k: t[k]
                        for k in ("name", "overrides", "fats", "fdhs", "boq_total", "cost_per_unit")
                    }
                    for t in table
                ],
                "cheapest": best["name"],
            },
            source="solver",
        )
    ]
    result = ToolResult(
        tool="generate_scenarios",
        summary=" | ".join(
            f"{t['name']}: {t['fats']} FATs, {t['fdhs']} FDHs, "
            f"{t['boq_total']:,.0f} {t['currency']} ({t['cost_per_unit']:,.0f}/unit)"
            for t in table
        ),
        decisions=decisions,
        data={"scenarios": table, "cheapest": best["name"]},
    )
    s.project.commit(result, s.design)
    return result


def apply_scenario(s: Session, name: str, time_limit_s: float = WORK_LIMIT) -> ToolResult:
    """Adopt a scenario: apply its rulebook overrides, then re-run the solve chain."""
    from .solve.scenarios import SCENARIOS, resolve_overrides

    if name not in SCENARIOS:
        return ToolResult(
            tool="apply_scenario",
            summary=f"unknown scenario {name!r}",
            warnings=[f"known: {', '.join(SCENARIOS)}"],
        )
    overrides = resolve_overrides(s.rules, SCENARIOS[name])
    applied = [set_rule(s, path, value) for path, value in overrides.items()]
    results = _run_chain(s, time_limit_s)
    results.append(assign_fibers(s))
    results.append(validate_design(s))
    summary = "; ".join(
        r.summary for r in results if r.tool in ("cluster_homes", "place_fdh", "check_rules")
    )
    return ToolResult(
        tool="apply_scenario",
        summary=f"{name} applied: {summary}",
        decisions=[d for r in applied + results for d in r.decisions],
        warnings=[w for r in results for w in r.warnings],
        version=results[-1].version,
        data={"overrides": overrides, "steps": [r.tool for r in applied + results]},
    )


# ---- Manual steering ----------------------------------------------------------------------
# The engineer's own changes. Each one is an ordinary tool call: ids and a reason in, the
# snapped numbers out, every dependent solver run again, and the lot recorded as one version
# with a `source="user"` decision. The crew re-validates afterwards; nothing here bypasses a
# check. The app calls these through the harness; they are on no crew member's allow-list.

MOVABLE_KINDS = ("pop", "fdh", "fat")

# What a moved site, an excluded building or a no-dig street invalidates, in run order. Only
# the tools whose output already exists run again, so a steer before routing is just the edit.
# Chambers come from the cached route trees, so both routing tools always precede them.
RERUN_CHAIN = (
    "route_feeder",
    "route_distribution",
    "route_drops",
    "place_chambers",
    "size_cables",
    "assign_fibers",
)


def _ran(s: Session, tool: str) -> bool:
    """Whether a solver's output is in the design, so a steer has to run it again."""
    d = s.design
    if tool == "route_feeder":
        return bool(d.by_kind("feeder"))
    if tool == "route_distribution":
        return bool(d.by_kind("distribution"))
    if tool == "route_drops":
        return bool(d.by_kind("drop")) or any(
            "path_to_pop_m" in h.openftth for h in d.by_kind("home")
        )
    if tool == "place_chambers":
        return bool(d.by_kind("chamber"))
    if tool == "size_cables":
        return any("tube_count" in c.props for c in d.by_kind("feeder") + d.by_kind("distribution"))
    if tool == "assign_fibers":
        return any("fibers_used" in c.openftth for c in d.by_kind("distribution"))
    return False


def _rerun(s: Session, chain: tuple[str, ...] = RERUN_CHAIN) -> list[ToolResult]:
    """Run the solvers whose output exists again, in order, collecting their results instead
    of committing each one; the caller records everything as one version."""
    todo = [name for name in chain if _ran(s, name)]
    s.deferred = []
    try:
        for name in todo:
            globals()[name](s)
    finally:
        out, s.deferred = s.deferred, None
    return out


def _merge(result: ToolResult, sub: list[ToolResult]) -> ToolResult:
    """Fold the re-run solvers' decisions and warnings into the steering tool's result."""
    for r in sub:
        result.decisions.extend(r.decisions)
        result.warnings.extend(r.warnings)
    if sub:
        result.summary += "; re-ran " + ", ".join(r.tool for r in sub)
    result.data["reran"] = [r.tool for r in sub]
    return result


def _rejected(tool: str, why: str) -> ToolResult:
    return ToolResult(tool=tool, summary=f"rejected: {why}", warnings=[why])


def _to_metric(twin: Twin, lon: float, lat: float) -> Point:
    return gpd.GeoSeries([Point(lon, lat)], crs=4326).to_crs(twin.crs).iloc[0]


def _label(f: Feature) -> str:
    return str(f.props.get("name") or f"{f.kind} {f.element_uuid[:8]}")


def move_site(s: Session, element_uuid: str, lon: float, lat: float, reason: str) -> ToolResult:
    """The engineer moves the POP, an FDH or an outdoor FAT. The point snaps to the nearest
    routable street node; the site keeps its id and its connections; everything routed from
    it is routed and sized again, as one version with the reason on record."""
    reason = (reason or "").strip()
    if not reason:
        return _rejected("move_site", "a reason is required; every manual move records why")
    if element_uuid not in s.design:
        return _rejected("move_site", f"no element {element_uuid} in the design")
    f = s.design.get(element_uuid)
    kind = f.kind
    if kind not in MOVABLE_KINDS:
        return _rejected(
            "move_site",
            f"a {kind} cannot be moved by hand; only the POP, cabinets (FDH) and outdoor FATs can",
        )
    if kind == "fat" and f.openftth.get("mdu"):
        return _rejected(
            "move_site",
            "an indoor FAT sits in its building; exclude the building or change fat.max_homes instead",
        )
    s.project.set_stage("solve")
    twin = _twin(s)
    before = s.design.copy()
    name = _label(f)
    old_node = int(f.openftth["node"])
    p = _to_metric(twin, lon, lat)
    node, snap_m = twin.nearest_node(p.x, p.y)
    if int(node) == old_node:
        return _rejected(
            "move_site", f"{name} already stands on the street node nearest to that point"
        )
    moved_m = float(twin.node_point(old_node).distance(twin.node_point(node)))
    f.geometry = _wgs(twin, twin.node_point(node))
    f.openftth["node"] = int(node)
    f.openftth["placed_by"] = "engineer"
    f.openftth["placed_reason"] = reason
    f.openftth["provenance"] = {
        **f.openftth.get("provenance", {}),
        "source": "user",
        "updated_by": "move_site",
    }
    if kind == "pop":
        s.cache["pop_node"] = int(node)
    decisions = [
        Decision(
            stage="solve",
            tool="move_site",
            element=element_uuid,
            reason=(
                f"{name} moved {moved_m:.0f} m by the engineer: {reason}. "
                f"The point was snapped {snap_m:.0f} m to the nearest routable street node."
            ),
            values={
                "moved_m": round(moved_m, 1),
                "snap_m": round(snap_m, 1),
                "node": int(node),
                "from_node": old_node,
            },
            source="user",
        )
    ]
    warnings: list[str] = []
    if kind == "fat":
        fdh_id = f.openftth.get("fdh_id")
        if fdh_id and fdh_id in s.design:
            fdh_node = int(s.design.get(fdh_id).openftth["node"])
            try:
                dist = float(
                    nx.shortest_path_length(twin.graph, int(node), fdh_node, weight="length_m")
                )
            except (nx.NetworkXNoPath, nx.NodeNotFound):
                dist = math.inf
            limit = s.rules.distances.max_dist_m
            if dist > limit:
                warnings.append(
                    f"{name} is now {dist:.0f} m along the street from its cabinet, over max_dist_m ({limit:g} m); the Checker will flag it"
                )
    sub = _rerun(s)
    result = _merge(
        ToolResult(
            tool="move_site",
            summary=f"{name} moved {moved_m:.0f} m to street node {node}",
            decisions=decisions,
            warnings=warnings,
            data={
                "element": element_uuid,
                "kind": kind,
                "node": int(node),
                "moved_m": round(moved_m, 1),
                "snap_m": round(snap_m, 1),
            },
        ),
        sub,
    )
    return _commit(s, result, before)


def exclude_building(s: Session, element_uuid: str, reason: str) -> ToolResult:
    """The engineer takes a building out of the design (not a dwelling, out of scope, will
    not take the service). Its homes and drops go, its FAT and cabinet lose the units, and
    the exclusion is kept with the project inputs so a re-import or a twin rebuild honours it."""
    reason = (reason or "").strip()
    if not reason:
        return _rejected("exclude_building", "a reason is required")
    if element_uuid not in s.design:
        return _rejected("exclude_building", f"no element {element_uuid} in the design")
    b = s.design.get(element_uuid)
    if b.kind != "building":
        return _rejected("exclude_building", f"{element_uuid} is a {b.kind}, not a building")
    if b.openftth.get("excluded"):
        return _rejected("exclude_building", "that building is already excluded")
    s.project.set_stage("solve")
    before = s.design.copy()
    osm_id = str(b.openftth.get("osm_id", ""))
    name = f"building {osm_id}"
    homes = [h for h in s.design.by_kind("home") if h.openftth.get("building") == element_uuid]
    units = sum(int(h.openftth.get("units", 0)) for h in homes)
    b.openftth["excluded"] = True
    b.openftth["excluded_reason"] = reason
    s.project.meta.setdefault("inputs", {})["excluded_buildings"] = _steering(
        s, "excluded_buildings"
    ) + [{"osm_id": osm_id, "element": element_uuid, "reason": reason}]

    def keep(df: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
        col = "building_id" if "building_id" in df.columns else "osm_id"
        return df[df[col].astype(str) != osm_id].copy()

    if s.twin is not None and not s.twin.homes.empty:
        s.twin.homes = keep(s.twin.homes)
    hw = s.cache.get("homes_wgs")
    if hw is not None and not hw.empty:
        s.cache["homes_wgs"] = keep(hw)

    # Detach the homes: a dedicated indoor FAT goes with its building, a shared FAT loses
    # the units, and so does the cabinet above it. Ids of everything that stays are unchanged.
    fats_removed = 0
    fats_reduced: set[str] = set()
    for h in homes:
        fat_ids = [x for x in (h.openftth.get("fat_ids") or [h.openftth.get("fat_id")]) if x]
        for fid in fat_ids:
            if fid not in s.design:
                continue
            fat = s.design.get(fid)
            fdh_id = fat.openftth.get("fdh_id")
            fdh = s.design.get(fdh_id) if fdh_id and fdh_id in s.design else None
            if fat.openftth.get("mdu"):
                lost = int(fat.openftth.get("units", 0))
                s.design.remove(fid)
                fats_removed += 1
            else:
                lost = int(h.openftth.get("units", 0))
                left = max(0, int(fat.openftth.get("units", 0)) - lost)
                fat.openftth["units"] = left
                fat.props["total_connections"] = left
                fat.props["reserve_capacity"] = int(fat.props.get("port_capacity", 0)) - left
                if left == 0:
                    s.design.remove(fid)
                    fats_removed += 1
                else:
                    fats_reduced.add(fid)
            if fdh is not None:
                if fid not in s.design:
                    fdh.openftth["fats"] = max(0, int(fdh.openftth.get("fats", 0)) - 1)
                left = max(0, int(fdh.openftth.get("units", 0)) - lost)
                fdh.openftth["units"] = left
                fdh.props["total_connections"] = left
                fdh.props["reserve_capacity"] = int(fdh.props.get("port_capacity", 0)) - left
        s.design.remove_where(kind="drop", home=h.element_uuid)
        s.design.remove(h.element_uuid)

    decisions = [
        Decision(
            stage="solve",
            tool="exclude_building",
            element=element_uuid,
            reason=(
                f"{name} excluded by the engineer: {reason}. "
                f"{len(homes)} home(s) with {units} unit(s) left the design; "
                f"{fats_removed} FAT(s) removed, {len(fats_reduced)} reduced."
            ),
            values={
                "homes": len(homes),
                "units": units,
                "fats_removed": fats_removed,
                "fats_reduced": len(fats_reduced),
            },
            source="user",
        )
    ]
    warnings: list[str] = []
    empty = [_label(x) for x in s.design.by_kind("fdh") if int(x.openftth.get("units", 0)) == 0]
    if empty:
        warnings.append(
            f"{', '.join(empty)} now serve(s) nothing; ask the Planner to place cabinets again"
        )
    sub = _rerun(s) if homes else []
    result = _merge(
        ToolResult(
            tool="exclude_building",
            summary=f"{name} excluded: {len(homes)} home(s), {units} unit(s) removed",
            decisions=decisions,
            warnings=warnings,
            data={
                "element": element_uuid,
                "osm_id": osm_id,
                "homes": len(homes),
                "units": units,
                "fats_removed": fats_removed,
            },
        ),
        sub,
    )
    return _commit(s, result, before)


def forbid_street(s: Session, osm_id: str, reason: str) -> ToolResult:
    """The engineer marks a street as no-dig (fresh asphalt, a pending permit, a private
    road). Its ways leave the route graph for good: the list is kept with the project inputs,
    sites and homes that stood on it move to the nearest remaining street node, and every
    route is planned again around it, as one version."""
    reason = (reason or "").strip()
    osm_id = str(osm_id or "").strip()
    if not reason:
        return _rejected("forbid_street", "a reason is required")
    if not osm_id:
        return _rejected("forbid_street", "an OSM way id is required")
    if osm_id in _no_dig_ids(s):
        return _rejected("forbid_street", f"way {osm_id} is already on the no-dig list")
    twin = _twin(s)
    rows = twin.streets[twin.streets["osm_id"].astype(str) == osm_id]
    if rows.empty:
        return _rejected("forbid_street", f"no street with OSM id {osm_id} in the twin")
    s.project.set_stage("solve")
    before = s.design.copy()
    highway = str(rows["highway"].iloc[0])
    s.project.meta.setdefault("inputs", {})["no_dig"] = _steering(s, "no_dig") + [
        {"osm_id": osm_id, "reason": reason}
    ]
    edges, length_m = twin.forbid_ways(_no_dig_ids(s))
    twin.resnap_homes()

    # Whatever stood on a node that left the graph moves to the nearest remaining one: a
    # site moves bodily (it sits on the street), a home only changes its street node.
    resnapped = 0
    sited = (
        s.design.by_kind("pop")
        + s.design.by_kind("fdh")
        + s.design.by_kind("fat")
        + s.design.by_kind("home")
    )
    for f in sited:
        node = int(f.openftth["node"])
        if node in twin.graph:
            continue
        c = f.geometry.representative_point()
        p = _to_metric(twin, c.x, c.y)
        new, d = twin.nearest_node(p.x, p.y)
        f.openftth["node"] = int(new)
        if f.kind == "home":
            f.openftth["offset_m"] = round(float(d), 1)
        elif not f.openftth.get("mdu"):
            f.geometry = _wgs(twin, twin.node_point(new))
        if f.kind == "pop":
            s.cache["pop_node"] = int(new)
        resnapped += 1
    _write_streets(s, twin)

    decisions = [
        Decision(
            stage="twin",
            tool="forbid_street",
            reason=(
                f"{highway} way {osm_id} marked no-dig by the engineer: {reason}. "
                f"{edges} graph edge(s), {length_m:.0f} m, left the route graph"
                + (
                    f"; {resnapped} element(s) moved to the nearest remaining street node."
                    if resnapped
                    else "."
                )
            ),
            values={
                "osm_id": osm_id,
                "edges": edges,
                "length_m": round(length_m, 1),
                "resnapped": resnapped,
            },
            source="user",
        )
    ]
    sub = _rerun(s)
    result = _merge(
        ToolResult(
            tool="forbid_street",
            summary=f"{highway} {osm_id} is no-dig: {length_m:.0f} m of street left the route graph",
            decisions=decisions,
            data={
                "osm_id": osm_id,
                "highway": highway,
                "edges": edges,
                "length_m": round(length_m, 1),
                "resnapped": resnapped,
            },
        ),
        sub,
    )
    return _commit(s, result, before)


# ---- Network of record --------------------------------------------------------------------
# Operator pack, lifecycle, work orders and fibre trace (docs/PLAN-NETWORK.md, phase N1).
# A work order records the network of record it started from; everything committed after it
# opened belongs to it; posting moves the `refs/network/record` pointer to the new version
# after the operator checks and the rule checks pass, and needs the engineer's approval.


def get_operator_pack(s: Session) -> ToolResult:
    pack = _pack(s)
    return ToolResult(
        tool="get_operator_pack",
        summary=f"operator pack {pack.name}: {len(pack.objects)} object rules",
        data=pack.model_dump(),
    )


def set_operator_pack(s: Session, name: str) -> ToolResult:
    try:
        pack = load_operator_pack(name)
    except (FileNotFoundError, ValueError) as exc:
        return _rejected("set_operator_pack", str(exc))
    s.project.meta["operator"] = name
    s.cache.pop("operator_pack_name", None)
    return _commit(
        s,
        ToolResult(
            tool="set_operator_pack",
            summary=f"operator pack set to {pack.name}",
            decisions=[
                Decision(
                    stage="rules",
                    tool="set_operator_pack",
                    reason=f"Objects are named, recorded and accepted by the {pack.name} operator pack: {pack.description}",
                    values={"operator": pack.name},
                    source="user",
                )
            ],
            data={"operator": pack.name},
        ),
        s.design.copy(),
    )


def check_operator_tool(s: Session) -> ToolResult:
    pack = _pack(s)
    violations = check_operator(s.design, pack)
    errors = [v for v in violations if v.severity == "error"]
    by_rule: dict[str, int] = {}
    for v in violations:
        by_rule[v.rule] = by_rule.get(v.rule, 0) + 1
    msg = (
        "no violations"
        if not violations
        else ", ".join(f"{k}: {n}" for k, n in sorted(by_rule.items()))
    )
    return ToolResult(
        tool="check_operator",
        summary=f"operator pack {pack.name}: {msg}",
        warnings=[f"{v.severity}: {v.message}" for v in violations[:10]],
        data={
            "operator": pack.name,
            "errors": len(errors),
            "warnings": len(violations) - len(errors),
            "by_rule": by_rule,
            "violations": [v.to_dict() for v in violations[:200]],
            "design_version": s.project.head(),
        },
    )


def set_lifecycle(
    s: Session,
    state: str,
    reason: str,
    element_uuids: list[str] | None = None,
    kinds: list[str] | None = None,
) -> ToolResult:
    """Move objects to a new lifecycle state (for example in_construction when the crew starts
    digging, as_built when the field reports it done). Every transition must be allowed by
    the operator pack; nothing changes if one is not."""
    reason = (reason or "").strip()
    if not reason:
        return _rejected("set_lifecycle", "a reason is required")
    if not element_uuids and not kinds:
        return _rejected("set_lifecycle", "name the objects by element_uuids or kinds")
    pack = _pack(s)
    missing = [u for u in (element_uuids or []) if u not in s.design]
    if missing:
        return _rejected("set_lifecycle", f"unknown element(s): {', '.join(missing[:5])}")
    targets = [s.design.get(u) for u in (element_uuids or [])]
    for k in kinds or []:
        targets.extend(s.design.by_kind(k))
    unique = {f.element_uuid: f for f in targets if f.kind != "service_area"}
    targets = [unique[u] for u in sorted(unique)]
    if not targets:
        return _rejected("set_lifecycle", "no objects of those kinds in the design")
    blocked = [
        f
        for f in targets
        if f.openftth.get("lifecycle") != state
        and not pack.lifecycle.allows(
            str(f.openftth.get("lifecycle", pack.lifecycle.initial)), state
        )
    ]
    if blocked:
        f = blocked[0]
        allowed = pack.lifecycle.transitions.get(str(f.openftth.get("lifecycle")), [])
        return _rejected(
            "set_lifecycle",
            f"{len(blocked)} object(s) cannot go to {state}; e.g. {_label(f)} is "
            f"{f.openftth.get('lifecycle')} and may go to {allowed or 'nothing'}",
        )
    before = s.design.copy()
    changed = 0
    by_kind: dict[str, int] = {}
    for f in targets:
        by_kind[f.kind] = by_kind.get(f.kind, 0) + 1
        if f.openftth.get("lifecycle") != state:
            f.openftth["lifecycle"] = state
            changed += 1
    return _commit(
        s,
        ToolResult(
            tool="set_lifecycle",
            summary=f"{changed} object(s) now {state}",
            decisions=[
                Decision(
                    stage="deliver",
                    tool="set_lifecycle",
                    reason=f"{changed} object(s) moved to {state} by the engineer: {reason}.",
                    values={"state": state, "objects": by_kind},
                    source="user",
                )
            ],
            data={"state": state, "changed": changed, "objects": by_kind},
        ),
        before,
    )


def _work_orders(s: Session) -> list[dict[str, Any]]:
    return s.project.meta.setdefault("work_orders", [])


def _open_work_order(s: Session) -> dict[str, Any] | None:
    return next((w for w in _work_orders(s) if w["state"] == "open"), None)


def open_work_order(s: Session, title: str) -> ToolResult:
    title = (title or "").strip()
    if not title:
        return _rejected("open_work_order", "a title is required")
    current = _open_work_order(s)
    if current:
        return _rejected(
            "open_work_order", f"{current['id']} ({current['title']}) is still open; post it first"
        )
    wo = {
        "id": f"WO-{len(_work_orders(s)) + 1:04d}",
        "title": title,
        "state": "open",
        "base": s.project.record(),
        "opened_at": s.project.head(),
    }
    _work_orders(s).append(wo)
    return _commit(
        s,
        ToolResult(
            tool="open_work_order",
            summary=f"{wo['id']} opened: {title}",
            decisions=[
                Decision(
                    stage="solve",
                    tool="open_work_order",
                    reason=f"Changes from here on belong to {wo['id']} ({title}) until it is posted to the network of record.",
                    values={"work_order": wo["id"], "base": wo["base"]},
                    source="user",
                )
            ],
            data=dict(wo),
        ),
        s.design.copy(),
    )


def _kind_diff(old: Design, new: Design) -> dict[str, dict[str, int]]:
    diff = old.diff(new)
    out: dict[str, dict[str, int]] = {}
    for key, ids, design in (
        ("added", diff.added, new),
        ("removed", diff.removed, old),
        ("modified", diff.modified, new),
    ):
        for u in ids:
            if u.startswith("table:"):
                kind = u.split(":")[1]
            else:
                kind = design.get(u).kind if u in design else "unknown"
            out.setdefault(kind, {"added": 0, "removed": 0, "modified": 0})[key] += 1
    return out


def _record_design(s: Session) -> tuple[str | None, Design]:
    record = s.project.record()
    return record, (s.project.design_at(record) if record else Design(s.design.metadata))


def work_order_status(s: Session) -> ToolResult:
    wo = _open_work_order(s)
    record, base = _record_design(s)
    changes = _kind_diff(base, s.design)
    totals = {k: sum(v[k] for v in changes.values()) for k in ("added", "removed", "modified")}
    states: dict[str, int] = {}
    for f in s.design:
        st = str(f.openftth.get("lifecycle", "unset"))
        states[st] = states.get(st, 0) + 1
    what = f"{wo['id']} open ({wo['title']})" if wo else "no work order open"
    return ToolResult(
        tool="work_order_status",
        summary=f"{what}; network of record {record[:8] if record else 'empty'}; "
        f"{totals['added']} added, {totals['removed']} removed, {totals['modified']} modified since",
        data={
            "work_order": wo,
            "network_of_record": record,
            "head": s.project.head(),
            "changes_by_kind": changes,
            "totals": totals,
            "lifecycle": states,
            "work_orders": _work_orders(s),
        },
    )


def post_work_order(s: Session) -> ToolResult:
    """Post the open work order to the network of record. The MCP layer requires the
    engineer's approval; here the operator pack and the design rules must pass with no errors."""
    wo = _open_work_order(s)
    if wo is None:
        return _rejected("post_work_order", "no work order is open")
    record, base = _record_design(s)
    if base.diff(s.design).is_empty():
        return _rejected("post_work_order", f"{wo['id']} changes nothing in the network of record")
    op = [v for v in check_operator(s.design, _pack(s)) if v.severity == "error"]
    rule_violations, _stats = run_all_checks(s.design, s.rules)
    rule_errors = [v for v in rule_violations if v.severity == "error"]
    if op or rule_errors:
        first = op[0].message if op else rule_errors[0].message
        return _rejected(
            "post_work_order",
            f"{len(op)} operator and {len(rule_errors)} design rule error(s) must be fixed "
            f"first; e.g. {first}",
        )
    changes = _kind_diff(base, s.design)
    wo["state"] = "posted"
    wo["posted_from"] = s.project.head()
    result = _commit(
        s,
        ToolResult(
            tool="post_work_order",
            summary=f"{wo['id']} posted to the network of record ({wo['title']})",
            decisions=[
                Decision(
                    stage="deliver",
                    tool="post_work_order",
                    reason=f"{wo['id']} ({wo['title']}) became the network of record after the engineer's approval; "
                    f"operator pack {_pack(s).name} and the design rules passed with no errors.",
                    values={"work_order": wo["id"], "changes": changes},
                    source="user",
                )
            ],
            data={"work_order": wo["id"], "changes_by_kind": changes, "previous_record": record},
        ),
        s.design.copy(),
    )
    if result.version:
        s.project.set_record(result.version)
    result.data["network_of_record"] = result.version
    return result


def trace_fibre(s: Session, element_uuid: str) -> ToolResult:
    """Trace a home's fibre (or the home behind a drop) from the OLT port to the home."""
    if element_uuid not in s.design:
        return _rejected("trace_fibre", f"no element {element_uuid} in the design")
    f = s.design.get(element_uuid)
    home_id = element_uuid if f.kind == "home" else f.openftth.get("home")
    if f.kind not in ("home", "drop") or not home_id:
        return _rejected(
            "trace_fibre", f"a {f.kind} carries many fibres; trace a home, or ask homes_behind"
        )
    out = trace_home(s.design, s.rules, home_id)
    steps = out.get("steps", [])
    names = " → ".join(str(x.get("name")) for x in steps)
    home_name = out.get("home", {}).get("name", "home")
    return ToolResult(
        tool="trace_fibre",
        summary=(
            f"{home_name}: OLT port {out.get('olt_port')} → {names}"
            if out.get("reaches_olt")
            else f"no path from this home to an OLT port ({out.get('reason', 'path ends before the POP')})"
        ),
        warnings=[] if out.get("reaches_olt") else ["the home is not connected to the OLT"],
        data=out,
    )


def homes_behind_tool(s: Session, element_uuid: str) -> ToolResult:
    if element_uuid not in s.design:
        return _rejected("homes_behind", f"no element {element_uuid} in the design")
    homes = homes_behind(s.design, element_uuid)
    units = sum(int(s.design.get(h).openftth.get("units", 1)) for h in homes)
    name = _label(s.design.get(element_uuid))
    return ToolResult(
        tool="homes_behind",
        summary=f"{len(homes)} home(s), {units} unit(s) depend on {name}",
        data={
            "element": element_uuid,
            "homes": len(homes),
            "units": units,
            "home_ids": homes[:500],
        },
    )


# ---- Inputs: quality and download -----------------------------------------------------------


def check_inputs_tool(s: Session) -> ToolResult:
    """What the project's inputs hold and what will make the design wrong: read-only."""
    from .ingest.quality import check_inputs

    inp = s.cache.get("inputs")
    if inp is None:
        profile = _load_profile(s)
        if profile is None:
            return _rejected("check_inputs", "no data imported yet")
        inp = load_inputs(profile, s.rules)
        s.cache["inputs"] = inp
    report = check_inputs(inp, s.rules)
    n = len(report["issues"])
    return ToolResult(
        tool="check_inputs",
        summary=f"inputs {report['level']}: {n} finding(s); "
        + ", ".join(f"{k} {v}" for k, v in report["counts"].items() if v),
        warnings=[f"{i['level']}: {i['message']}" for i in report["issues"]],
        data=report,
    )


def fetch_osm_tool(s: Session, area_path: str) -> ToolResult:
    """Download an OpenStreetMap extract for the area into the project (a network call)."""
    from .ingest.fetch import fetch_osm

    out = s.project.dir / "sources" / f"{Path(area_path).stem}.osm"
    path = fetch_osm(area_path, out)
    size_kb = path.stat().st_size // 1024
    return ToolResult(
        tool="fetch_osm",
        summary=f"OpenStreetMap extract saved: {path.name} ({size_kb} kB)",
        decisions=[
            Decision(
                stage="ingest",
                tool="fetch_osm",
                reason="Streets and buildings downloaded from OpenStreetMap for the area (public "
                "Overpass API), because no local extract was given.",
                values={"path": str(path), "kb": size_kb},
                source="user",
            )
        ],
        data={"path": str(path)},
    )
