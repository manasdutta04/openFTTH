"""MCP server exposing the design tools to the agent crew.

One long-lived process per project (streamable HTTP; stdio for tests). Every tool returns the
`ToolResult` contract. Risky tools require an `approval_id` that is validated against the
harness before anything happens; without a harness URL they are refused outright.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from . import edit, pipeline
from .results import ToolResult
from .rules import Rulebook, list_presets, load_rulebook
from .solve.cluster import WORK_LIMIT

RISKY_TOOLS = {
    "route_outside_ducts",
    "add_trench",
    "export_for_construction",
    "rewind_design",
    "post_work_order",
}

mcp = FastMCP(
    "openftth-engine",
    instructions=(
        "Deterministic FTTH/GPON design tools. Call tools in stage order: import_osm → build_twin → set_pop → "
        "cluster_homes → place_fdh → assign_pon → route_feeder → route_distribution → route_drops → place_chambers → "
        "check_rules → boq. Never state a number that a tool did not return; quote decisions[].reason when explaining."
    ),
)

_session: pipeline.Session | None = None
_project_dir: Path | None = None
_rulebook_name: str = "generic-gpon"


def _s() -> pipeline.Session:
    global _session
    if _session is None:
        assert _project_dir is not None
        _session = pipeline.open_session(_project_dir, load_rulebook(_rulebook_name))
    return _session


def _out(r: ToolResult) -> dict[str, Any]:
    return r.model_dump()


def _refuse_without_approval(tool: str, approval_id: str | None) -> dict[str, Any] | None:
    """Return a refusal payload unless the harness confirms `approval_id` for `tool`."""
    url = os.environ.get("OPENFTTH_APPROVALS_URL")
    token = os.environ.get("OPENFTTH_COMMS_TOKEN", "")
    if not approval_id:
        return {
            "error": "approval_required",
            "tool": tool,
            "message": f"{tool} changes the design in a way an engineer must approve. Ask the user for approval; the harness will issue an approval_id.",
        }
    if not url:
        return {
            "error": "approval_unverifiable",
            "tool": tool,
            "message": "No OPENFTTH_APPROVALS_URL configured; risky tools are disabled in this session.",
        }
    req = urllib.request.Request(
        f"{url.rstrip('/')}/{approval_id}", headers={"authorization": f"Bearer {token}"}
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310 (loopback harness)
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError) as exc:
        return {"error": "approval_check_failed", "tool": tool, "message": str(exc)}
    if not (body.get("approved") and body.get("tool") == tool):
        return {
            "error": "approval_denied",
            "tool": tool,
            "message": "The approval does not cover this tool or was denied.",
        }
    return None


# ---- read-only tools -------------------------------------------------------------------------


@mcp.tool()
def list_presets_tool() -> list[str]:
    """List the shipped rulebook presets (generic-gpon, belgium, norway, …)."""
    return list_presets()


@mcp.tool()
def get_rulebook() -> dict[str, Any]:
    """Return the active rulebook with every design number the solvers use."""
    return _s().rules.model_dump(by_alias=True)


@mcp.tool()
def explain_rule(path: str) -> dict[str, Any]:
    """Explain one rulebook value, e.g. 'pon.budget_db' or 'fat.max_homes'."""
    cur: Any = Rulebook.model_json_schema(by_alias=True)
    value: Any = _s().rules.model_dump(by_alias=True)
    for part in path.split("."):
        value = value[part] if isinstance(value, dict) else None
    return {"path": path, "value": value, "schema": _schema_at(cur, path)}


@mcp.tool()
def design_summary() -> dict[str, Any]:
    """Counts and lengths of the current design, by element kind."""
    d = _s().design
    kinds: dict[str, int] = {}
    for f in d:
        kinds[f.kind or f.element_type] = kinds.get(f.kind or f.element_type, 0) + 1
    return {
        "project": _s().pid,
        "stage": _s().project.meta.get("stage"),
        "version": _s().project.head(),
        "elements": kinds,
        "network_of_record": _s().project.record(),
        "work_order": next(
            (w for w in _s().project.meta.get("work_orders", []) if w["state"] == "open"), None
        ),
        "operator": _s().project.meta.get("operator", "generic"),
        "rulebook": {
            "name": _s().rules.name,
            "status": _s().rules.review.status,
            "note": _s().rules.review.note,
        },
    }


@mcp.tool()
def explain_element(element_uuid: str) -> dict[str, Any]:
    """Why is this element where it is? Returns its attributes and the decisions that mention it."""
    s = _s()
    if element_uuid not in s.design:
        return {"error": "not_found", "element_uuid": element_uuid}
    f = s.design.get(element_uuid)
    related = [
        d.model_dump()
        for d in s.project.decisions()
        if d.element == element_uuid or d.tool in _tools_for_kind(f.kind)
    ]
    return {
        "element_uuid": element_uuid,
        "element_type": f.element_type,
        "kind": f.kind,
        "props": f.props,
        "openftth": f.openftth,
        "decisions": related[-10:],
    }


@mcp.tool()
def list_assumptions() -> list[dict[str, Any]]:
    """Every assumption the engine made so far (decisions with source=assumption)."""
    return [d.model_dump() for d in _s().project.decisions() if d.source == "assumption"]


@mcp.tool()
def versions(limit: int = 30) -> list[dict[str, Any]]:
    """Version history: one commit per tool call."""
    return _s().project.log(limit)


@mcp.tool()
def diff_versions(older: str, newer: str) -> dict[str, Any]:
    """Semantic diff (added/removed/modified element_uuids) between two versions."""
    p = _s().project
    return p.design_at(older).diff(p.design_at(newer)).__dict__


# ---- design tools (each commits one version) ---------------------------------------------------


@mcp.tool()
def import_osm(area_path: str, osm_path: str) -> dict[str, Any]:
    """Ingest the design area polygon and a local OpenStreetMap .osm file (streets, buildings)."""
    return _out(pipeline.import_area_and_osm(_s(), area_path, osm_path))


@mcp.tool()
def list_layers(path: str) -> list[dict[str, Any]]:
    """What a data file holds: layers, geometry type, feature count, CRS, columns, sample values.
    Reads GeoJSON, GeoPackage, Shapefile, KML, FlatGeobuf, GML, MapInfo, DXF and CSV."""
    from .ingest.sources import list_layers as _list

    return [i.to_dict() for i in _list(path)]


@mcp.tool()
def propose_import(paths: list[str]) -> dict[str, Any]:
    """A first import profile for a set of files: which layer is the area, the streets, the
    address register, existing ducts and so on, and which columns carry what. Review it, then
    pass it to import_data."""
    from .ingest.profiles import propose_profile

    profile, infos = propose_profile(paths)
    return {"profile": profile.model_dump(), "layers": [i.to_dict() for i in infos]}


@mcp.tool()
def import_data(
    profile: dict[str, Any] | None = None, profile_path: str | None = None
) -> dict[str, Any]:
    """Import the project's data from any sources an import profile names (area, streets,
    buildings, address register, existing ducts/poles/manholes, no-dig zones, OSM extract)."""
    from .ingest.profiles import ImportProfile

    if profile is not None:
        p = ImportProfile.model_validate(profile)
    elif profile_path:
        p = ImportProfile.load(profile_path)
    else:
        return {"error": "give a profile or a profile_path"}
    return _out(pipeline.import_data(_s(), p))


@mcp.tool()
def check_inputs() -> dict[str, Any]:
    """Data-quality report on the imported inputs: homes far from streets, duplicates,
    buildings without addresses, a street network in pieces. Read-only."""
    return _out(pipeline.check_inputs_tool(_s()))


@mcp.tool()
def fetch_osm(area_path: str) -> dict[str, Any]:
    """Download an OpenStreetMap extract for an area polygon from the public Overpass API into
    the project (a network call; only when no local extract exists)."""
    return _out(pipeline.fetch_osm_tool(_s(), area_path))


@mcp.tool()
def build_twin() -> dict[str, Any]:
    """Build the street graph with trench costs and snap homes to it."""
    return _out(pipeline.build_twin_tool(_s()))


@mcp.tool()
def set_pop(
    lon: float | None = None,
    lat: float | None = None,
    count: int | None = None,
    sites: list[list[float]] | None = None,
) -> dict[str, Any]:
    """Place the POP/OLT site, or several. Without coordinates one POP goes to the area centroid
    (recorded as an assumption); an area with more units than `pops.max_units` gets several,
    each with its own catchment. `count` asks for a number of POPs, `sites` gives their
    [lon, lat] positions."""
    return _out(pipeline.set_pop(_s(), lon, lat, count, sites))


@mcp.tool()
def cluster_homes(time_limit_s: float = WORK_LIMIT) -> dict[str, Any]:
    """Place FATs by capacitated clustering of homes (CP-SAT) within the max drop distance."""
    return _out(pipeline.cluster_homes(_s(), time_limit_s))


@mcp.tool()
def place_fdh(time_limit_s: float = WORK_LIMIT) -> dict[str, Any]:
    """Place FDH splitter cabinets over the FATs (CP-SAT) at street intersections."""
    return _out(pipeline.place_fdh(_s(), time_limit_s))


@mcp.tool()
def assign_pon() -> dict[str, Any]:
    """Group FATs under each FDH into PONs of ≤ split_ratio units."""
    return _out(pipeline.assign_pon(_s()))


@mcp.tool()
def route_feeder() -> dict[str, Any]:
    """Route the feeder POP → FDHs as a min-cost Steiner tree and size its cables."""
    return _out(pipeline.route_feeder(_s()))


@mcp.tool()
def route_distribution() -> dict[str, Any]:
    """Route distribution cables FDH → FATs per FDH and size them."""
    return _out(pipeline.route_distribution(_s()))


@mcp.tool()
def route_drops() -> dict[str, Any]:
    """Connect every home to its FAT along the street graph."""
    return _out(pipeline.route_drops(_s()))


@mcp.tool()
def place_chambers() -> dict[str, Any]:
    """Place chambers at branch points and FDHs, with closures sized to the passing cable."""
    return _out(pipeline.place_chambers(_s()))


@mcp.tool()
def plan_phases(phases: int = 0, order: str = "") -> dict[str, Any]:
    """Group the cabinet areas into build phases of about equal units (order: density, the best
    business case first, or distance from the POP); every element gets the phase that builds it
    and the BOQ then comes per phase. 0 / empty take the rulebook's phasing."""
    return _out(pipeline.plan_phases(_s(), phases or None, order or None))


@mcp.tool()
def check_rules() -> dict[str, Any]:
    """Validate capacity, distances and the per-home optical power budget."""
    return _out(pipeline.validate_design(_s()))


@mcp.tool()
def boq() -> dict[str, Any]:
    """Build the bill of quantities and the map; writes deliverables/."""
    return _out(pipeline.deliver(_s()))


@mcp.tool()
def set_rule(path: str, value: str) -> dict[str, Any]:
    """Change one rulebook value by dotted path (e.g. 'fat.max_homes', '24'); validated and
    saved. Re-run the solvers afterwards."""
    return _out(pipeline.set_rule(_s(), path, value))


@mcp.tool()
def size_cables() -> dict[str, Any]:
    """Re-size feeder/distribution sections from downstream demand and the rulebook ladders
    (tubes, slack, parallel cables)."""
    return _out(pipeline.size_cables(_s()))


@mcp.tool()
def assign_fibers() -> dict[str, Any]:
    """Assign splitter ports, section fibres/tubes and drop fibres per unit; write splice
    points and fibre connections."""
    return _out(pipeline.assign_fibers(_s()))


@mcp.tool()
def splice_schedule() -> dict[str, Any]:
    """Write the per-closure splice schedule (CSV) from the fibre connections."""
    return _out(pipeline.splice_schedule(_s()))


@mcp.tool()
def export_profile(name: str = "generic-gis") -> dict[str, Any]:
    """Write the design in an operator's or another program's schema through an export profile
    (layer and field names, types, code lists, CRS): GeoPackage, Shapefile or CSV. `name` is a
    shipped profile (see list_export_profiles) or a path to a profile YAML."""
    return _out(pipeline.export_profile(_s(), name))


@mcp.tool()
def list_export_profiles() -> dict[str, Any]:
    """The shipped export profiles with their format and description."""
    from .deliver.exchange import list_export_profiles as names
    from .deliver.exchange import load_export_profile

    return {
        "profiles": [
            {"name": n, "driver": p.driver, "crs": p.crs, "description": p.description}
            for n in names()
            for p in [load_export_profile(n)]
        ]
    }


@mcp.tool()
def reports() -> dict[str, Any]:
    """Write the cable schedule (every cable with its ends, size, length and metres per kind of
    route), the port report (cabinets, FATs and closures with ports used and free) and, with
    build phases, the materials by phase, as CSV files."""
    return _out(pipeline.reports(_s()))


@mcp.tool()
def plan_sheets(scale: int = 1000, paper: str = "A3") -> dict[str, Any]:
    """Write the printable plan set (plans.pdf: a key plan, then one sheet per grid cell with
    legend, scale bar, north arrow and title block) and the whole design as a DXF drawing
    (plans.dxf). `scale` is the sheet scale (1000 = 1:1000), `paper` A3 or A1."""
    return _out(pipeline.plan_sheets(_s(), scale, paper))


@mcp.tool()
def export_gpkg() -> dict[str, Any]:
    """Export the design as a GeoPackage (one layer per element type, side tables as attribute
    tables)."""
    return _out(pipeline.export_gpkg(_s()))


@mcp.tool()
def generate_scenarios(
    n: int = 3, time_limit_s: float = 10.0, names: list[str] | None = None
) -> dict[str, Any]:
    """Solve the twin under rulebook variants and price each; nothing is applied. Variants:
    baseline, larger-fats, shorter-drops, lean-spares, and the architectures centralised,
    cascaded and point-to-point, and the route types trench-only, facade, aerial and
    aerial-and-facade. `names` picks which; otherwise the first `n`."""
    return _out(pipeline.generate_scenarios(_s(), n, time_limit_s, names))


@mcp.tool()
def apply_scenario(name: str, time_limit_s: float = WORK_LIMIT) -> dict[str, Any]:
    """Adopt a scenario from generate_scenarios: apply its rulebook overrides and re-run the
    solve chain."""
    return _out(pipeline.apply_scenario(_s(), name, time_limit_s))


# ---- the engineer's own moves (the app calls these through the harness; no crew member has them)


@mcp.tool()
def move_site(element_uuid: str, lon: float, lat: float, reason: str) -> dict[str, Any]:
    """Move the POP, an FDH or an outdoor FAT to a new position; the point snaps to the street
    graph and every route downstream is planned and sized again, as one version. Records the
    engineer's reason."""
    return _out(edit.move_site(_s(), element_uuid, lon, lat, reason))


@mcp.tool()
def add_site(kind: str, lon: float, lat: float, reason: str) -> dict[str, Any]:
    """Place a FAT, FDH cabinet, chamber or POP (kind fat|fdh|chamber|pop) at the street node
    nearest the point; pinned so solver runs keep it. Records the engineer's reason."""
    return _out(edit.add_site(_s(), kind, lon, lat, reason))


@mcp.tool()
def delete_element(element_uuid: str, reason: str) -> dict[str, Any]:
    """Delete a site or a cable. A FAT or cabinet that still serves something is refused until
    what it serves is reassigned."""
    return _out(edit.delete_element(_s(), element_uuid, reason))


@mcp.tool()
def draw_cable(
    kind: str, points: list[list[float]], reason: str, fiber_count: int | None = None
) -> dict[str, Any]:
    """Draw a feeder, distribution or drop cable through [lon, lat] points; it follows the
    cheapest street path between them and routing runs keep it."""
    return _out(edit.draw_cable(_s(), kind, points, reason, fiber_count))


@mcp.tool()
def split_cable(element_uuid: str, lon: float, lat: float, reason: str) -> dict[str, Any]:
    """Split a cable in two at the street node nearest the point (a chamber is added there)."""
    return _out(edit.split_cable(_s(), element_uuid, lon, lat, reason))


@mcp.tool()
def join_cables(first_uuid: str, second_uuid: str, reason: str) -> dict[str, Any]:
    """Join two cables of the same kind and size that end at the same street node into one; the
    straight splices at the joint go. A joint that crosses fibres is refused."""
    return _out(edit.join_cables(_s(), first_uuid, second_uuid, reason))


@mcp.tool()
def delete_elements(element_uuids: list[str], reason: str) -> dict[str, Any]:
    """Delete several sites and cables as one edit (one undo step); homes and buildings are
    skipped."""
    return _out(edit.delete_elements(_s(), element_uuids, reason))


@mcp.tool()
def edit_splice(
    connection_uuid: str,
    reason: str,
    remove: bool = False,
    connection_type: str | None = None,
    tray_ref: str | None = None,
    notes: str | None = None,
    source_fiber: int | None = None,
    dest_fiber: int | None = None,
) -> dict[str, Any]:
    """Change one splice schedule row: re-patch an end to another fibre of the same cable
    (numbered from 1 across tubes), make it a splice or a patch, set the tray or a note, or
    remove it. Fibre numbers come from the engineer, never from you."""
    return _out(
        edit.edit_splice(
            _s(),
            connection_uuid,
            reason,
            remove=remove,
            connection_type=connection_type,
            tray_ref=tray_ref,
            notes=notes,
            source_fiber=source_fiber,
            dest_fiber=dest_fiber,
        )
    )


@mcp.tool()
def list_violations() -> dict[str, Any]:
    """The rule check of the design as it stands, per element (nothing is written)."""
    return edit.list_violations(_s())


@mcp.tool()
def set_attribute(element_uuid: str, key: str, value: Any, reason: str) -> dict[str, Any]:
    """Change an attribute the engineer owns: name, status, notes, fiber_count, cable_count,
    slack_m, port_capacity or manhole_type, checked against the rulebook."""
    return _out(edit.set_attribute(_s(), element_uuid, key, value, reason))


@mcp.tool()
def reassign(
    to_uuid: str,
    reason: str,
    element_uuid: str | None = None,
    element_uuids: list[str] | None = None,
) -> dict[str, Any]:
    """Give homes to a FAT or FATs to a cabinet (one, or several at once); PONs, routes, sizes
    and fibres are worked out again as one version."""
    return _out(edit.reassign(_s(), element_uuid, to_uuid, reason, element_uuids=element_uuids))


@mcp.tool()
def add_zone(kind: str, points: list[list[float]], reason: str) -> dict[str, Any]:
    """Draw a zone through [lon, lat] corner points: keep_out (no trench and no equipment
    inside) or no_dig (no new trench; existing ducts usable). The design is solved again around
    it; delete_element removes a zone."""
    return _out(edit.add_zone(_s(), kind, points, reason))


@mcp.tool()
def complete_network(reason: str) -> dict[str, Any]:
    """Finish a network laid out by hand: route feeder and distribution along the cables the
    engineer drew, add drops for every assigned home, place chambers, size cables and number
    fibres, as one version. Places no site and clusters nothing."""
    return _out(edit.complete_network(_s(), reason))


@mcp.tool()
def undo_edit() -> dict[str, Any]:
    """Undo the engineer's last edit, if nothing has changed since."""
    return _out(edit.undo_edit(_s()))


@mcp.tool()
def redo_edit() -> dict[str, Any]:
    """Redo the edit undone last, if nothing has changed since the undo."""
    return _out(edit.redo_edit(_s()))


@mcp.tool()
def edit_history() -> dict[str, Any]:
    """What undo and redo would do now."""
    return edit.edit_history(_s())


@mcp.tool()
def exclude_building(element_uuid: str, reason: str) -> dict[str, Any]:
    """Take a building out of the design: its homes and drops go, FAT and cabinet units are
    reduced, routes are planned again. The exclusion survives a re-import."""
    return _out(pipeline.exclude_building(_s(), element_uuid, reason))


@mcp.tool()
def forbid_street(osm_id: str, reason: str) -> dict[str, Any]:
    """Mark a street (OSM way id) as no-dig: it leaves the route graph, sites on it move to
    the nearest remaining street node, routes are planned again. Kept with the project inputs."""
    return _out(pipeline.forbid_street(_s(), osm_id, reason))


# ---- network of record: operator pack, lifecycle, work orders, trace -------------------------


@mcp.tool()
def get_operator_pack() -> dict[str, Any]:
    """The operator pack in force: object types, id patterns, required attributes, lifecycle."""
    return _out(pipeline.get_operator_pack(_s()))


@mcp.tool()
def set_operator_pack(name: str) -> dict[str, Any]:
    """Switch the project to another shipped operator pack (for example `generic`)."""
    return _out(pipeline.set_operator_pack(_s(), name))


@mcp.tool()
def check_operator() -> dict[str, Any]:
    """Check the design against the operator pack: ids, required attributes, lifecycle states,
    and that every home's fibre reaches an OLT port. Read-only; commits nothing."""
    return _out(pipeline.check_operator_tool(_s()))


@mcp.tool()
def set_lifecycle(
    state: str,
    reason: str,
    element_uuids: list[str] | None = None,
    kinds: list[str] | None = None,
) -> dict[str, Any]:
    """Move objects (by id, or every object of the given kinds) to a lifecycle state:
    planned, in_construction, as_built, in_service, out_of_service, removed. The operator
    pack decides which transitions are allowed."""
    return _out(pipeline.set_lifecycle(_s(), state, reason, element_uuids, kinds))


@mcp.tool()
def open_work_order(title: str) -> dict[str, Any]:
    """Open a work order: every change from now on belongs to it until it is posted."""
    return _out(pipeline.open_work_order(_s(), title))


@mcp.tool()
def work_order_status() -> dict[str, Any]:
    """The open work order, the network of record, and what changed since, by kind."""
    return _out(pipeline.work_order_status(_s()))


@mcp.tool()
def trace_fibre(element_uuid: str) -> dict[str, Any]:
    """Trace a home's fibre (or the home behind a drop) from its OLT port through every
    cable, splice and splitter to the home, with the loss of each hop."""
    return _out(pipeline.trace_fibre(_s(), element_uuid))


@mcp.tool()
def homes_behind(element_uuid: str) -> dict[str, Any]:
    """Every home whose fibre passes through a cable, closure, chamber, FAT or cabinet."""
    return _out(pipeline.homes_behind_tool(_s(), element_uuid))


# ---- risky tools (never on an allow-list; need an approval_id) ---------------------------------


@mcp.tool()
def post_work_order(approval_id: str | None = None) -> dict[str, Any]:
    """RISKY: post the open work order to the network of record. Needs the engineer's
    approval; the operator pack and the design rules must pass with no errors."""
    refusal = _refuse_without_approval("post_work_order", approval_id)
    if refusal:
        return refusal
    r = pipeline.post_work_order(_s())
    if r.version:
        # The card's id travels in the permission layer; say it here so the crew can quote it.
        r.data["approval_id"] = approval_id
        r.summary += f"; approved by the engineer (card {str(approval_id)[:8]})"
    return _out(r)


@mcp.tool()
def add_trench(
    from_lon: float,
    from_lat: float,
    to_lon: float,
    to_lat: float,
    surface: str = "verge",
    approval_id: str | None = None,
) -> dict[str, Any]:
    """RISKY: add a new trench segment to the street graph (digging where no street is)."""
    refusal = _refuse_without_approval("add_trench", approval_id)
    if refusal:
        return refusal
    return {
        "error": "not_implemented",
        "message": "add_trench lands in Phase 1 with the duct layer.",
    }


@mcp.tool()
def rewind_design(version: str, approval_id: str | None = None) -> dict[str, Any]:
    """RISKY: reset the design to an earlier version."""
    refusal = _refuse_without_approval("rewind_design", approval_id)
    if refusal:
        return refusal
    s = _s()
    s.design = s.project.design_at(version)
    r = ToolResult(tool="rewind_design", summary=f"design rewound to {version[:8]}")
    s.project.commit(r, s.design)
    return _out(r)


@mcp.tool()
def export_for_construction(approval_id: str | None = None) -> dict[str, Any]:
    """RISKY: mark the current version as approved for construction and export the bundle."""
    refusal = _refuse_without_approval("export_for_construction", approval_id)
    if refusal:
        return refusal
    s = _s()
    version = s.project.head()
    return {
        "approved_version": version,
        "bundle": str(s.project.bundle_dir),
        "message": "GeoPackage/DXF/PDF exporters land in Phase 1; the GeoJSON-lite bundle is the approved artefact.",
    }


# ---- entry point ------------------------------------------------------------------------------


def serve(project: Path, rulebook: str, port: int) -> None:
    global _project_dir, _rulebook_name
    _project_dir = project
    _rulebook_name = rulebook
    if port:
        mcp.settings.host = "127.0.0.1"
        mcp.settings.port = port
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")


def _schema_at(schema: dict[str, Any], path: str) -> dict[str, Any] | None:
    defs = schema.get("$defs", {})
    node: dict[str, Any] | None = schema
    for part in path.split("."):
        if node is None:
            return None
        props = node.get("properties", {})
        node = props.get(part)
        if node and "$ref" in node:
            node = defs.get(node["$ref"].split("/")[-1])
        elif node and "allOf" in node:
            node = defs.get(node["allOf"][0]["$ref"].split("/")[-1])
    return node


def _tools_for_kind(kind: str | None) -> set[str]:
    return {
        "fat": {"cluster_homes"},
        "fdh": {"place_fdh"},
        "pop": {"set_pop"},
        "home": {"import_osm", "build_twin", "route_drops"},
        "feeder": {"route_feeder"},
        "distribution": {"route_distribution"},
        "drop": {"route_drops"},
        "chamber": {"place_chambers"},
    }.get(kind or "", set())
