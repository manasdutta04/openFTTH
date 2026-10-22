"""Phase P2: priced crossings under motorways, railways, rivers and obstacles the map leaves out.

The synthetic town is two street grids on either side of a railway, joined only by a road far
to the east; the Gent sample is split by a canal its extract does not contain.
"""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import networkx as nx
import pytest
from shapely.geometry import LineString, box

from openftth_engine.deliver.boq import build_boq
from openftth_engine.pipeline import (
    assign_fibers,
    assign_pon,
    build_twin_tool,
    cluster_homes,
    import_area_and_osm,
    open_session,
    place_chambers,
    place_fdh,
    route_distribution,
    route_drops,
    route_feeder,
    set_pop,
    size_cables,
)
from openftth_engine.twin import build_twin

GENT = Path(__file__).resolve().parents[2] / "fixtures" / "gent"
LAMBERT = "EPSG:31370"
X0, Y0 = 104000.0, 194000.0


def _wgs(geoms, **cols) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(cols, geometry=geoms, crs=LAMBERT).to_crs(4326)


def _town() -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """Streets north (y ≥ 30) and south (y ≤ -30) of a railway along y = 0, 60 m apart, joined by
    one road 2.5 km east."""
    lines, names = [], []
    for side, y in (("n", 30.0), ("s", -30.0)):
        lines.append(LineString([(X0, Y0 + y), (X0 + 2500, Y0 + y)]))
        names.append(f"{side}-main")
        for k in range(5):
            x = X0 + 100 + 120 * k
            far = 300.0 if y > 0 else -300.0
            lines.append(LineString([(x, Y0 + y), (x, Y0 + far)]))
            names.append(f"{side}-{k}")
    lines.append(LineString([(X0 + 2500, Y0 + 30), (X0 + 2500, Y0 - 30)]))
    names.append("bridge-road")
    streets = _wgs(lines, highway="residential", osm_id=names, surface=None)
    rail = _wgs([LineString([(X0 - 200, Y0), (X0 + 2400, Y0)])], kind=["railway"], osm_id=["r1"])
    homes = _wgs(
        [
            *(gpd.points_from_xy([X0 + 100 + 120 * k + 10 for k in range(5)], [Y0 + 150] * 5)),
            *(gpd.points_from_xy([X0 + 100 + 120 * k + 10 for k in range(5)], [Y0 - 150] * 5)),
        ],
        osm_id=[f"h{i}" for i in range(10)],
        units=[1] * 10,
        kind=["house"] * 10,
    )
    return streets, homes, rail


def _crossings(twin) -> list[tuple[int, int, dict]]:
    return sorted(
        ((u, v, d) for u, v, d in twin.graph.edges(data=True) if d.get("crossing")),
        key=lambda e: (e[0], e[1]),
    )


def test_a_railway_gets_a_priced_crossing_that_beats_going_round(rules):
    streets, homes, rail = _town()
    twin = build_twin(streets, homes, rules, rail)
    found = _crossings(twin)
    assert found and all(d["crossing"] == "railway" for _, _, d in found)
    u, v, d = found[0]
    assert 55 <= d["length_m"] <= rules.crossings.max_length_m + 1e-6
    assert d["cost"] == pytest.approx(rules.crossings.cost("railway", d["length_m"]))
    # From one side to the other the cheapest way is the crossing, not the 5 km road round.
    cost, path = nx.single_source_dijkstra(twin.graph, u, v, weight="cost")
    assert path == [u, v] and cost == pytest.approx(d["cost"])
    assert any("crossing(s) offered under the railway" in x.reason for x in twin.decisions)


def test_crossings_are_spaced_and_identical_on_every_build(rules):
    streets, homes, rail = _town()
    a = _crossings(build_twin(streets, homes, rules, rail))
    b = _crossings(build_twin(streets, homes, rules, rail))
    assert [(u, v) for u, v, _ in a] == [(u, v) for u, v, _ in b]
    xs = sorted(
        (
            build_twin(streets, homes, rules, rail).xy[u][0]
            + build_twin(streets, homes, rules, rail).xy[v][0]
        )
        / 2
        for u, v, _ in a
    )
    assert all(x2 - x1 >= 150 - 1e-6 for x1, x2 in zip(xs, xs[1:], strict=False))


def test_an_unmapped_gap_is_offered_unless_a_building_stands_in_it(rules):
    streets, homes, _rail = _town()
    twin = build_twin(streets, homes, rules)
    assert any(d["crossing"] == "unmapped" for _, _, d in _crossings(twin))
    # Buildings along the whole gap: no bore through them.
    wall = _wgs([box(X0 - 300, Y0 - 20, X0 + 2400, Y0 + 20)], osm_id=["b1"])
    blocked = build_twin(streets, homes, rules, None, wall)
    assert not _crossings(blocked)


def test_the_gent_canal_is_crossed_instead_of_a_long_detour(rules, tmp_path):
    s = open_session(tmp_path / "p", rules, project_id="canal")
    import_area_and_osm(s, GENT / "area-small.geojson", GENT / "gent.osm")
    t = build_twin_tool(s)
    assert any(d.source == "assumption" and "map does not show" in d.reason for d in t.decisions)
    set_pop(s)
    cluster_homes(s, 6.0)
    place_fdh(s, 6.0)
    assign_pon(s)
    feeder = route_feeder(s)
    dist = route_distribution(s)
    route_drops(s)
    place_chambers(s)
    size_cables(s)
    assign_fibers(s)
    crossings = s.design.by_kind("crossing")
    assert crossings, "the cables cross the canal"
    c = crossings[0]
    assert c.element_type == "route" and c.openftth["barrier"] == "unmapped"
    assert c.openftth["detour_m"] > 1000 and c.openftth["detour_cost"] > c.openftth["cost"]
    said = [d.reason for d in feeder.decisions + dist.decisions if "cross under" in d.reason]
    assert said and "going round would be" in said[0]
    assert feeder.data["length_m"] < 2000, "no 2.7 km feeder detour round the canal"
    boq = build_boq(s.design, rules)
    items = {line.item: line for line in boq.lines}
    pits = items["crossing under unmapped obstacle: permit and pits"]
    assert pits.qty == len(crossings) and pits.unit_cost == rules.crossings.unmapped.fixed
