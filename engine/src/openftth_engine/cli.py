"""Headless CLI: `openftth-engine design|schema|rules|versions|serve`."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from . import __version__
from .rules import Rulebook, list_presets, load_rulebook

app = typer.Typer(no_args_is_help=True, add_completion=False, help="openFTTH design engine")
rules_app = typer.Typer(no_args_is_help=True, help="Rulebook commands")
app.add_typer(rules_app, name="rules")
for _stream in (sys.stdout, sys.stderr):  # Windows consoles default to cp1252
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")
console = Console()


@app.callback(invoke_without_command=True)
def _version_cb(version: Annotated[bool, typer.Option("--version", is_eager=True)] = False) -> None:
    if version:
        console.print(f"openftth-engine {__version__}")
        raise typer.Exit()


@app.command()
def design(
    out: Annotated[
        Path, typer.Option(help="Project directory to create/update (becomes a git repo)")
    ],
    rulebook: Annotated[
        str, typer.Option(help="Rulebook YAML path or preset name")
    ] = "generic-gpon",
    pop: Annotated[
        str | None, typer.Option(help="POP location as 'lon,lat'; default: area centroid")
    ] = None,
    time_limit: Annotated[
        float,
        typer.Option(help="CP-SAT work per cell (deterministic time units, about seconds)"),
    ] = 5.0,
    fresh: Annotated[bool, typer.Option(help="Delete an existing project directory first")] = False,
    area: Annotated[
        Path | None, typer.Option(help="Design area polygon (GeoJSON/GPKG), EPSG:4326")
    ] = None,
    osm: Annotated[
        Path | None,
        typer.Option(help="Local OpenStreetMap .osm XML with highways and buildings"),
    ] = None,
    profile: Annotated[
        Path | None,
        typer.Option(help="Import profile YAML (instead of --area/--osm), as File → Import data"),
    ] = None,
) -> None:
    """Run the whole pipeline headless: ingest → twin → solve → validate → deliver."""
    from .pipeline import design_all

    if profile is None and (area is None or osm is None):
        raise typer.BadParameter("give --area and --osm, or --profile")

    rules = load_rulebook(rulebook)
    rulebook_path = _resolve_rulebook(rulebook)
    if fresh and out.exists():
        shutil.rmtree(out)
    pop_xy = None
    if pop:
        lon, lat = (float(x) for x in pop.split(","))
        pop_xy = (lon, lat)
    console.rule(f"[bold]openFTTH design · {out.name} · rulebook {rules.name}")
    results = design_all(
        area or "",
        osm or "",
        rules,
        out,
        pop=pop_xy,
        time_limit_s=time_limit,
        rulebook_path=rulebook_path,
        profile=profile,
    )
    table = Table(title="Pipeline", show_lines=False)
    table.add_column("tool", style="cyan")
    table.add_column("summary")
    table.add_column("version", style="dim")
    for r in results:
        table.add_row(
            r.tool,
            r.summary + ("".join(f"\n[yellow]! {w}" for w in r.warnings)),
            (r.version or "")[:8],
        )
    console.print(table)
    summary = json.loads((out / "deliverables" / "summary.json").read_text(encoding="utf-8"))
    console.print(
        f"[bold green]BOQ total {summary['boq_total']:,.0f} {summary['currency']} "
        f"({summary['cost_per_unit']:,.0f}/unit) · map: {out / 'deliverables' / 'map.png'}"
    )


@app.command()
def schema(
    out: Annotated[
        Path | None, typer.Option(help="Write JSON schema here instead of stdout")
    ] = None,
    operator: Annotated[
        bool, typer.Option("--operator", help="The operator pack schema instead")
    ] = False,
    profile: Annotated[
        bool, typer.Option("--profile", help="The import profile schema instead")
    ] = False,
    export: Annotated[
        bool, typer.Option("--export", help="The export profile schema instead")
    ] = False,
) -> None:
    """Print (or write) the rulebook (or operator pack) JSON schema from the pydantic model."""
    from .deliver.exchange import ExportProfile
    from .ingest.profiles import ImportProfile
    from .network import OperatorPack

    model = (
        ExportProfile
        if export
        else ImportProfile
        if profile
        else (OperatorPack if operator else Rulebook)
    )
    text = json.dumps(model.model_json_schema(by_alias=True), indent=2) + "\n"
    if out:
        out.write_text(text, encoding="utf-8")
        console.print(f"wrote {out}")
    else:
        typer.echo(text)


@rules_app.command("list")
def rules_list() -> None:
    """List shipped rulebook presets."""
    for name in list_presets():
        rb = load_rulebook(name)
        console.print(f"[cyan]{name:14}[/] {rb.description}")


@rules_app.command("validate")
def rules_validate(path: Path) -> None:
    """Validate a rulebook YAML file."""
    rb = load_rulebook(path)
    console.print(
        f"[green]OK[/] {rb.name}: class {rb.pon.pon_class}, split 1:{rb.pon.split_ratio}, "
        f"splitter loss {rb.splitter_loss():.1f} dB, {rb.connectors_on_path()} connectors/path"
    )


@app.command()
def inspect(
    paths: Annotated[list[Path], typer.Argument(help="Data files to look at")],
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output")] = False,
) -> None:
    """List the layers in data files and propose an import profile for them."""
    from .ingest.profiles import ROLE_FIELDS, ROLE_LABEL, propose_profile

    profile, infos = propose_profile([str(p) for p in paths])
    if as_json:
        roles = {r: {"label": ROLE_LABEL[r], "fields": list(ROLE_FIELDS[r])} for r in ROLE_LABEL}
        out = {
            "layers": [i.to_dict() for i in infos],
            "profile": profile.model_dump(),
            "roles": roles,
        }
        sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
        return
    for i in infos:
        console.print(
            f"[cyan]{i.layer}[/] {i.geometry} ×{i.count} {i.crs or 'no CRS'} — {', '.join(i.columns[:8])}"
        )
    for b in profile.bindings:
        console.print(f"  → {b.role}: {b.path}{' / ' + b.layer if b.layer else ''} {b.columns}")


@app.command("profile-check")
def profile_check(
    path: Path,
    rulebook: Annotated[str, typer.Option()] = "generic-gpon",
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Validate an import profile, read its data and print the data-quality report."""
    from .ingest.inputs import load_inputs
    from .ingest.profiles import ImportProfile
    from .ingest.quality import check_inputs

    inp = load_inputs(ImportProfile.load(path), load_rulebook(_resolve_rulebook(rulebook)))
    report = check_inputs(inp, load_rulebook(_resolve_rulebook(rulebook)))
    if as_json:
        sys.stdout.write(json.dumps(report, ensure_ascii=False, default=str) + "\n")
        return
    console.print(f"[bold]{report['level']}[/] {report['counts']}")
    for i in report["issues"]:
        console.print(f"  {i['level']}: {i['message']}")


@app.command()
def versions(project: Path, limit: int = 30) -> None:
    """Show the project's version history (one commit per tool call)."""
    from .project import Project

    p = Project(project, create=False)
    for entry in p.log(limit):
        console.print(f"[dim]{entry['version'][:8]}[/] {entry['subject']}")


@app.command()
def serve(
    project: Annotated[Path, typer.Option(help="Project directory")],
    rulebook: Annotated[str, typer.Option()] = "generic-gpon",
    port: Annotated[int, typer.Option(help="Port for streamable-HTTP MCP; 0 = stdio")] = 0,
) -> None:
    """Run the MCP server exposing the design tools to the agent crew."""
    from .mcp_server import serve as _serve

    _serve(project, rulebook, port)


def _resolve_rulebook(name_or_path: str) -> Path:
    p = Path(name_or_path)
    if p.exists():
        return p
    from .rules import preset_path

    return preset_path(name_or_path)


if __name__ == "__main__":
    app()
