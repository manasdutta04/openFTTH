"""A data-quality report on a project's inputs, before anything is designed: what was read, and
what will make the design wrong or impossible (homes far from any street, duplicate addresses,
buildings with no home, a street network in pieces)."""

from __future__ import annotations

from typing import Any

import geopandas as gpd
import networkx as nx
from shapely.geometry import LineString

from ..rules import Rulebook
from .inputs import Inputs
from .osm import APARTMENT_TAGS, RESIDENTIAL_UNIT_TAGS


def check_inputs(inp: Inputs, rules: Rulebook) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    utm = gpd.GeoSeries([inp.area], crs=4326).estimate_utm_crs()
    streets = inp.streets[inp.streets["highway"] != "existing_duct"].to_crs(utm)
    homes = inp.homes.to_crs(utm)

    def issue(
        level: str, code: str, message: str, count: int = 0, fix: str = "", **extra: Any
    ) -> None:
        """One finding: what is wrong, and `fix`, what the engineer can do about it."""
        issues.append(
            {"level": level, "code": code, "message": message, "count": count, "fix": fix, **extra}
        )

    if streets.empty:
        issue(
            "error",
            "no_streets",
            "No streets inside or within reach of the area.",
            fix="Add a streets layer or an OpenStreetMap extract that covers the area. If you "
            "imported a file without a CRS, check its CRS: an area in the wrong place finds nothing.",
        )
    if homes.empty:
        issue(
            "error",
            "no_homes",
            "No homes in the area.",
            fix="Add an address register (points with units) or a buildings layer; an "
            "OpenStreetMap extract only gives homes where buildings are mapped.",
        )

    # A very large area designs slowly; it is better split into areas of a few thousand homes.
    area_km2 = float(gpd.GeoSeries([inp.area], crs=4326).to_crs(utm).area.iloc[0]) / 1e6
    units = int(inp.homes["units"].sum()) if not inp.homes.empty else 0
    if area_km2 > 30 or units > 60_000:
        issue(
            "warning",
            "large_area",
            f"The area is {area_km2:.1f} km² with {units:,} units; designing it may take a long time.",
            fix="Split it into areas of up to about 20,000 units (one per POP catchment) and design "
            "them one by one.",
        )
    # Homes farther than a drop can reach from any street.
    if not streets.empty and not homes.empty:
        lines = streets.geometry.union_all()
        far = homes[homes.geometry.distance(lines) > rules.distances.max_drop_m]
        if len(far):
            issue(
                "warning",
                "homes_far_from_street",
                f"{len(far)} home(s) lie farther than max_drop_m ({rules.distances.max_drop_m:g} m) "
                "from any street; their drops will exceed the limit unless a street is missing.",
                len(far),
                fix="Add the missing street or private lane (a streets layer), check those "
                "addresses' positions, or raise max_drop_m in the rulebook if such drops are built.",
                sample=[str(x) for x in far["osm_id"].head(10)],
            )

    # Two homes on the same spot: usually a register listing one address twice.
    if not homes.empty:
        keys = homes.geometry.apply(lambda g: (round(g.x, 1), round(g.y, 1)))
        dup = int(keys.duplicated().sum())
        if dup:
            issue(
                "warning",
                "duplicate_locations",
                f"{dup} home(s) share their exact location with another.",
                dup,
                fix="Check the register for addresses listed twice; flats in one building should "
                "share a building id so they become one home with several units.",
            )
        zero = int((inp.homes["units"] <= 0).sum())
        if zero:
            issue(
                "error",
                "no_units",
                f"{zero} home(s) have no units.",
                zero,
                fix="Map the register's units column (number of dwellings) or leave it unmapped "
                "so each address counts as one unit.",
            )

    # Residential buildings without any home when homes come from an address register.
    if inp.counts.get("addresses") and not inp.buildings.empty:
        used = set(inp.homes["building_id"].dropna().astype(str))
        resid = inp.buildings[
            inp.buildings["building"]
            .astype(str)
            .str.lower()
            .isin(RESIDENTIAL_UNIT_TAGS | APARTMENT_TAGS)
        ]
        empty = int((~resid["osm_id"].astype(str).isin(used)).sum())
        if empty:
            issue(
                "info",
                "buildings_without_address",
                f"{empty} residential building(s) have no address in the register; they will not be served.",
                empty,
                fix="Usually sheds and garages mapped as houses. If they are homes, add them to the "
                "register or design from buildings instead.",
            )

    # The street network in pieces: homes on a small piece cannot be reached.
    if not streets.empty:
        g = nx.Graph()
        for geom in streets.geometry:
            if isinstance(geom, LineString):
                cs = list(geom.coords)
                for a, b in zip(cs[:-1], cs[1:], strict=False):
                    g.add_edge((round(a[0], 1), round(a[1], 1)), (round(b[0], 1), round(b[1], 1)))
        comps = sorted(nx.connected_components(g), key=len, reverse=True)
        if len(comps) > 1:
            small = sum(len(c) for c in comps[1:])
            share = small / max(1, g.number_of_nodes())
            issue(
                "warning" if share > 0.05 else "info",
                "street_fragments",
                f"The street network is in {len(comps)} pieces; {share:.0%} of its vertices are off "
                "the main piece and will not be used for routing.",
                len(comps) - 1,
                fix="Usually private roads or paths that do not join the street layer; add the "
                "joining streets if homes depend on them.",
            )

    level = (
        "error"
        if any(i["level"] == "error" for i in issues)
        else ("warning" if any(i["level"] == "warning" for i in issues) else "ok")
    )
    return {"level": level, "counts": inp.counts, "issues": issues}
