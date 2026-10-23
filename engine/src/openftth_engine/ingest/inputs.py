"""Turn an import profile into what a design needs: the area, streets (with existing ducts as
cheap routes), buildings, homes with their units, existing poles and manholes, and no-dig
zones. Every choice made while reading is recorded as a decision.

Homes come from the address register when one is given (one home per address, units from the
register); otherwise they are estimated from building footprints. OpenStreetMap is one source
among others, used for whatever the profile does not provide.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely.geometry import MultiPolygon, Polygon
from shapely.ops import unary_union

from ..results import Decision
from ..rules import Rulebook
from .osm import STREET_BUFFER_M, homes_from_buildings, import_osm, streets_around
from .profiles import Binding, ImportProfile
from .sources import read_layer

ROAD_CLASSES = {
    "motorway",
    "trunk",
    "primary",
    "secondary",
    "tertiary",
    "unclassified",
    "residential",
    "living_street",
    "service",
    "pedestrian",
    "footway",
    "cycleway",
    "path",
    "track",
}
SURFACE_CLASSES = {"asphalt", "pavement", "verge", "aerial", "existing_duct"}


@dataclass
class Inputs:
    area: Polygon | MultiPolygon
    streets: gpd.GeoDataFrame  # highway, surface, surface_class, osm_id, name
    buildings: gpd.GeoDataFrame  # building, levels, flats, street, housenumber, osm_id
    homes: gpd.GeoDataFrame  # osm_id, units, kind, building_id, geometry (points)
    poles: gpd.GeoDataFrame
    manholes: gpd.GeoDataFrame
    no_dig: gpd.GeoDataFrame  # polygons with a reason
    barriers: gpd.GeoDataFrame | None = None  # railways and rivers: kind, osm_id, lines
    decisions: list[Decision] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)


def _empty(cols: list[str]) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {**{c: [] for c in cols}, "geometry": []}, geometry="geometry", crs=4326
    )


def _col(gdf: gpd.GeoDataFrame, b: Binding, attr: str) -> pd.Series:
    src = b.columns.get(attr)
    if src and src in gdf.columns:
        s = gdf[src]
        mapping = b.values.get(attr)
        if mapping:
            s = s.map(lambda v: mapping.get(str(v), v))
        return s
    return pd.Series([None] * len(gdf), index=gdf.index, dtype=object)


def _ids(gdf: gpd.GeoDataFrame, b: Binding, prefix: str) -> pd.Series:
    """Stable ids: the source id column when given, else the row number, both namespaced by
    the file so two sources can never collide."""
    stem = Path(b.path).stem
    raw = _col(gdf, b, "id")
    return pd.Series(
        [
            f"{prefix}:{stem}:{v}"
            if v not in (None, "") and str(v) != "nan"
            else f"{prefix}:{stem}:{i}"
            for i, v in zip(range(len(gdf)), raw, strict=False)
        ],
        index=gdf.index,
    )


def _read(b: Binding) -> gpd.GeoDataFrame:
    return read_layer(b.path, b.layer, crs=b.crs, x=b.x, y=b.y)


def _group_by_building(homes: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """One home per building: addresses with the same building id in the register, else at the
    same point (flats sharing an entrance), are merged with their units added. The OSM outline
    is not used: semi-detached and terraced houses are often one outline there, and each house
    needs its own drop. The home keeps the smallest address id and the point of its first
    address, so the result is deterministic."""
    if homes.empty:
        return homes

    def key(row) -> str:
        ref = row["building_ref"]
        if isinstance(ref, str) and ref.strip() and ref.lower() != "nan":
            return f"ref:{ref.strip()}"
        return f"pt:{row.geometry.x:.7f},{row.geometry.y:.7f}"

    homes = homes.assign(_key=[key(r) for _, r in homes.iterrows()])
    homes = homes.sort_values(["_key", "osm_id"], kind="stable")
    rows = []
    for _k, g in homes.groupby("_key", sort=True):
        first = g.iloc[0]
        units = int(g["units"].sum())
        rows.append(
            {
                "osm_id": first["osm_id"],
                "units": units,
                "kind": first["kind"]
                if len(g) == 1
                else ("apartments" if units > 1 else first["kind"]),
                "building_ref": first["building_ref"],
                "building_id": first["building_id"],
                "street": first["street"],
                "housenumber": first["housenumber"],
                "geometry": first.geometry,
            }
        )
    out = gpd.GeoDataFrame(rows, geometry="geometry", crs=homes.crs)
    return out.sort_values("osm_id", kind="stable").reset_index(drop=True)


def _zone_kind(value) -> str:
    """`keep_out` (no trench and no equipment) when a zone's kind says so, else `no_dig` (no new
    trench; existing ducts through it stay usable)."""
    v = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return (
        "keep_out"
        if v in ("keep_out", "keepout", "exclusion", "excluded", "no_equipment")
        else "no_dig"
    )


def _to_int(v) -> int | None:
    try:
        n = int(float(str(v).replace(",", ".")))
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def load_inputs(profile: ImportProfile, rules: Rulebook) -> Inputs:
    decisions: list[Decision] = []
    counts: dict[str, int] = {}

    # ---- area ------------------------------------------------------------------------------
    parts = []
    for b in profile.by_role("area"):
        gdf = _read(b)
        parts.extend(g for g in gdf.geometry if g.geom_type in ("Polygon", "MultiPolygon"))
    if not parts:
        raise ValueError("the import has no design area; mark a polygon layer as the area")
    area = unary_union(parts)
    area_s = gpd.GeoSeries([area], crs=4326)
    utm = area_s.estimate_utm_crs()
    street_zone = area_s.to_crs(utm).buffer(STREET_BUFFER_M).to_crs(4326).iloc[0]

    # ---- OpenStreetMap, when given ------------------------------------------------------------
    osm_streets = osm_buildings = None
    barriers = _empty(["kind", "osm_id"])
    for b in profile.by_role("osm"):
        osm = import_osm(b.path, area)
        if osm.barriers is not None and not osm.barriers.empty:
            barriers = osm.barriers
            counts["barriers"] = len(barriers)
        osm_streets, osm_buildings = osm.streets.copy(), osm.buildings.copy()
        osm_streets["surface_class"] = None
        if osm.street_margin_m > STREET_BUFFER_M:
            decisions.append(
                Decision(
                    stage="ingest",
                    tool="import_data",
                    reason=f"Streets kept up to {osm.street_margin_m:.0f} m beyond the area instead of {STREET_BUFFER_M:.0f} m: nearer in, the street network was in pieces that only join further out, and homes on them could not be routed.",
                    values={"street_margin_m": osm.street_margin_m},
                    source="solver",
                )
            )
        counts["osm_streets"] = len(osm_streets)
        counts["osm_buildings"] = len(osm_buildings)

    # ---- streets ---------------------------------------------------------------------------
    street_parts = []
    for b in profile.by_role("streets"):
        gdf = _read(b)
        gdf = gdf[gdf.geometry.geom_type.isin(["LineString", "MultiLineString"])]
        gdf = gdf.explode(index_parts=False, ignore_index=True)
        gdf, margin = streets_around(gdf, area)
        cls = _col(gdf, b, "class").astype(object)
        highway = [str(c).lower() if str(c).lower() in ROAD_CLASSES else "residential" for c in cls]
        surf = [
            str(s).lower() if str(s).lower() in SURFACE_CLASSES else None
            for s in _col(gdf, b, "surface")
        ]
        street_parts.append(
            gpd.GeoDataFrame(
                {
                    "highway": highway,
                    "surface": None,
                    "surface_class": surf,
                    "name": _col(gdf, b, "name").values,
                    "osm_id": _ids(gdf, b, "street").values,
                },
                geometry=gdf.geometry.values,
                crs=4326,
            )
        )
        unknown = int(sum(1 for c in cls if str(c).lower() not in ROAD_CLASSES))
        decisions.append(
            Decision(
                stage="ingest",
                tool="import_data",
                reason=f"{len(gdf)} street segment(s) from {Path(b.path).name}"
                + (
                    f"; {unknown} without a known road class were treated as residential streets"
                    if unknown
                    else ""
                )
                + (
                    f"; kept up to {margin:.0f} m beyond the area so the network joins up"
                    if margin > STREET_BUFFER_M
                    else ""
                ),
                values={"streets": len(gdf), "unclassified": unknown, "street_margin_m": margin},
                source="assumption" if unknown else "rulebook",
            )
        )
    if street_parts:
        streets = pd.concat(street_parts, ignore_index=True)
    elif osm_streets is not None:
        streets = osm_streets
    else:
        raise ValueError(
            "the import has no streets: add a streets layer or an OpenStreetMap extract "
            "(fetch_osm downloads one for the area)"
        )
    if street_parts and osm_streets is not None:
        decisions.append(
            Decision(
                stage="ingest",
                tool="import_data",
                reason="Streets come from the supplied layer; OpenStreetMap streets were not used.",
                source="user",
            )
        )

    # ---- existing ducts become cheap routes ---------------------------------------------------
    duct_parts = []
    for b in profile.by_role("existing_ducts"):
        gdf = _read(b)
        gdf = gdf[gdf.geometry.geom_type.isin(["LineString", "MultiLineString"])]
        gdf = gdf.explode(index_parts=False, ignore_index=True)
        gdf = gdf[gdf.intersects(street_zone)]
        duct_parts.append(
            gpd.GeoDataFrame(
                {
                    "highway": "existing_duct",
                    "surface": None,
                    "surface_class": "existing_duct",
                    "name": _col(gdf, b, "owner").values,
                    "osm_id": _ids(gdf, b, "duct").values,
                },
                geometry=gdf.geometry.values,
                crs=4326,
            )
        )
    if duct_parts:
        ducts = pd.concat(duct_parts, ignore_index=True)
        counts["existing_ducts"] = len(ducts)
        streets = pd.concat([streets, ducts], ignore_index=True)
        decisions.append(
            Decision(
                stage="ingest",
                tool="import_data",
                reason=f"{len(ducts)} existing duct section(s) join the route graph at the "
                f"rulebook's existing-duct cost, so routes reuse them where that is cheaper.",
                values={"existing_ducts": len(ducts)},
                source="user",
            )
        )
    streets = gpd.GeoDataFrame(streets, geometry="geometry", crs=4326)

    # ---- buildings -------------------------------------------------------------------------
    b_parts = []
    for b in profile.by_role("buildings"):
        gdf = _read(b)
        gdf = gdf[gdf.geometry.geom_type.isin(["Polygon", "MultiPolygon"])]
        gdf = gdf[gdf.representative_point().within(area)]
        b_parts.append(
            gpd.GeoDataFrame(
                {
                    "building": [
                        str(u).lower() if u not in (None, "") else "yes"
                        for u in _col(gdf, b, "use")
                    ],
                    "levels": _col(gdf, b, "levels").values,
                    "flats": _col(gdf, b, "units").values,
                    "street": None,
                    "housenumber": None,
                    "osm_id": _ids(gdf, b, "bld").values,
                },
                geometry=gdf.geometry.values,
                crs=4326,
            )
        )
    if b_parts:
        buildings = gpd.GeoDataFrame(
            pd.concat(b_parts, ignore_index=True), geometry="geometry", crs=4326
        )
    elif osm_buildings is not None:
        buildings = osm_buildings
    else:
        buildings = _empty(["building", "levels", "flats", "street", "housenumber", "osm_id"])

    # ---- homes: the address register, else building footprints ---------------------------------
    a_parts = []
    for b in profile.by_role("addresses"):
        gdf = _read(b)
        if not gdf.empty and not gdf.geometry.geom_type.eq("Point").all():
            gdf = gdf.set_geometry(gdf.geometry.representative_point())
        gdf = gdf[gdf.within(area)]
        units = [_to_int(u) or 1 for u in _col(gdf, b, "units")]
        kinds = [
            str(k).lower()
            if k not in (None, "") and str(k) != "nan"
            else ("apartments" if u > 1 else "house")
            for k, u in zip(_col(gdf, b, "kind"), units, strict=False)
        ]
        a_parts.append(
            gpd.GeoDataFrame(
                {
                    "osm_id": _ids(gdf, b, "addr").values,
                    "units": units,
                    "kind": kinds,
                    "building_ref": _col(gdf, b, "building_id").values,
                    "street": _col(gdf, b, "street").values,
                    "housenumber": _col(gdf, b, "housenumber").values,
                },
                geometry=gdf.geometry.values,
                crs=4326,
            )
        )
    if a_parts:
        homes = gpd.GeoDataFrame(
            pd.concat(a_parts, ignore_index=True), geometry="geometry", crs=4326
        )
        homes = homes.drop_duplicates(subset="osm_id").reset_index(drop=True)
        # Link each address to the building it stands in, for exclusions and the map.
        if not buildings.empty:
            joined = gpd.sjoin(
                homes[["osm_id", "geometry"]],
                buildings[["osm_id", "geometry"]].rename(columns={"osm_id": "bid"}),
                how="left",
                predicate="within",
            ).drop_duplicates(subset="osm_id")
            homes["building_id"] = joined["bid"].values
        else:
            homes["building_id"] = None
        homes["building_id"] = [b if isinstance(b, str) else None for b in homes["building_id"]]
        n_addresses = len(homes)
        homes = _group_by_building(homes)
        grouped = n_addresses - len(homes)
        decisions.append(
            Decision(
                stage="ingest",
                tool="import_data",
                reason=f"{len(homes)} homes with {int(homes['units'].sum())} units from the "
                "address register; demand is not estimated from building footprints."
                + (
                    f" {n_addresses} addresses: those in the same building (by the register's "
                    "building id, else at the same point) are one home with their units added, "
                    "so a block of flats gets one connection and each house its own."
                    if grouped
                    else ""
                ),
                values={
                    "homes": len(homes),
                    "units": int(homes["units"].sum()),
                    "addresses": n_addresses,
                },
                source="user",
            )
        )
        counts["addresses"] = n_addresses
        homes = homes[["osm_id", "units", "kind", "building_id", "geometry"]]
    else:
        homes, home_decisions = homes_from_buildings(buildings, rules)
        homes = homes.copy()
        homes["building_id"] = homes["osm_id"] if not homes.empty else []
        decisions.extend(home_decisions)

    # ---- existing poles, manholes and no-dig zones ---------------------------------------------
    def points(role: str, prefix: str, extra: str) -> gpd.GeoDataFrame:
        parts = []
        for b in profile.by_role(role):
            gdf = _read(b)
            gdf = gdf.set_geometry(gdf.geometry.representative_point())
            gdf = gdf[gdf.within(street_zone)]
            parts.append(
                gpd.GeoDataFrame(
                    {"osm_id": _ids(gdf, b, prefix).values, extra: _col(gdf, b, extra).values},
                    geometry=gdf.geometry.values,
                    crs=4326,
                )
            )
        if not parts:
            return _empty(["osm_id", extra])
        return gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), geometry="geometry", crs=4326)

    poles = points("poles", "pole", "owner")
    manholes = points("manholes", "mh", "type")
    nd_parts = []
    for b in profile.by_role("no_dig_zones"):
        gdf = _read(b)
        gdf = gdf[gdf.geometry.geom_type.isin(["Polygon", "MultiPolygon"])]
        nd_parts.append(
            gpd.GeoDataFrame(
                {
                    "reason": [
                        str(r) if r not in (None, "") else "no-dig zone"
                        for r in _col(gdf, b, "reason")
                    ],
                    "kind": [_zone_kind(k) for k in _col(gdf, b, "kind")],
                },
                geometry=gdf.geometry.values,
                crs=4326,
            )
        )
    no_dig = (
        gpd.GeoDataFrame(pd.concat(nd_parts, ignore_index=True), geometry="geometry", crs=4326)
        if nd_parts
        else _empty(["reason", "kind"])
    )
    counts.update(
        {
            "streets": len(streets),
            "buildings": len(buildings),
            "homes": len(homes),
            "units": int(homes["units"].sum()) if not homes.empty else 0,
            "poles": len(poles),
            "manholes": len(manholes),
            "no_dig_zones": len(no_dig),
        }
    )
    if len(no_dig):
        decisions.append(
            Decision(
                stage="ingest",
                tool="import_data",
                reason=f"{len(no_dig)} no-dig zone(s): no new trench is planned inside them.",
                values={"no_dig_zones": len(no_dig)},
                source="user",
            )
        )
    return Inputs(
        area=area,
        streets=streets.reset_index(drop=True),
        buildings=buildings.reset_index(drop=True),
        homes=gpd.GeoDataFrame(homes, geometry="geometry", crs=4326).reset_index(drop=True),
        poles=poles,
        manholes=manholes,
        no_dig=no_dig,
        barriers=barriers,
        decisions=decisions,
        counts=counts,
    )


def profile_for(area_path: str | Path, osm_path: str | Path | None) -> ImportProfile:
    """The profile behind the classic two-file start: an area polygon and an OSM extract."""
    b = [Binding(path=str(Path(area_path).resolve()), role="area")]
    if osm_path:
        b.append(Binding(path=str(Path(osm_path).resolve()), role="osm"))
    return ImportProfile(name=Path(area_path).stem, bindings=b)
