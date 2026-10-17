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

from . import pipeline
from .results import ToolResult
from .rules import Rulebook, list_presets, load_rulebook

RISKY_TOOLS = {"route_outside_ducts", "add_trench", "export_for_construction", "rewind_design"}

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
def build_twin() -> dict[str, Any]:
    """Build the street graph with trench costs and snap homes to it."""
    return _out(pipeline.build_twin_tool(_s()))


@mcp.tool()
def set_pop(lon: float | None = None, lat: float | None = None) -> dict[str, Any]:
    """Place the POP/OLT site. Without coordinates it goes to the area centroid (recorded as an assumption)."""
    return _out(pipeline.set_pop(_s(), lon, lat))


@mcp.tool()
def cluster_homes(time_limit_s: float = 20.0) -> dict[str, Any]:
    """Place FATs by capacitated clustering of homes (CP-SAT) within the max drop distance."""
    return _out(pipeline.cluster_homes(_s(), time_limit_s))


@mcp.tool()
def place_fdh(time_limit_s: float = 20.0) -> dict[str, Any]:
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
def export_gpkg() -> dict[str, Any]:
    """Export the design as a GeoPackage (one layer per element type, side tables as attribute
    tables)."""
    return _out(pipeline.export_gpkg(_s()))


@mcp.tool()
def generate_scenarios(n: int = 3, time_limit_s: float = 10.0) -> dict[str, Any]:
    """Solve the twin under rulebook variants (baseline, larger-fats, shorter-drops,
    lean-spares) and price each. Nothing is applied."""
    return _out(pipeline.generate_scenarios(_s(), n, time_limit_s))


@mcp.tool()
def apply_scenario(name: str, time_limit_s: float = 20.0) -> dict[str, Any]:
    """Adopt a scenario from generate_scenarios: apply its rulebook overrides and re-run the
    solve chain."""
    return _out(pipeline.apply_scenario(_s(), name, time_limit_s))


# ---- risky tools (never on an allow-list; need an approval_id) ---------------------------------


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
