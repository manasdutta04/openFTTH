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
from shapely.geometry import LineString, Point, mapping

from .deliver import build_boq, render_map
from .deliver.boq import boq_to_csv
from .ingest import homes_from_buildings, import_osm, load_area
from .interchange import Design, Feature, Metadata, stable_uuid
from .project import Project
from .results import Decision, ToolResult
from .rules import Rulebook
from .solve.cluster import Demand, capacitated_sites, repair_reach
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


def _wgs(twin: Twin, geom) -> Any:
    return gpd.GeoSeries([geom], crs=twin.crs).to_crs(4326).iloc[0]


def _commit(s: Session, result: ToolResult, before: Design | None = None) -> ToolResult:
    if before is not None:
        result.changes = before.diff(s.design).summary()
    s.project.commit(result, s.design)
    return result


# ---- Ingest / Twin ------------------------------------------------------------------------


def import_area_and_osm(s: Session, area_path: str | Path, osm_path: str | Path) -> ToolResult:
    s.project.set_stage("ingest")
    before = s.design.copy()
    area = load_area(area_path)
    osm = import_osm(osm_path, area)
    homes_wgs, decisions = homes_from_buildings(osm.buildings, s.rules)

    s.design.remove_where(kind="building")
    s.design.remove_where(kind="service_area")
    area_series = gpd.GeoSeries([area], crs=4326)
    area_m2 = float(area_series.to_crs(area_series.estimate_utm_crs()).area.iloc[0])
    s.design.add(
        Feature(
            element_uuid=s.uid("service_area", "area"),
            element_type="service_area",
            geometry=area,
            props={"name": Path(area_path).stem, "area_m2": round(area_m2, 1)},
            openftth={"kind": "service_area"},
        )
    )
    for b in osm.buildings.itertuples(index=False):
        s.design.add(
            Feature(
                element_uuid=s.uid("building", b.osm_id),
                element_type="building",
                geometry=b.geometry,
                props={
                    "building_type": str(b.building or "yes"),
                    "street": b.street if isinstance(b.street, str) else None,
                    "street_number": b.housenumber if isinstance(b.housenumber, str) else None,
                },
                openftth={"kind": "building", "osm_id": b.osm_id},
            )
        )
    s.cache["osm"] = osm
    s.cache["homes_wgs"] = homes_wgs
    s.cache["area"] = area
    s.project.meta["inputs"] = {
        "area": str(Path(area_path).resolve()),
        "osm": str(Path(osm_path).resolve()),
    }
    result = ToolResult(
        tool="import_osm",
        summary=f"{len(osm.streets)} street segments, {len(osm.buildings)} buildings, {len(homes_wgs)} synthetic homes ({int(homes_wgs['units'].sum()) if not homes_wgs.empty else 0} units)",
        decisions=decisions,
        data={
            "streets": len(osm.streets),
            "buildings": len(osm.buildings),
            "homes": len(homes_wgs),
        },
    )
    return _commit(s, result, before)


def build_twin_tool(s: Session) -> ToolResult:
    s.project.set_stage("twin")
    before = s.design.copy()
    osm = s.cache["osm"]
    twin = build_twin(osm.streets, s.cache["homes_wgs"], s.rules)
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
                    "building": s.uid("building", h.osm_id),
                },
            )
        )
    # Twin streets (openFTTH-specific layer) for the renderer.
    streets = twin.streets[["highway", "surface", "cost_per_m", "osm_id", "geometry"]].to_crs(4326)
    streets.to_file(s.project.twin_dir / "streets.geojson", driver="GeoJSON")
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


def set_pop(s: Session, lon: float | None = None, lat: float | None = None) -> ToolResult:
    s.project.set_stage("solve")
    twin = _twin(s)
    before = s.design.copy()
    decisions: list[Decision] = []
    if lon is None or lat is None:
        area = s.cache.get("area") or s.design.by_kind("service_area")[0].geometry
        c = gpd.GeoSeries([area.centroid], crs=4326).to_crs(twin.crs).iloc[0]
        node, _ = twin.nearest_node(c.x, c.y)
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


def cluster_homes(s: Session, time_limit_s: float = 20.0) -> ToolResult:
    """Place FATs: capacitated clustering of homes within max_drop_m street distance."""
    s.project.set_stage("solve")
    twin = _twin(s)
    r = s.rules
    before = s.design.copy()
    homes = s.design.by_kind("home")
    cap = r.fat.max_homes
    s.design.remove_where(kind="fat")
    fat_no = 0

    def new_fat(node: int, units: int, key: str, placement: str, geometry) -> str:
        nonlocal fat_no
        fat_no += 1
        fid = s.uid("tb", key)
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
    fat_candidates = {n for n, deg in twin.graph.degree if deg >= 3} | {
        int(h.openftth["node"]) for h in regular
    }
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
        twin.graph,
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
    )
    home_by_id = {h.element_uuid: h for h in regular}
    for site in sites:
        fid = new_fat(
            site.node,
            site.units,
            f"fat:{site.node}:{site.slot}",
            "outdoor",
            _wgs(twin, twin.node_point(site.node)),
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
    warnings = (
        [f"{stats.fallback_cells} cell(s) hit the time limit; greedy fallback used"]
        if stats.fallback_cells
        else []
    )
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


def place_fdh(s: Session, time_limit_s: float = 20.0) -> ToolResult:
    """Place FDHs (splitter cabinets) over the FATs, capacity in homes served."""
    s.project.set_stage("solve")
    twin = _twin(s)
    r = s.rules
    before = s.design.copy()
    fats = s.design.by_kind("fat")
    demands = [
        Demand(key=f.element_uuid, node=int(f.openftth["node"]), units=int(f.openftth["units"]))
        for f in fats
    ]
    cap_max = max(r.fdh.capacities)
    capacity = int(cap_max * r.fdh.max_fill_pct / 100.0)
    # Cabinets sit at intersections: candidate nodes with degree ≥ 3 (fallback: all nodes).
    candidates = {n for n, d in twin.graph.degree if d >= 3} or set(twin.graph.nodes)
    # Distance is priced as distribution cable only: the trench along the street is needed for
    # the drops anyway, so it must not push the solver towards many small cabinets.
    per_m = r.units.cable_base_per_m + 24 * r.units.cable_per_fiber_m
    sites, stats = capacitated_sites(
        twin.graph,
        twin.xy,
        demands,
        capacity=capacity,
        max_dist_m=r.distances.max_dist_m * FDH_REACH_FACTOR,
        open_cost=r.units.fdh_288 + r.labour.fdh_install,
        per_m_cost=per_m,
        candidate_nodes=candidates,
        time_limit_s=time_limit_s,
        k_nearest=1000,
    )
    # The clustering prices distance to the nearest candidate, so a FAT can end up on a cabinet
    # that is within reach as the crow flies but not along the street: check and move those.
    sites, moved, stranded = repair_reach(twin.graph, sites, r.distances.max_dist_m, capacity)
    s.design.remove_where(kind="fdh")
    fat_by_id = {f.element_uuid: f for f in fats}
    for i, site in enumerate(sites):
        chosen = next(
            (c for c in sorted(r.fdh.capacities) if site.units <= c * r.fdh.max_fill_pct / 100.0),
            cap_max,
        )
        fid = s.uid("otb", f"fdh:{site.node}")
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
            reason=f"FDHs placed by capacitated clustering of FATs (CP-SAT, status {','.join(stats.status)}) at street intersections, ≤ {capacity} units per cabinet ({r.fdh.max_fill_pct:g}% of {cap_max}), each FAT within {r.distances.max_dist_m:g} m; cabinet size then shrunk to the smallest that fits.",
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
    return _commit(
        s,
        ToolResult(
            tool="place_fdh",
            summary=f"{len(sites)} FDH(s): " + ", ".join(f"{x.units} units" for x in sites),
            decisions=decisions,
            data={"fdhs": len(sites)},
        ),
        before,
    )


def assign_pon(s: Session) -> ToolResult:
    """Group homes under each FDH into PONs of ≤ split_ratio units (one splitter each)."""
    s.project.set_stage("solve")
    before = s.design.copy()
    r = s.rules
    homes = s.design.by_kind("home")
    fats = {f.element_uuid: f for f in s.design.by_kind("fat")}
    n_pon = 0
    for fdh in s.design.by_kind("fdh"):
        my_fats = sorted(
            (f for f in fats.values() if f.openftth.get("fdh_id") == fdh.element_uuid),
            key=lambda f: f.openftth.get("distribution_est_m", 0.0),
        )
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
            if load and load + units > r.pon.split_ratio:
                pon_idx += 1
                load = 0
                pon_id = f"{fdh.props['name']}-PON-{pon_idx + 1:02d}"
            fat.openftth["pon_id"] = pon_id
            for h in fat_homes:
                h.openftth["pon_id"] = pon_id
            load += units
        fdh.openftth["pons"] = pon_idx + 1 if my_fats else 0
        n_pon += fdh.openftth["pons"]
    decisions = [
        Decision(
            stage="solve",
            tool="assign_pon",
            reason=f"Centralised split: whole FATs are packed into 1:{r.pon.split_ratio} PONs per FDH in order of distance, never splitting a FAT across two splitters (one 1:{r.pon.split_ratio} splitter per PON in the FDH).",
            values={"pons": n_pon},
            source="rulebook",
        )
    ]
    return _commit(
        s,
        ToolResult(
            tool="assign_pon",
            summary=f"{n_pon} PONs (1:{r.pon.split_ratio})",
            decisions=decisions,
            data={"pons": n_pon},
        ),
        before,
    )


def route_feeder(s: Session) -> ToolResult:
    s.project.set_stage("solve")
    twin = _twin(s)
    r = s.rules
    before = s.design.copy()
    pop = s.cache.get("pop_node")
    if pop is None:
        pop = int(s.design.by_kind("pop")[0].openftth["node"])
    fdhs = s.design.by_kind("fdh")
    terminals: dict[int, int] = {}
    for f in fdhs:
        node = int(f.openftth["node"])
        terminals[node] = terminals.get(node, 0) + int(f.openftth.get("pons", 1))
    tree = route_tree(twin.graph, pop, terminals)
    s.trees["feeder"] = tree
    s.design.remove_where(kind="feeder")
    n = _emit_cable_sections(s, tree, "feeder", r.fibers.feeder, prefix="FDR")
    for f in fdhs:
        f.openftth["feeder_path_m"] = round(_path_length(twin, tree, int(f.openftth["node"])), 1)
    decisions = [
        Decision(
            stage="solve",
            tool="route_feeder",
            reason="Feeder routed POP → FDHs as a min-cost Steiner tree over the street graph (trench cost per metre by surface); fibre count per section = downstream PONs + spare, rounded up the feeder ladder.",
            values={
                "sections": n,
                "length_m": round(tree.total_length_m(), 1),
                "unreachable_fdhs": len(tree.unreachable),
            },
        )
    ]
    return _commit(
        s,
        ToolResult(
            tool="route_feeder",
            summary=f"feeder {tree.total_length_m():.0f} m in {n} sections to {len(terminals)} FDH(s)",
            decisions=decisions,
            warnings=[f"{len(tree.unreachable)} FDH(s) unreachable from the POP"]
            if tree.unreachable
            else [],
            data={"length_m": round(tree.total_length_m(), 1)},
        ),
        before,
    )


def route_distribution(s: Session) -> ToolResult:
    s.project.set_stage("solve")
    twin = _twin(s)
    r = s.rules
    before = s.design.copy()
    s.design.remove_where(kind="distribution")
    total = 0.0
    n = 0
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
            terminals[node] = terminals.get(node, 0) + int(f.openftth["units"])
        tree = route_tree(twin.graph, int(fdh.openftth["node"]), terminals)
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
            reason="Per FDH, distribution routed FDH → FATs as a min-cost Steiner tree; fibre count per section = downstream units (one fibre per home in a centralised-split design) + spare, rounded up the distribution ladder.",
            values={"sections": n, "length_m": round(total, 1)},
        )
    ]
    return _commit(
        s,
        ToolResult(
            tool="route_distribution",
            summary=f"distribution {total:.0f} m in {n} sections",
            decisions=decisions,
            data={"length_m": round(total, 1)},
        ),
        before,
    )


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
    s.design.remove_where(kind="drop")
    fats = {f.element_uuid: f for f in s.design.by_kind("fat")}
    total = 0.0
    n = 0
    too_long = 0
    for h in s.design.by_kind("home"):
        fat = fats.get(h.openftth.get("fat_id", ""))
        if fat is None:
            continue
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
            h.openftth["path_to_pop_m"] = round(dist_m + feed_m, 1)
            tree = s.trees.get(fat.openftth.get("fdh_id", ""))
            closures = _closures_on_path(tree, int(fat.openftth["node"])) if tree else 0
            feeder_tree = s.trees.get("feeder")
            closures += (
                _closures_on_path(feeder_tree, int(fdh.openftth["node"]))
                if (feeder_tree and fdh)
                else 0
            )
            h.openftth["splices_on_path"] = 3 + closures
            continue
        hn, fn = int(h.openftth["node"]), int(fat.openftth["node"])
        path = nx.shortest_path(twin.graph, hn, fn, weight="length_m") if hn != fn else [hn]
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
        h.openftth["path_to_pop_m"] = round(length + dist_m + feed_m, 1)
        # Splices on the path: FAT, FDH (×2: distribution in, feeder in) + one per closure passed.
        tree = s.trees.get(fat.openftth.get("fdh_id", ""))
        closures = _closures_on_path(tree, fn) if tree else 0
        feeder_tree = s.trees.get("feeder")
        closures += (
            _closures_on_path(feeder_tree, int(fdh.openftth["node"]))
            if (feeder_tree and fdh)
            else 0
        )
        h.openftth["splices_on_path"] = 3 + closures
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
    s.design.remove_where(kind="chamber")
    fdh_nodes = {int(f.openftth["node"]) for f in s.design.by_kind("fdh")}
    fat_nodes = {int(f.openftth["node"]) for f in s.design.by_kind("fat")}
    pop_node = s.cache.get("pop_node")
    branch: dict[int, int] = {}  # node → max downstream fibres passing
    for key, tree in s.trees.items():
        deg = dict(tree.tree.degree)
        for node, d in deg.items():
            if d >= 3 or (node in fat_nodes and key != "feeder"):
                fibres = max(
                    (u for (p, c), u in tree.downstream_units.items() if p == node or c == node),
                    default=0,
                )
                branch[node] = max(branch.get(node, 0), fibres)
    n_small = n_large = 0
    for node in sorted(set(branch) | fdh_nodes):
        if node == pop_node:
            continue
        large = node in fdh_nodes
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
                },
            )
        )
        n_large += large
        n_small += not large
    decisions = [
        Decision(
            stage="solve",
            tool="place_chambers",
            reason="Chambers placed at every branch point of the feeder/distribution trees and at FAT sites (small) and at FDHs (large); branch chambers get a splice closure sized to the largest cable passing through.",
            values={"small": n_small, "large": n_large},
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
            reason=f"ODN loss per home = fibre ({s.rules.pon.worst_case_nm} nm) + {s.rules.connectors_on_path()} connectors × {s.rules.loss_db.connector} + splices × {s.rules.loss_db.splice} + splitter {s.rules.splitter_loss():.1f} dB + margin {s.rules.loss_db.margin} dB, checked against class {s.rules.pon.pon_class} budget {s.rules.pon.budget_db} dB and differential {s.rules.pon.max_differential_db} dB per PON.",
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
            summary=f"{msg}; worst ODN loss {stats.get('worst_db')} dB of {s.rules.pon.budget_db} dB",
            decisions=decisions,
            warnings=[f"{v.severity}: {v.message}" for v in violations[:10]],
            data=data,
        ),
        before,
    )


def deliver(s: Session, title: str | None = None) -> ToolResult:
    s.project.set_stage("deliver")
    twin = _twin(s)
    boq = build_boq(s.design, s.rules)
    d = s.project.deliver_dir
    (d / "boq.json").write_text(boq.model_dump_json(indent=2) + "\n", encoding="utf-8")
    (d / "boq.csv").write_text(boq_to_csv(boq), encoding="utf-8")
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
        "totals": boq.totals,
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
    time_limit_s: float = 20.0,
    rulebook_path: str | Path | None = None,
) -> list[ToolResult]:
    s = open_session(out_dir, rules, project_id=project_id or Path(out_dir).name)
    if rulebook_path is not None:
        shutil.copyfile(rulebook_path, s.project.dir / "rulebook.yaml")
    results = [
        import_area_and_osm(s, area_path, osm_path),
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
        deliver(s),
        splice_schedule(s),
        export_gpkg(s),
    ]
    return results


# ---- internals ----------------------------------------------------------------------------


def _twin(s: Session) -> Twin:
    """The twin, rebuilt from the project's recorded inputs when this session has none."""
    if s.twin is None:
        inputs = s.project.meta.get("inputs") or {}
        if not (inputs.get("area") and inputs.get("osm")):
            raise RuntimeError("twin not built; call import_osm and build_twin first")
        area = load_area(inputs["area"])
        osm = import_osm(inputs["osm"], area)
        homes_wgs, _ = homes_from_buildings(osm.buildings, s.rules)
        s.cache.update({"osm": osm, "homes_wgs": homes_wgs, "area": area})
        s.twin = build_twin(osm.streets, homes_wgs, s.rules)
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
) -> int:
    twin = _twin(s)
    r = s.rules
    n = 0
    for i, sec in enumerate(tree.sections):
        cable_count, fibers = r.ladder_cables(ladder, sec.downstream_units)
        edges = {
            f"{min(u, v)}-{max(u, v)}": (
                twin.graph[u][v]["surface"],
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
            demand_at[node] = demand_at.get(node, 0) + int(f.openftth["units"])
        tf = assign_tree_fibers(fdh_node, fsecs, demand_at)
        for c in secs:
            lo, hi = tf.section_range.get(c.element_uuid, (0, -1))
            c.openftth["fiber_start"], c.openftth["fiber_end"] = lo, hi
            c.openftth["fibers_used"] = max(0, hi - lo + 1)
            if c.openftth["fibers_used"] > int(c.props.get("fiber_count", 0)) * int(
                c.props.get("cable_count", 1)
            ):
                c.openftth["undersized"] = True
        # Splitter ports: PON order as assign_pon used (FATs by distribution distance), homes by id.
        port_of: dict[tuple[str, str, int], tuple[str, int]] = {}
        pon_fill: dict[str, int] = {}
        for f in sorted(
            my_fats, key=lambda f: (f.openftth.get("distribution_est_m", 0.0), f.element_uuid)
        ):
            pon = f.openftth.get("pon_id", "")
            for h in sorted(homes_by_fat.get(f.element_uuid, []), key=lambda h: h.element_uuid):
                for k in range(1, units_of(h, f) + 1):
                    pon_fill[pon] = pon_fill.get(pon, 0) + 1
                    port_of[(f.element_uuid, h.element_uuid, k)] = (pon, pon_fill[pon])
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
                                connection_type="splitter",
                                loss_db=r.splitter_loss(),
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
                                connection_type="splitter",
                                loss_db=r.splitter_loss(),
                                **dest,
                            )
                        g += 1
                    if not h.openftth.get("mdu") or h.openftth.get("fat_id") == f.element_uuid:
                        h.openftth.update(
                            {
                                "fiber_start": h_lo,
                                "fiber_end": g - 1,
                                "splitter_port": port_of[(f.element_uuid, h.element_uuid, 1)][1],
                            }
                        )
                    units_total += k_units
                f.openftth.update({"fiber_start": f_lo, "fiber_end": g - 1})
        # Pass-through splices at every inner node: incoming section -> each outgoing section.
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

    # ---- feeder: one fibre per PON ---------------------------------------------------------
    pops = s.design.by_kind("pop")
    if pops:
        pop = pops[0]
        pop_node = int(pop.openftth["node"])
        secs = s.design.by_kind("feeder")
        fsecs = [
            FSection(c.element_uuid, _node_of(c.props["from_label"]), _node_of(c.props["to_label"]))
            for c in secs
        ]
        fdhs = s.design.by_kind("fdh")
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
                            point_for("feeder", pop_node, pop.element_uuid),
                            f"feeder:pop:{g}",
                            source_element_uuid=pop.element_uuid,
                            source_port=g,
                            source_side="OLT",
                            dest_cable_uuid=out_sec,
                            dest_tube=t,
                            dest_fiber=fb,
                            connection_type="patch",
                            loss_db=r.loss_db.connector,
                        )
                    if in_sec:
                        t, fb = tube_fiber(tf.local(in_sec, g))
                        connect(
                            point_for("feeder", node, f.element_uuid),
                            f"feeder:fdh:{g}",
                            source_cable_uuid=in_sec,
                            source_tube=t,
                            source_fiber=fb,
                            dest_element_uuid=f.element_uuid,
                            dest_port=p,
                            dest_side=f"{f.props['name']}-PON-{p:02d} splitter in",
                        )
                    elif node == pop_node:
                        # FDH standing at the POP: OLT port patched straight to the splitter.
                        connect(
                            point_for("feeder", node, pop.element_uuid),
                            f"feeder:fdh:{g}",
                            source_element_uuid=pop.element_uuid,
                            source_port=g,
                            source_side="OLT",
                            dest_element_uuid=f.element_uuid,
                            dest_port=p,
                            dest_side=f"{f.props['name']}-PON-{p:02d} splitter in",
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
                        point_for("feeder", node, fallback),
                        f"feeder:thru:{node}:{g}",
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


def generate_scenarios(s: Session, n: int = 3, time_limit_s: float = 10.0) -> ToolResult:
    """Solve the current twin under rulebook variants and price each; nothing is applied."""
    import tempfile

    from .solve.scenarios import SCENARIOS, apply_overrides, resolve_overrides

    _twin(s)
    s.project.set_stage("solve")
    names = list(SCENARIOS)[: max(1, n)]
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
            table.append(
                {
                    "name": name,
                    "overrides": overrides,
                    "fats": len(scratch.design.by_kind("fat")),
                    "fdhs": len(scratch.design.by_kind("fdh")),
                    "trench_m": round(trench, 1),
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


def apply_scenario(s: Session, name: str, time_limit_s: float = 20.0) -> ToolResult:
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
