"""OpenStreetMap ingest from a local `.osm` XML file (local-first; no network).

Streets (`highway=*` ways) become the trench/route graph. Buildings become the interchange format `building`
features and, in Phase 0, synthetic homes (one `to` per dwelling unit) until the Flemish
address register is wired in.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import geopandas as gpd
import networkx as nx
import osmnx as ox
import pandas as pd
from shapely.geometry import MultiPolygon, Polygon

from ..results import Decision
from ..rules import Rulebook

RESIDENTIAL_UNIT_TAGS = {
    "house",
    "detached",
    "semidetached_house",
    "terrace",
    "bungalow",
    "residential",
    "dormitory",
    "farm",
}
APARTMENT_TAGS = {"apartments", "flats"}
BUSINESS_TAGS = {"commercial", "retail", "office", "school", "hospital", "public", "civic", "hotel"}
EXCLUDED_TAGS = {
    "garage",
    "garages",
    "shed",
    "roof",
    "hut",
    "greenhouse",
    "carport",
    "construction",
    "industrial",
    "warehouse",
    "service",
    "ruins",
    "shelter",
    "kiosk",
    "toilets",
    "transformer_tower",
}
MIN_HOME_FOOTPRINT_M2 = 30.0
MAX_YES_FOOTPRINT_M2 = 400.0
STREET_BUFFER_M = 200.0
MAX_STREET_BUFFER_M = 1600.0
STREET_PIECES_MAX = 0.05  # share of street vertices allowed off the main connected piece
# What a trench cannot cross, besides motorways (which are streets the routing excludes).
BARRIER_TAGS = {
    "railway": ["rail", "light_rail", "narrow_gauge"],
    "waterway": ["river", "canal"],
}


@dataclass
class OsmData:
    streets: gpd.GeoDataFrame  # EPSG:4326 LineStrings with `highway`, `surface`, `osm_id`
    buildings: gpd.GeoDataFrame  # EPSG:4326 Polygons with `building`, `levels`, `flats`, `osm_id`
    street_margin_m: float = 0.0  # how far beyond the area streets were kept
    # Railways and rivers, which a trench cannot cross: `kind` (railway, water), `osm_id`.
    barriers: gpd.GeoDataFrame | None = None


def import_osm(path: str | Path, area: Polygon | MultiPolygon | None = None) -> OsmData:
    path = Path(path)
    streets = _features(path, {"highway": True})
    streets = streets[streets.geometry.geom_type.isin(["LineString", "MultiLineString"])].copy()
    streets = streets.explode(index_parts=False, ignore_index=True)
    keep_cols = [c for c in ("highway", "surface", "name", "oneway") if c in streets.columns]
    streets = streets[keep_cols + ["osm_id", "geometry"]].copy()
    if "surface" not in streets.columns:
        streets["surface"] = None
    if "name" not in streets.columns:
        streets["name"] = None

    buildings = _features(path, {"building": True})
    buildings = buildings[buildings.geometry.geom_type.isin(["Polygon", "MultiPolygon"])].copy()
    cols = {
        "building": "building",
        "building:levels": "levels",
        "building:flats": "flats",
        "addr:street": "street",
        "addr:housenumber": "housenumber",
    }
    for src, dst in cols.items():
        buildings[dst] = buildings[src] if src in buildings.columns else None
    buildings = buildings[list(cols.values()) + ["osm_id", "geometry"]].copy()

    barriers = _barriers(path)
    margin = 0.0
    if area is not None:
        # Keep streets a little beyond the area so homes on its edge can snap to the street
        # they actually face (which may lie just outside the polygon), and widen that margin
        # when the streets only join up outside it.
        streets, margin = streets_around(streets, area)
        buildings = buildings[buildings.representative_point().within(area)].copy()

    if area is not None and not barriers.empty:
        zone = gpd.GeoSeries([area], crs=4326)
        utm = zone.estimate_utm_crs()
        reach = zone.to_crs(utm).buffer(max(margin, STREET_BUFFER_M)).to_crs(4326).iloc[0]
        barriers = barriers[barriers.intersects(reach)]
    return OsmData(
        streets=streets.reset_index(drop=True),
        buildings=buildings.reset_index(drop=True),
        street_margin_m=margin,
        barriers=barriers.reset_index(drop=True),
    )


def _barriers(path: Path) -> gpd.GeoDataFrame:
    """Railway and river lines from the extract, as `kind` (railway, water) and `osm_id`."""
    try:
        gdf = _features(path, BARRIER_TAGS)
    except Exception:  # osmnx raises when the extract has none of these features
        return gpd.GeoDataFrame({"kind": [], "osm_id": []}, geometry=[], crs=4326)
    gdf = gdf[gdf.geometry.geom_type.isin(["LineString", "MultiLineString"])]
    rail = gdf["railway"].isin(BARRIER_TAGS["railway"]) if "railway" in gdf.columns else False
    water = gdf["waterway"].isin(BARRIER_TAGS["waterway"]) if "waterway" in gdf.columns else False
    # A railway in a tunnel or on a bridge does not block a trench beside it.
    lifted = gdf.get("tunnel", None)
    keep = (rail | water) & ~(
        lifted.isin(["yes", "building_passage"]) if lifted is not None else False
    )
    gdf = gdf[keep].copy()
    gdf["kind"] = [
        "railway" if r == r and r in BARRIER_TAGS["railway"] else "water"
        for r in gdf.get("railway", [None] * len(gdf))
    ]
    gdf = gdf[["kind", "osm_id", "geometry"]].explode(index_parts=False, ignore_index=True)
    return gdf.sort_values("osm_id", kind="stable").reset_index(drop=True)


def off_main_share(streets: gpd.GeoDataFrame) -> float:
    """Share of street vertices outside the largest connected piece (0 when it is one piece)."""
    if streets.empty:
        return 0.0
    utm = streets.estimate_utm_crs()
    g = nx.Graph()
    for geom in streets.to_crs(utm).geometry:
        for line in getattr(geom, "geoms", [geom]):
            cs = [(round(x, 2), round(y, 2)) for x, y, *_ in line.coords]
            g.add_edges_from(zip(cs[:-1], cs[1:], strict=False))
    if g.number_of_nodes() == 0:
        return 0.0
    main = max(len(c) for c in nx.connected_components(g))
    return 1.0 - main / g.number_of_nodes()


def streets_around(
    streets: gpd.GeoDataFrame, area: Polygon | MultiPolygon
) -> tuple[gpd.GeoDataFrame, float]:
    """The streets within a margin of the area, and that margin in metres.

    The margin starts at STREET_BUFFER_M and doubles (up to MAX_STREET_BUFFER_M) while more than
    STREET_PIECES_MAX of the network lies in pieces that only connect further out: routing
    cannot reach homes on those pieces, and they end up with long drops to the main piece.
    """
    area_s = gpd.GeoSeries([area], crs=4326)
    utm = area_s.estimate_utm_crs()
    margin = STREET_BUFFER_M
    while True:
        zone = area_s.to_crs(utm).buffer(margin).to_crs(4326).iloc[0]
        kept = streets[streets.intersects(zone)].copy()
        if margin >= MAX_STREET_BUFFER_M or off_main_share(kept) <= STREET_PIECES_MAX:
            return kept, margin
        margin *= 2


def _features(path: Path, tags: dict) -> gpd.GeoDataFrame:
    gdf = ox.features_from_xml(str(path), tags=tags)
    gdf = gdf.reset_index()
    # osmnx 2.x uses a (element, id) MultiIndex; older versions (element_type, osmid).
    id_col = "id" if "id" in gdf.columns else "osmid"
    el_col = "element" if "element" in gdf.columns else "element_type"
    gdf["osm_id"] = gdf[el_col].astype(str).str[0] + gdf[id_col].astype(str)
    if gdf.crs is None:
        gdf = gdf.set_crs(4326)
    return gdf


# ---- synthetic homes (Phase 0) -----------------------------------------------------------


def homes_from_buildings(
    buildings: gpd.GeoDataFrame, rules: Rulebook
) -> tuple[gpd.GeoDataFrame, list[Decision]]:
    """One synthetic home per building with an estimated number of dwelling units.

    Returns homes in the buildings' CRS with columns `osm_id`, `units`, `kind`, `geometry`
    (representative point) and the decisions explaining the estimates.
    """
    if buildings.empty:
        return gpd.GeoDataFrame(
            columns=["osm_id", "units", "kind", "geometry"], geometry="geometry", crs=buildings.crs
        ), []

    metric = buildings.to_crs(buildings.estimate_utm_crs())
    areas = metric.geometry.area
    rows = []
    for (_idx, b), area_m2 in zip(buildings.iterrows(), areas, strict=True):
        tag = str(b.get("building") or "yes").lower()
        if tag in EXCLUDED_TAGS or area_m2 < MIN_HOME_FOOTPRINT_M2:
            continue
        levels = _to_int(b.get("levels"))
        flats = _to_int(b.get("flats"))
        if tag in APARTMENT_TAGS:
            if flats:
                units, kind = flats, "apartments"
            else:
                lv = levels or 3
                est = int(round(area_m2 * lv / rules.homes.m2_per_unit))
                units, kind = max(rules.homes.default_units_apartment, est), "apartments"
        elif tag in RESIDENTIAL_UNIT_TAGS:
            units, kind = 1, "house"
        elif tag in BUSINESS_TAGS:
            units, kind = 1, "business"
        elif tag == "yes":
            if area_m2 > MAX_YES_FOOTPRINT_M2:
                continue
            units, kind = 1, "house"
        else:
            continue
        if not rules.homes.count_units:
            units = 1
        rows.append(
            {
                "osm_id": b["osm_id"],
                "units": int(units),
                "kind": kind,
                "geometry": b.geometry.representative_point(),
            }
        )

    homes = gpd.GeoDataFrame(rows, geometry="geometry", crs=buildings.crs)
    n_apart = int((homes["kind"] == "apartments").sum()) if not homes.empty else 0
    decisions = [
        Decision(
            stage="ingest",
            tool="import_osm",
            reason=(
                "No address register loaded: homes are synthesised from OSM building footprints. "
                "Houses count 1 unit; apartment blocks use building:flats, else "
                f"footprint × levels / {rules.homes.m2_per_unit:g} m² "
                f"(min {rules.homes.default_units_apartment}); untagged buildings over "
                f"{MAX_YES_FOOTPRINT_M2:g} m² or under {MIN_HOME_FOOTPRINT_M2:g} m² are skipped."
            ),
            values={
                "homes": int(len(homes)),
                "units": int(homes["units"].sum()) if not homes.empty else 0,
                "apartment_buildings": n_apart,
            },
            source="assumption",
        )
    ]
    return homes, decisions


def _to_int(v) -> int | None:
    if (
        v is None
        or (isinstance(v, float) and math.isnan(v))
        or (isinstance(v, str) and not v.strip())
    ):
        return None
    try:
        return int(float(str(v).split(";")[0]))
    except ValueError:
        return None


def ensure_geodataframe(df: pd.DataFrame, crs) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(df, geometry="geometry", crs=crs)
