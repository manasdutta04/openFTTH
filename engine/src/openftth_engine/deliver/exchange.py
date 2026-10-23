"""Export profiles: the design written in someone else's schema.

An export profile (YAML, `exports/*.yaml`) names the file format, the coordinate system and,
for each design kind, the output layer and its fields: where each field's value comes from in
the design, its type, and an optional code list (`planned` → `PLN`). `export_with_profile`
writes the files and a manifest; `read_with_profile` reads them back through the same profile,
so a round trip can prove nothing was lost.
"""

from __future__ import annotations

import csv
import json
import shutil
from pathlib import Path
from typing import Any, Literal

import geopandas as gpd
import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pyproj import CRS
from shapely import wkt

from ..interchange import Design

Driver = Literal["gpkg", "shapefile", "csv"]
FieldType = Literal["text", "int", "real", "bool"]
KINDS = (
    "pop",
    "fdh",
    "fat",
    "chamber",
    "feeder",
    "distribution",
    "drop",
    "home",
    "crossing",
    "zone",
    "building",
)
EXT = {"gpkg": ".gpkg", "shapefile": ".shp", "csv": ".csv"}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class FieldMap(_Strict):
    source: str = Field(
        alias="from",
        description="Where the value comes from: uuid, kind, lifecycle, props.<key> or openftth.<key>",
    )
    type: FieldType = "text"
    codes: dict[str, str] = Field(
        default_factory=dict, description="Design value → the schema's code (a code list)"
    )

    @model_validator(mode="after")
    def _known_source(self) -> FieldMap:
        head = self.source.split(".", 1)[0]
        if self.source not in ("uuid", "kind", "lifecycle") and head not in ("props", "openftth"):
            raise ValueError(f"unknown source {self.source!r}")
        if len(set(self.codes.values())) != len(self.codes):
            raise ValueError(f"the codes for {self.source} are not one-to-one")
        return self


class LayerMap(_Strict):
    kind: str
    layer: str
    fields: dict[str, FieldMap]

    @model_validator(mode="after")
    def _kind(self) -> LayerMap:
        if self.kind not in KINDS:
            raise ValueError(f"unknown kind {self.kind!r}; known: {', '.join(KINDS)}")
        return self


class ExportProfile(_Strict):
    format: str = Field("openftth.export", pattern=r"^openftth\.export$")
    version: int = 1
    name: str
    description: str = ""
    driver: Driver
    crs: str = Field(
        "EPSG:4326", description="Output CRS (an EPSG code) or `local` for the design's metric CRS"
    )
    layers: list[LayerMap]

    @model_validator(mode="after")
    def _consistent(self) -> ExportProfile:
        names = [layer.layer for layer in self.layers]
        if len(set(names)) != len(names):
            raise ValueError("two layers share a name")
        if self.crs != "local":
            CRS.from_user_input(self.crs)
        for layer in self.layers:
            if not any(f.source == "uuid" for f in layer.fields.values()):
                raise ValueError(f"layer {layer.layer} needs a field from uuid to be read back")
            if self.driver == "shapefile":
                long = [k for k in layer.fields if len(k) > 10]
                if long:
                    raise ValueError(f"shapefile field names over 10 characters: {', '.join(long)}")
        return self


# ---- profiles on disk ----------------------------------------------------------------------


def exports_dir() -> Path | None:
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "exports"
        if (candidate / "generic-gis.yaml").exists():
            return candidate
    return None


def list_export_profiles() -> list[str]:
    d = exports_dir()
    return sorted(p.stem for p in d.glob("*.yaml")) if d else []


def load_export_profile(path_or_name: str | Path) -> ExportProfile:
    p = Path(path_or_name)
    if not p.exists():
        d = exports_dir()
        if d is None or not (d / f"{path_or_name}.yaml").exists():
            raise FileNotFoundError(f"no export profile named {path_or_name!r}")
        p = d / f"{path_or_name}.yaml"
    with p.open("r", encoding="utf-8") as fh:
        return ExportProfile.model_validate(yaml.safe_load(fh) or {})


# ---- values --------------------------------------------------------------------------------


def _value(f: Any, source: str) -> Any:
    if source == "uuid":
        return f.element_uuid
    if source == "kind":
        return f.kind
    if source == "lifecycle":
        return f.openftth.get("lifecycle")
    head, key = source.split(".", 1)
    return (f.props if head == "props" else f.openftth).get(key)


def _cast(v: Any, typ: FieldType) -> Any:
    if v is None or v == "":
        return None
    if typ == "int":
        return int(float(v))
    if typ == "real":
        return float(v)
    if typ == "bool":
        return v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes")
    return v if isinstance(v, str) else json.dumps(v, sort_keys=True)


def _encode(v: Any, fm: FieldMap) -> Any:
    v = _cast(v, fm.type)
    if fm.codes and v is not None:
        return fm.codes.get(str(v), v)
    return v


def _decode(v: Any, fm: FieldMap) -> Any:
    if v is None or (isinstance(v, float) and v != v):
        return None
    if fm.codes:
        back = {code: value for value, code in fm.codes.items()}
        v = back.get(str(v), v)
    return _cast(v, fm.type)


# ---- writing and reading -------------------------------------------------------------------


def export_with_profile(
    design: Design, profile: ExportProfile, out_dir: str | Path, local_crs: CRS
) -> dict[str, Any]:
    """Write every layer of the profile into `out_dir` (emptied first) plus `export.json`."""
    out = Path(out_dir)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    crs = local_crs if profile.crs == "local" else CRS.from_user_input(profile.crs)
    layers: dict[str, dict[str, Any]] = {}
    for lm in profile.layers:
        feats = sorted(design.by_kind(lm.kind), key=lambda f: f.element_uuid)
        rows = [
            {k: _encode(_value(f, fm.source), fm) for k, fm in lm.fields.items()} for f in feats
        ]
        gdf = gpd.GeoDataFrame(
            rows, columns=list(lm.fields), geometry=[f.geometry for f in feats], crs=4326
        ).to_crs(crs)
        if profile.driver == "gpkg":
            path = out / f"{profile.name}.gpkg"
            if feats:
                gdf.to_file(path, layer=lm.layer, driver="GPKG", mode="a" if path.exists() else "w")
        elif profile.driver == "shapefile":
            path = out / f"{lm.layer}.shp"
            if feats:
                gdf.to_file(path, driver="ESRI Shapefile", engine="pyogrio", encoding="UTF-8")
        else:
            path = out / f"{lm.layer}.csv"
            with path.open("w", encoding="utf-8", newline="") as fh:
                w = csv.writer(fh, lineterminator="\n")
                w.writerow([*lm.fields, "wkt"])
                for row, geom in zip(rows, gdf.geometry, strict=True):
                    cells = ["" if row[k] is None else row[k] for k in lm.fields]
                    w.writerow([*cells, wkt.dumps(geom, rounding_precision=8)])
        layers[lm.layer] = {"kind": lm.kind, "file": path.name, "features": len(feats)}
    manifest = {
        "format": "openftth.export",
        "profile": profile.name,
        "driver": profile.driver,
        "crs": crs.to_string(),
        "layers": layers,
    }
    (out / "export.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def read_with_profile(profile: ExportProfile, out_dir: str | Path) -> dict[str, dict[str, Any]]:
    """Read an export back: element uuid → {kind, values by design source, geometry (WGS 84)}."""
    out = Path(out_dir)
    manifest = json.loads((out / "export.json").read_text(encoding="utf-8"))
    crs = CRS.from_user_input(manifest["crs"])
    found: dict[str, dict[str, Any]] = {}
    for lm in profile.layers:
        info = manifest["layers"].get(lm.layer)
        if not info or not info["features"]:
            continue
        path = out / info["file"]
        if profile.driver == "csv":
            with path.open(encoding="utf-8", newline="") as fh:
                recs = list(csv.DictReader(fh))
            gdf = gpd.GeoDataFrame(
                [{k: (r[k] if r[k] != "" else None) for k in lm.fields} for r in recs],
                columns=list(lm.fields),
                geometry=[wkt.loads(r["wkt"]) for r in recs],
                crs=crs,
            )
        elif profile.driver == "gpkg":
            gdf = gpd.read_file(path, layer=lm.layer)
        else:
            gdf = gpd.read_file(path, encoding="UTF-8")
        gdf = gdf.to_crs(4326)
        uuid_field = next(k for k, fm in lm.fields.items() if fm.source == "uuid")
        for rec, geom in zip(gdf.to_dict("records"), gdf.geometry, strict=True):
            values = {fm.source: _decode(rec.get(k), fm) for k, fm in lm.fields.items()}
            found[str(rec[uuid_field])] = {"kind": lm.kind, "values": values, "geometry": geom}
    return found


def expected_values(design: Design, profile: ExportProfile) -> dict[str, dict[str, Any]]:
    """What a perfect round trip gives back: the design's values cast as the profile says."""
    out: dict[str, dict[str, Any]] = {}
    for lm in profile.layers:
        for f in design.by_kind(lm.kind):
            out[f.element_uuid] = {
                "kind": lm.kind,
                "values": {
                    fm.source: _cast(_value(f, fm.source), fm.type) for fm in lm.fields.values()
                },
                "geometry": f.geometry,
            }
    return out
