"""Read any vector source a designer brings: GeoJSON, GeoPackage, Shapefile, KML, FlatGeobuf,
GML, MapInfo, DXF and CSV (x/y or WKT columns), in any coordinate system, through GDAL.

`list_layers` tells what is in a file (layers, geometry, size, CRS, columns, a few sample
values); `read_layer` returns one layer reprojected to WGS84. CSV files are read with pandas so
coordinate columns can be detected by name, and their CRS can be given when the file has none.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import geopandas as gpd
import pandas as pd
import pyogrio
from shapely import wkt as shapely_wkt

VECTOR_SUFFIXES = {
    ".geojson",
    ".json",
    ".gpkg",
    ".shp",
    ".kml",
    ".kmz",
    ".fgb",
    ".gml",
    ".tab",
    ".mif",
    ".dxf",
    ".csv",
    ".txt",
    ".osm",
    ".pbf",
}

X_NAMES = (
    "lon",
    "lng",
    "long",
    "longitude",
    "x",
    "easting",
    "east",
    "x_coord",
    "xcoord",
    "point_x",
)
Y_NAMES = ("lat", "latitude", "y", "northing", "north", "y_coord", "ycoord", "point_y")
WKT_NAMES = ("wkt", "geometry", "geom", "the_geom", "shape")


@dataclass
class LayerInfo:
    path: str
    layer: str
    geometry: str  # Point, LineString, Polygon, Multi…, or "None"
    count: int
    crs: str | None  # e.g. "EPSG:31370"; None when the file does not say
    columns: list[str]
    samples: dict[str, list[str]] = field(default_factory=dict)
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _crs_text(crs: Any) -> str | None:
    if crs is None:
        return None
    try:
        from pyproj import CRS

        c = CRS.from_user_input(crs)
        epsg = c.to_epsg()
        return f"EPSG:{epsg}" if epsg else c.to_string()
    except Exception:  # noqa: BLE001 — an unreadable CRS is reported, not fatal
        return str(crs)


def _csv_frame(path: Path) -> pd.DataFrame:
    for sep in (",", ";", "\t", "|"):
        df = pd.read_csv(path, sep=sep, dtype=str, keep_default_na=False, encoding_errors="replace")
        if len(df.columns) > 1:
            return df
    return df


def _pick(columns: list[str], names: tuple[str, ...]) -> str | None:
    low = {c.lower().strip(): c for c in columns}
    for n in names:
        if n in low:
            return low[n]
    return None


def csv_coordinates(columns: list[str]) -> tuple[str | None, str | None, str | None]:
    """Columns holding x, y (or a WKT geometry) in a table, guessed from their names."""
    return _pick(columns, X_NAMES), _pick(columns, Y_NAMES), _pick(columns, WKT_NAMES)


def _samples(df: pd.DataFrame, columns: list[str], n: int = 3) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for c in columns[:40]:
        vals = [str(v) for v in df[c].dropna().astype(str).head(50) if str(v).strip()]
        seen: list[str] = []
        for v in vals:
            if v not in seen:
                seen.append(v[:40])
            if len(seen) >= n:
                break
        out[c] = seen
    return out


def list_layers(path: str | Path) -> list[LayerInfo]:
    """Every layer in a file with what a designer needs to decide what it is."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"no such file: {p}")
    if p.suffix.lower() in (".osm", ".pbf"):
        # One source: streets and buildings are read from it by the OSM importer.
        return [
            LayerInfo(
                path=str(p),
                layer="osm",
                geometry="OpenStreetMap",
                count=-1,
                crs="EPSG:4326",
                columns=[],
                note=f"OpenStreetMap extract, {p.stat().st_size // 1024} kB: streets and buildings",
            )
        ]
    if p.suffix.lower() in (".csv", ".txt"):
        df = _csv_frame(p)
        cols = [str(c) for c in df.columns]
        x, y, w = csv_coordinates(cols)
        geom = "Point" if (x and y) else ("WKT" if w else "None")
        note = ""
        if geom == "None":
            note = "no coordinate columns found; name them lon/lat or x/y, or add a WKT column"
        elif x and y:
            note = f"coordinates from {x}, {y}"
        return [
            LayerInfo(
                path=str(p),
                layer=p.stem,
                geometry=geom,
                count=len(df),
                crs=None,
                columns=cols,
                samples=_samples(df, cols),
                note=note,
            )
        ]
    out: list[LayerInfo] = []
    for name, gtype in pyogrio.list_layers(str(p)):
        info = pyogrio.read_info(str(p), layer=name)
        cols = [str(c) for c in info.get("fields", [])]
        sample = pyogrio.read_dataframe(str(p), layer=name, max_features=50, read_geometry=False)
        out.append(
            LayerInfo(
                path=str(p),
                layer=str(name),
                geometry=str(gtype or info.get("geometry_type") or "None"),
                count=int(info.get("features", -1)),
                crs=_crs_text(info.get("crs")),
                columns=cols,
                samples=_samples(sample, [c for c in cols if c in sample.columns]),
            )
        )
    return out


def read_layer(
    path: str | Path,
    layer: str | None = None,
    crs: str | None = None,
    x: str | None = None,
    y: str | None = None,
) -> gpd.GeoDataFrame:
    """One layer in WGS84. `crs` is used when the source has none (or overrides a wrong one);
    for tables, `x`/`y` name the coordinate columns (guessed when omitted)."""
    p = Path(path)
    if p.suffix.lower() in (".csv", ".txt"):
        df = _csv_frame(p)
        cols = [str(c) for c in df.columns]
        gx, gy, gw = csv_coordinates(cols)
        x, y = x or gx, y or gy
        if x and y:
            xs = pd.to_numeric(df[x].str.replace(",", ".", regex=False), errors="coerce")
            ys = pd.to_numeric(df[y].str.replace(",", ".", regex=False), errors="coerce")
            keep = xs.notna() & ys.notna()
            df = df[keep].copy()
            gdf = gpd.GeoDataFrame(
                df,
                geometry=gpd.points_from_xy(xs[keep], ys[keep]),
                crs=crs or _guess_xy_crs(xs, ys),
            )
        elif gw:
            gdf = gpd.GeoDataFrame(
                df, geometry=df[gw].map(shapely_wkt.loads), crs=crs or "EPSG:4326"
            )
        else:
            raise ValueError(f"{p.name}: no coordinate columns; name them lon/lat or x/y")
    else:
        gdf = pyogrio.read_dataframe(str(p), layer=layer)
        if crs:
            gdf = gdf.set_crs(crs, allow_override=True)
        elif gdf.crs is None:
            gdf = gdf.set_crs(_guess_bounds_crs(gdf))
    gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty].copy()
    if gdf.crs is not None and gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(4326)
    return gdf.reset_index(drop=True)


def _guess_xy_crs(xs: pd.Series, ys: pd.Series) -> str:
    """Degrees when the values look like longitudes and latitudes; otherwise the caller must
    say (a projected table without a CRS cannot be placed safely)."""
    if xs.between(-180, 180).all() and ys.between(-90, 90).all():
        return "EPSG:4326"
    raise ValueError(
        "the coordinates are not degrees and the file has no CRS; give the CRS (for example "
        "EPSG:31370 for Belgian Lambert 72, EPSG:27700 for British National Grid)"
    )


def _guess_bounds_crs(gdf: gpd.GeoDataFrame) -> str:
    if gdf.empty:
        return "EPSG:4326"
    minx, miny, maxx, maxy = gdf.total_bounds
    if -180 <= minx <= 180 and -180 <= maxx <= 180 and -90 <= miny <= 90 and -90 <= maxy <= 90:
        return "EPSG:4326"
    raise ValueError("the file has no CRS and its coordinates are not degrees; give the CRS")
