"""Phase P2: aerial spans on existing poles and cable along building facades, offered per street
edge where the data allows them, priced against the trench, and compared as scenarios.

The synthetic street runs 400 m east; its western half is lined by a terrace of houses, its
eastern half has a pole every 40 m.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import geopandas as gpd
from shapely.geometry import LineString, Point, box

from openftth_engine.deliver.boq import build_boq
from openftth_engine.pipeline import (
    apply_scenario,
    generate_scenarios,
    import_area_and_osm,
    open_session,
)
from openftth_engine.solve.scenarios import apply_overrides
from openftth_engine.twin import build_twin

GENT = Path(__file__).resolve().parents[2] / "fixtures" / "gent"
LAMBERT = "EPSG:31370"
X0, Y0 = 104000.0, 194000.0


def _wgs(geoms, **cols) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(cols, geometry=geoms, crs=LAMBERT).to_crs(4326)


def _street():
    streets = _wgs(
        [LineString([(X0, Y0), (X0 + 400, Y0)])],
        highway=["residential"],
        osm_id=["w1"],
        surface=None,
    )
    terrace = [box(X0 + x, Y0 + 4, X0 + x + 10, Y0 + 12) for x in range(0, 200, 10)]
    buildings = _wgs(terrace, osm_id=[f"b{i}" for i in range(len(terrace))])
    homes = _wgs(
        [b.centroid for b in terrace],
        osm_id=[f"h{i}" for i in range(len(terrace))],
        units=[1] * len(terrace),
        kind=["house"] * len(terrace),
    )
    poles = _wgs([Point(X0 + x, Y0 - 3) for x in range(200, 401, 40)], owner=["grid"] * 6)
    return streets, homes, buildings, poles


def _by_surface(twin) -> dict[str, list[float]]:
    out: dict[str, list[float]] = {}
    for u, v, d in twin.graph.edges(data=True):
        out.setdefault(d["surface"], []).append((twin.xy[u][0] + twin.xy[v][0]) / 2.0)
    return out


def test_poles_give_aerial_spans_and_walls_give_facade_routes(rules):
    streets, homes, buildings, poles = _street()
    default = build_twin(streets, homes, rules, None, buildings, poles)
    found = _by_surface(default)
    assert "facade" not in found, "facade cabling is off unless the rulebook allows it"
    assert found.get("aerial") and found.get("pavement")
    # The aerial edges are the eastern half (the one with poles), the trench the western.
    assert min(found["aerial"]) > max(found["pavement"])
    for _u, _v, d in default.graph.edges(data=True):
        if d["surface"] == "aerial":
            assert d["cost"] < d["trench_cost"]
    assert any("strung on the poles" in x.reason for x in default.decisions)

    both = apply_overrides(rules, {"facade.enabled": True})
    twin = build_twin(streets, homes, both, None, buildings, poles)
    found = _by_surface(twin)
    assert found.get("facade") and found.get("aerial")
    assert max(found["facade"]) < min(found["aerial"])
    assert "pavement" not in found, "the whole street is on walls or poles"
    assert any("lined by building walls" in x.reason for x in twin.decisions)

    none = apply_overrides(rules, {"aerial.enabled": False, "facade.enabled": False})
    assert set(_by_surface(build_twin(streets, homes, none, None, buildings, poles))) == {
        "pavement"
    }


def test_a_facade_needs_walls_along_most_of_the_stretch(rules):
    streets, homes, _buildings, _poles = _street()
    # One house in 40 m: not enough wall to carry the cable.
    sparse = _wgs([box(X0 + x, Y0 + 4, X0 + x + 8, Y0 + 12) for x in range(0, 400, 40)])
    allowed = apply_overrides(rules, {"facade.enabled": True})
    assert "facade" not in _by_surface(build_twin(streets, homes, allowed, None, sparse))


def test_a_no_dig_zone_keeps_the_spans_a_keep_out_zone_does_not(rules):
    streets, homes, buildings, poles = _street()
    allowed = apply_overrides(rules, {"facade.enabled": True})
    whole = box(X0 - 50, Y0 - 50, X0 + 450, Y0 + 50)
    for kind, kept in (("no_dig", True), ("keep_out", False)):
        twin = build_twin(streets, homes, allowed, None, buildings, poles)
        before = twin.graph.number_of_edges()
        twin.forbid_zones(_wgs([whole], kind=[kind]))
        assert (twin.graph.number_of_edges() == before) is kept, kind


def test_the_terraced_street_scenarios_price_trench_facade_and_poles(rules, tmp_path):
    """The P2 gate: one area, four route types, priced side by side."""
    s = open_session(tmp_path / "p", rules, project_id="routes")
    import_area_and_osm(s, GENT / "area-small.geojson", GENT / "gent.osm")
    # Poles along every other street of the sample (the OSM extract has none).
    streets = s.cache["streets"]
    utm = streets.estimate_utm_crs()
    pts = []
    for k, line in enumerate(streets.to_crs(utm).geometry):
        if k % 2 or line is None or line.length < 40:
            continue
        pts += [line.interpolate(d) for d in range(0, int(line.length) + 1, 40)]
    poles = gpd.GeoDataFrame({"owner": ["grid"] * len(pts)}, geometry=pts, crs=utm).to_crs(4326)
    s.cache["inputs"] = dataclasses.replace(s.cache["inputs"], poles=poles)
    s.twin = None
    from openftth_engine.pipeline import build_twin_tool

    build_twin_tool(s)
    out = generate_scenarios(
        s, time_limit_s=3.0, names=["trench-only", "facade", "aerial", "aerial-and-facade"]
    )
    table = {t["name"]: t for t in out.data["scenarios"]}
    assert table["trench-only"]["facade_m"] == table["trench-only"]["aerial_m"] == 0
    assert table["facade"]["facade_m"] > 0 and table["facade"]["aerial_m"] == 0
    assert table["aerial"]["aerial_m"] > 0 and table["aerial"]["facade_m"] == 0
    for name in ("facade", "aerial", "aerial-and-facade"):
        assert table[name]["trench_m"] < table["trench-only"]["trench_m"], name
        assert table[name]["boq_total"] < table["trench-only"]["boq_total"], name
    # Both together are never dearer than the dearer of the two alone (the solvers may land a
    # little either side of the cheaper one).
    assert table["aerial-and-facade"]["boq_total"] <= max(
        table["facade"]["boq_total"], table["aerial"]["boq_total"]
    )
    # Adopting one re-prices the street graph and re-runs the solvers: the BOQ then carries the
    # spans, the pole attachments and the transitions into the ground.
    applied = apply_scenario(s, "aerial-and-facade", time_limit_s=3.0)
    assert applied.version
    items = {line.item: line for line in build_boq(s.design, s.rules).lines}
    assert items["aerial span on existing poles"].qty > 0
    assert items["facade cabling"].qty > 0
    assert items["pole attachment"].qty >= items["aerial span on existing poles"].qty / 50
    assert items["pole to ground transition"].qty > 0
