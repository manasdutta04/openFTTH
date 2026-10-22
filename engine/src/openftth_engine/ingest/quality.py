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

    def issue(level: str, code: str, message: str, count: int = 0, **extra: Any) -> None:
        issues.append({"level": level, "code": code, "message": message, "count": count, **extra})

    if streets.empty:
        issue("error", "no_streets", "No streets inside or around the area.")
    if homes.empty:
        issue("error", "no_homes", "No homes: add an address register or buildings.")

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
                f"{dup} home(s) share their exact location with another; check for duplicate addresses.",
                dup,
            )
        zero = int((inp.homes["units"] <= 0).sum())
        if zero:
            issue("error", "no_units", f"{zero} home(s) have no units.", zero)

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
            )

    level = (
        "error"
        if any(i["level"] == "error" for i in issues)
        else ("warning" if any(i["level"] == "warning" for i in issues) else "ok")
    )
    return {"level": level, "counts": inp.counts, "issues": issues}
