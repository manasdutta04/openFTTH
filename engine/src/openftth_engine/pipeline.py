"""The design tools, and the headless pipeline that runs them in order.

Each public function here is one agent-callable tool: it mutates the design through the
project (which commits), and returns a `ToolResult`. `design_all` chains them for the CLI.
"""

from __future__ import annotations

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
from .solve.cluster import Demand, capacitated_sites
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
    # Homes become `to` features (one per building, with a unit count).
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
            values={"fdhs": len(sites), "capacity_used": capacity},
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
    terminals = {int(f.openftth["node"]): int(f.openftth.get("pons", 1)) for f in fdhs}
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
        terminals = {int(f.openftth["node"]): int(f.openftth["units"]) for f in fats}
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


def route_drops(s: Session) -> ToolResult:
    s.project.set_stage("solve")
    twin = _twin(s)
    r = s.rules
    before = s.design.copy()
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
            warnings=[f"{too_long} drops exceed max_drop_m"] if too_long else [],
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
    data = {
        "violations": [v.__dict__ for v in violations[:200]],
        "summary": summary,
        "power_budget": stats,
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
    msg = "no violations" if not violations else ", ".join(f"{k}: {v}" for k, v in summary.items())
    return _commit(
        s,
        ToolResult(
            tool="check_rules",
            summary=f"{msg}; worst ODN loss {stats.get('worst_db')} dB of {s.rules.pon.budget_db} dB",
            decisions=decisions,
            warnings=[v.message for v in violations[:10]],
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
        validate_design(s),
        deliver(s),
    ]
    return results


# ---- internals ----------------------------------------------------------------------------


def _twin(s: Session) -> Twin:
    if s.twin is None:
        raise RuntimeError("twin not built; call build_twin first")
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
    "design_all",
    "mapping",
]
