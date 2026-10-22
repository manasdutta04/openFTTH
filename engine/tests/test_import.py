"""Phase P1: designs from data a designer brings, not only OpenStreetMap.

The data is made from the Gent fixture in the shapes real projects arrive in: an address register
as CSV in Belgian Lambert 72 (no CRS in the file), streets as a Shapefile in the same projection,
and a GeoPackage carrying existing ducts, poles and a no-dig zone.
"""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import box

from openftth_engine.ingest import homes_from_buildings, import_osm, load_area
from openftth_engine.ingest.inputs import load_inputs
from openftth_engine.ingest.profiles import Binding, ImportProfile, propose_profile
from openftth_engine.ingest.sources import list_layers, read_layer
from openftth_engine.pipeline import (
    assign_fibers,
    assign_pon,
    build_twin_tool,
    check_inputs_tool,
    cluster_homes,
    import_data,
    open_session,
    place_chambers,
    place_fdh,
    route_distribution,
    route_drops,
    route_feeder,
    set_pop,
    size_cables,
    validate_design,
)

REPO = Path(__file__).resolve().parents[2]
GENT = REPO / "fixtures" / "gent"
LAMBERT = "EPSG:31370"


@pytest.fixture(scope="module")
def sources(tmp_path_factory, rules):
    """Address CSV + street Shapefile in Lambert 72, and a GeoPackage of existing assets."""
    d = tmp_path_factory.mktemp("sources")
    area = load_area(GENT / "area-small.geojson")
    osm = import_osm(GENT / "gent.osm", area)
    homes, _ = homes_from_buildings(osm.buildings, rules)
    lam = homes.to_crs(LAMBERT)
    pd.DataFrame(
        {
            "adres_id": [f"A{i:05d}" for i in range(len(lam))],
            "straatnaam": ["Teststraat"] * len(lam),
            "huisnummer": [str(i + 1) for i in range(len(lam))],
            "x": [f"{p.x:.2f}" for p in lam.geometry],
            "y": [f"{p.y:.2f}" for p in lam.geometry],
            "wooneenheden": [int(u) for u in lam["units"]],
        }
    ).to_csv(d / "adressen.csv", sep=";", index=False)
    streets = osm.streets.rename(columns={"highway": "wegtype"})[["wegtype", "osm_id", "geometry"]]
    streets.to_crs(LAMBERT).to_file(d / "wegen.shp")
    gpd.GeoDataFrame({"naam": ["projectgebied"]}, geometry=[area], crs=4326).to_file(
        d / "gebied.geojson"
    )
    # Existing assets: ducts along the five longest streets (2 m beside them), poles on their
    # vertices, and a no-dig zone over the middle of the area.
    lines = osm.streets.to_crs(LAMBERT)
    inside = gpd.GeoSeries([area], crs=4326).to_crs(LAMBERT).iloc[0]
    lines = lines[(lines.geometry.length > 0) & lines.within(inside.buffer(50))]
    longest = lines.assign(len=lines.geometry.length).nlargest(5, "len")
    ducts = longest.copy()
    ducts["geometry"] = longest.geometry.offset_curve(2.0)
    gpd.GeoDataFrame(
        {"duct_id": [f"D{i}" for i in range(len(ducts))], "eigenaar": "operator"},
        geometry=ducts.geometry.values,
        crs=LAMBERT,
    ).to_file(d / "bestaand.gpkg", layer="kokers", driver="GPKG")
    poles = longest.geometry.interpolate(0.5, normalized=True)
    gpd.GeoDataFrame(
        {"pole_id": [f"P{i}" for i in range(len(poles))]}, geometry=poles.values, crs=LAMBERT
    ).to_file(d / "bestaand.gpkg", layer="palen", driver="GPKG")
    minx, miny, maxx, maxy = gpd.GeoSeries([area], crs=4326).to_crs(LAMBERT).total_bounds
    cx, cy = (minx + maxx) / 2, (miny + maxy) / 2
    gpd.GeoDataFrame(
        {"reden": ["nieuwe asfaltlaag"]},
        geometry=[box(cx - 60, cy - 60, cx + 60, cy + 60)],
        crs=LAMBERT,
    ).to_file(d / "bestaand.gpkg", layer="nodig_zones", driver="GPKG")
    return d, len(lam), int(lam["units"].sum())


def test_layers_are_listed_with_their_columns_and_crs(sources):
    d, n, _ = sources
    csv = list_layers(d / "adressen.csv")[0]
    assert csv.geometry == "Point" and csv.count == n and "wooneenheden" in csv.columns
    shp = list_layers(d / "wegen.shp")[0]
    assert shp.crs == LAMBERT and "LineString" in shp.geometry
    gpkg = {i.layer for i in list_layers(d / "bestaand.gpkg")}
    assert gpkg == {"kokers", "palen", "nodig_zones"}


def test_a_projected_table_without_a_crs_needs_one(sources):
    d, _, _ = sources
    with pytest.raises(ValueError, match="give the CRS"):
        read_layer(d / "adressen.csv")
    pts = read_layer(d / "adressen.csv", crs=LAMBERT)
    assert pts.crs.to_epsg() == 4326 and 3.6 < pts.geometry.x.mean() < 3.8


def test_the_proposed_profile_recognises_every_layer(sources):
    d, _, _ = sources
    profile, _ = propose_profile(
        [d / "gebied.geojson", d / "adressen.csv", d / "wegen.shp", d / "bestaand.gpkg"]
    )
    roles = {(Path(b.path).name, b.layer): b.role for b in profile.bindings}
    assert roles[("gebied.geojson", "gebied")] == "area"
    assert roles[("adressen.csv", None)] == "addresses"
    assert roles[("wegen.shp", "wegen")] == "streets"
    assert roles[("bestaand.gpkg", "kokers")] == "existing_ducts"
    assert roles[("bestaand.gpkg", "palen")] == "poles"
    assert roles[("bestaand.gpkg", "nodig_zones")] == "no_dig_zones"
    addr = next(b for b in profile.bindings if b.role == "addresses")
    assert addr.columns["units"] == "wooneenheden" and addr.columns["id"] == "adres_id"
    assert (addr.x, addr.y) == ("x", "y")
    assert next(b for b in profile.bindings if b.role == "streets").columns["class"] == "wegtype"


def _profile(d: Path, with_assets: bool) -> ImportProfile:
    b = [
        Binding(path=str(d / "gebied.geojson"), role="area"),
        Binding(
            path=str(d / "adressen.csv"),
            role="addresses",
            crs=LAMBERT,
            x="x",
            y="y",
            columns={
                "id": "adres_id",
                "units": "wooneenheden",
                "street": "straatnaam",
                "housenumber": "huisnummer",
            },
        ),
        Binding(
            path=str(d / "wegen.shp"), role="streets", columns={"id": "osm_id", "class": "wegtype"}
        ),
    ]
    if with_assets:
        b += [
            Binding(
                path=str(d / "bestaand.gpkg"),
                layer="kokers",
                role="existing_ducts",
                columns={"id": "duct_id", "owner": "eigenaar"},
            ),
            Binding(
                path=str(d / "bestaand.gpkg"),
                layer="palen",
                role="poles",
                columns={"id": "pole_id"},
            ),
            Binding(
                path=str(d / "bestaand.gpkg"),
                layer="nodig_zones",
                role="no_dig_zones",
                columns={"reason": "reden"},
            ),
        ]
    return ImportProfile(name="lambert", bindings=b)


def _design(s, time_limit: float = 6.0) -> None:
    build_twin_tool(s)
    set_pop(s)
    cluster_homes(s, time_limit)
    place_fdh(s, time_limit)
    assign_pon(s)
    route_feeder(s)
    route_distribution(s)
    route_drops(s)
    place_chambers(s)
    size_cables(s)
    assign_fibers(s)


def test_the_address_register_drives_demand_without_any_osm(sources, rules, tmp_path):
    d, n, units = sources
    s = open_session(tmp_path / "p", rules, project_id="lambert")
    r = import_data(s, _profile(d, with_assets=False))
    assert r.data["homes"] == n and r.data["units"] == units, r.summary
    assert "address register" in r.summary
    assert any(dec.source == "user" and "address register" in dec.reason for dec in r.decisions)
    assert (tmp_path / "p" / "import.yaml").exists()
    q = check_inputs_tool(s)
    assert q.data["level"] in ("ok", "warning", "info") and q.data["counts"]["addresses"] == n
    _design(s)
    v = validate_design(s)
    homes = s.design.by_kind("home")
    assert len(homes) == n and sum(int(h.openftth["units"]) for h in homes) == units
    assert all(h.openftth.get("fat_id") for h in homes)
    assert "power_budget" not in v.data["summary"], v.summary


def test_existing_ducts_are_reused_and_no_dig_zones_avoided(sources, rules, tmp_path):
    d, _, _ = sources
    s = open_session(tmp_path / "p", rules, project_id="assets")
    r = import_data(s, _profile(d, with_assets=True))
    counts = r.data["counts"]
    assert counts["existing_ducts"] >= 5, counts
    # Poles beyond the margin around the area are left out.
    assert 0 < counts["poles"] <= 5
    assert len(s.design.by_kind("existing_duct")) == counts["existing_ducts"]
    assert len(s.design.by_kind("existing_pole")) == counts["poles"]
    assert all(f.openftth["lifecycle"] == "in_service" for f in s.design.by_kind("existing_duct"))
    _design(s)
    twin = s.twin
    surfaces = {
        k: v
        for c in s.design.by_kind("feeder") + s.design.by_kind("distribution")
        for k, v in c.openftth["surfaces"].items()
    }
    assert "existing_duct" in surfaces, "routes reuse the existing ducts"
    zone = gpd.read_file(d / "bestaand.gpkg", layer="nodig_zones").to_crs(twin.crs).geometry.iloc[0]
    for u, v, data in twin.graph.edges(data=True):
        mid = ((twin.xy[u][0] + twin.xy[v][0]) / 2, (twin.xy[u][1] + twin.xy[v][1]) / 2)
        if data["highway"] not in ("existing_duct",):
            from shapely.geometry import Point

            assert not zone.contains(Point(mid)), "no trench inside the no-dig zone"
    # A fresh session rebuilds exactly the same twin from the saved profile.
    s2 = open_session(tmp_path / "p", rules)
    from openftth_engine.pipeline import _twin

    t2 = _twin(s2)
    assert t2.graph.number_of_edges() == twin.graph.number_of_edges()


def test_inputs_load_identically_twice(sources, rules):
    d, _, _ = sources
    a = load_inputs(_profile(d, with_assets=True), rules)
    b = load_inputs(_profile(d, with_assets=True), rules)
    assert list(a.homes["osm_id"]) == list(b.homes["osm_id"])
    assert list(a.streets["osm_id"]) == list(b.streets["osm_id"])


def test_the_street_margin_widens_until_the_network_joins_up():
    from openftth_engine.ingest.osm import STREET_BUFFER_M, STREET_PIECES_MAX, off_main_share

    # The small Gent area is split by a motorway; its streets only join up well beyond it.
    area = load_area(GENT / "area-small.geojson")
    osm = import_osm(GENT / "gent.osm", area)
    assert osm.street_margin_m > STREET_BUFFER_M
    assert off_main_share(osm.streets) <= STREET_PIECES_MAX
    # An area whose streets join up inside the usual margin keeps it.
    whole = import_osm(GENT / "gent.osm", load_area(GENT / "area.geojson"))
    assert whole.street_margin_m == STREET_BUFFER_M
