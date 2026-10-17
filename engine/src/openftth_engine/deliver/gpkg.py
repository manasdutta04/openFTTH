"""GeoPackage export: one spatial layer per element type (and placement), the side tables as
attribute tables, and the bundle metadata. Opens in any GIS."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import geopandas as gpd

from ..interchange import Design

SCALAR = (str, int, float, bool)


def export_gpkg(design: Design, path: str | Path) -> dict[str, Any]:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    layers: list[str] = []
    for stem, feats in sorted(design.file_groups().items()):
        rows = []
        geoms = []
        for f in feats:
            row: dict[str, Any] = {
                "element_uuid": f.element_uuid,
                "element_type": f.element_type,
                "placement": f.placement,
                "kind": f.kind,
            }
            for k, v in sorted(f.props.items()):
                row[k] = v if isinstance(v, SCALAR) or v is None else json.dumps(v, sort_keys=True)
            row["extra_json"] = json.dumps(
                {"openftth": f.openftth}, sort_keys=True, separators=(",", ":")
            )
            rows.append(row)
            geoms.append(f.geometry)
        gdf = gpd.GeoDataFrame(rows, geometry=geoms, crs=4326)
        layer = stem.replace(".", "_")
        gdf.to_file(path, layer=layer, driver="GPKG", mode="a" if path.exists() else "w")
        layers.append(layer)

    tables: list[str] = []
    with sqlite3.connect(path) as con:
        for name, rows in sorted(design.tables.items()):
            if not rows:
                continue
            cols = sorted({k for r in rows for k in r})
            con.execute(f'DROP TABLE IF EXISTS "{name}"')
            con.execute(f'CREATE TABLE "{name}" ({", ".join(_col_def(cols, rows))})')
            con.executemany(
                f'INSERT INTO "{name}" ({", ".join(f"{chr(34)}{c}{chr(34)}" for c in cols)}) VALUES ({", ".join("?" for _ in cols)})',
                [[_cell(r.get(c)) for c in cols] for r in rows],
            )
            _register_attributes(con, name, f"openFTTH {name} table")
            tables.append(name)
        con.execute("DROP TABLE IF EXISTS _openftth_metadata")
        con.execute("CREATE TABLE _openftth_metadata (key TEXT PRIMARY KEY, value TEXT)")
        con.executemany(
            "INSERT INTO _openftth_metadata (key, value) VALUES (?, ?)",
            sorted(design.metadata.to_table().items()),
        )
        _register_attributes(con, "_openftth_metadata", "openFTTH bundle metadata")
        con.commit()
    return {"path": str(path), "layers": layers, "tables": tables, "bytes": path.stat().st_size}


def _col_def(cols: list[str], rows: list[dict[str, Any]]) -> list[str]:
    out = []
    for c in cols:
        sample = next((r[c] for r in rows if r.get(c) is not None), None)
        if c == "uuid":
            typ = "TEXT PRIMARY KEY"
        elif isinstance(sample, bool):
            typ = "INTEGER"
        elif isinstance(sample, int):
            typ = "INTEGER"
        elif isinstance(sample, float):
            typ = "REAL"
        else:
            typ = "TEXT"
        out.append(f'"{c}" {typ}')
    return out


def _cell(v: Any) -> Any:
    if v is None or isinstance(v, (int, float, str)):
        return v
    return json.dumps(v, sort_keys=True)


def _register_attributes(con: sqlite3.Connection, name: str, description: str) -> None:
    con.execute("DELETE FROM gpkg_contents WHERE table_name = ?", (name,))
    con.execute(
        "INSERT INTO gpkg_contents (table_name, data_type, identifier, description, last_change) VALUES (?, 'attributes', ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
        (name, name, description),
    )
