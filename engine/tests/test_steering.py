"""Tests of the engineer's own moves: move_site, exclude_building and forbid_street.

Every steer is one version, records a `source="user"` decision with the reason, re-runs the
solvers whose output exists, and is reproducible. The sessions here are opened over a copy of
the finished Gent run, so the twin is rebuilt lazily from the recorded inputs, exactly as it
is after a harness restart.
"""

from __future__ import annotations

import json
import shutil

import pytest

from openftth_engine.interchange import Design
from openftth_engine.pipeline import (
    RERUN_CHAIN,
    _twin,
    exclude_building,
    forbid_street,
    move_site,
    open_session,
)


@pytest.fixture
def session(gent_run, rules, tmp_path):
    out, _ = gent_run
    dst = tmp_path / "project"
    shutil.copytree(out, dst)
    return open_session(dst, rules)


def _sited(design: Design):
    return (
        design.by_kind("pop")
        + design.by_kind("fdh")
        + design.by_kind("fat")
        + design.by_kind("home")
    )


def test_move_site_snaps_reroutes_and_records_one_version(session):
    s = session
    fdh = s.design.by_kind("fdh")[0]
    old_node = int(fdh.openftth["node"])
    head = s.project.head()
    n_versions = len(s.project.log(limit=500))

    r = move_site(
        s,
        fdh.element_uuid,
        fdh.geometry.x + 0.002,
        fdh.geometry.y,
        "the cabinet must stand on the public side of the fence",
    )

    assert not r.summary.startswith("rejected"), r.summary
    assert r.version and r.version != head
    assert len(s.project.log(limit=500)) == n_versions + 1, (
        "the move and every re-run are one version"
    )
    moved = s.design.get(fdh.element_uuid)
    assert int(moved.openftth["node"]) != old_node
    assert moved.openftth["placed_by"] == "engineer"
    user = [d for d in r.decisions if d.source == "user"]
    assert len(user) == 1 and user[0].element == fdh.element_uuid and "fence" in user[0].reason
    assert r.data["reran"] == list(RERUN_CHAIN), "a finished design re-runs the whole routing chain"
    assert r.data["moved_m"] > 50
    # The site sits on its new node and the routes start from it.
    twin = _twin(s)
    assert moved.geometry.distance(gpd_point(twin, int(moved.openftth["node"]))) < 1e-6
    assert all("feeder_path_m" in f.openftth for f in s.design.by_kind("fdh"))
    assert all(int(f.openftth["node"]) in twin.graph for f in _sited(s.design))
    # What the session holds is what is on disk.
    on_disk = Design.read(s.project.bundle_dir)
    assert on_disk.get(fdh.element_uuid).openftth["node"] == moved.openftth["node"]


def gpd_point(twin, node):
    import geopandas as gpd

    return gpd.GeoSeries([twin.node_point(node)], crs=twin.crs).to_crs(4326).iloc[0]


def test_move_site_rejects_what_an_engineer_cannot_move(session):
    s = session
    fdh = s.design.by_kind("fdh")[0]
    home = s.design.by_kind("home")[0]
    head = s.project.head()
    assert move_site(s, home.element_uuid, 3.7, 51.0, "x").summary.startswith("rejected: a home")
    assert move_site(s, fdh.element_uuid, 3.7, 51.0, "  ").summary.startswith("rejected: a reason")
    assert move_site(s, "no-such-id", 3.7, 51.0, "x").summary.startswith("rejected: no element")
    mdu = next((f for f in s.design.by_kind("fat") if f.openftth.get("mdu")), None)
    if mdu is not None:
        assert "indoor" in move_site(s, mdu.element_uuid, 3.7, 51.0, "x").summary
    assert s.project.head() == head, "a rejection commits nothing"


def test_a_move_is_deterministic(gent_run, rules, tmp_path):
    out, _ = gent_run
    bundles = []
    for i in range(2):
        dst = tmp_path / f"p{i}"
        shutil.copytree(out, dst)
        s = open_session(dst, rules)
        fdh = s.design.by_kind("fdh")[0]
        move_site(s, fdh.element_uuid, fdh.geometry.x + 0.002, fdh.geometry.y, "same move")
        bundles.append(
            {p.name: p.read_bytes() for p in sorted(s.project.bundle_dir.glob("*.geojson"))}
        )
    assert bundles[0] == bundles[1]


def test_exclude_building_removes_its_homes_and_survives_a_twin_rebuild(session, rules):
    s = session
    home = next(h for h in s.design.by_kind("home") if not h.openftth.get("mdu"))
    units = int(home.openftth["units"])
    b_id = home.openftth["building"]
    fat_id = home.openftth["fat_id"]
    fat_units = int(s.design.get(fat_id).openftth["units"])
    fdh_id = s.design.get(fat_id).openftth["fdh_id"]
    fdh_units = int(s.design.get(fdh_id).openftth["units"])
    homes_before = len(s.design.by_kind("home"))
    n_versions = len(s.project.log(limit=500))

    r = exclude_building(s, b_id, "a garage, not a dwelling")

    assert not r.summary.startswith("rejected"), r.summary
    assert len(s.project.log(limit=500)) == n_versions + 1
    assert home.element_uuid not in s.design
    assert not any(d.openftth.get("home") == home.element_uuid for d in s.design.by_kind("drop"))
    assert s.design.get(b_id).openftth["excluded"] is True
    assert s.design.get(b_id).openftth["excluded_reason"] == "a garage, not a dwelling"
    if fat_id in s.design:
        assert int(s.design.get(fat_id).openftth["units"]) == fat_units - units
    assert int(s.design.get(fdh_id).openftth["units"]) == fdh_units - units
    assert r.data["reran"] == list(RERUN_CHAIN)
    assert exclude_building(s, b_id, "again").summary.startswith("rejected")
    assert exclude_building(s, home.openftth["fat_id"], "x").summary.startswith("rejected")

    # A new session rebuilds the twin from the recorded inputs and still leaves it out.
    s2 = open_session(s.project.dir, rules)
    twin = _twin(s2)
    osm_id = str(s.design.get(b_id).openftth["osm_id"])
    assert osm_id not in set(twin.homes["osm_id"].astype(str))
    assert len(s2.design.by_kind("home")) == homes_before - 1
    assert s2.project.meta["inputs"]["excluded_buildings"][0]["osm_id"] == osm_id


def test_forbid_street_removes_the_way_and_reroutes_around_it(session):
    s = session
    twin = _twin(s)
    # The way under the longest distribution section, so the re-route has to change something.
    longest = max(s.design.by_kind("distribution"), key=lambda c: float(c.props["length_m"]))
    u, v = map(int, next(iter(longest.openftth["edges"])).split("-"))
    way = str(twin.graph[u][v]["osm_id"])
    edges_before = twin.graph.number_of_edges()
    n_versions = len(s.project.log(limit=500))

    r = forbid_street(s, way, "fresh asphalt, no digging before 2028")

    assert not r.summary.startswith("rejected"), r.summary
    assert len(s.project.log(limit=500)) == n_versions + 1
    assert r.data["edges"] > 0
    assert twin.graph.number_of_edges() <= edges_before - r.data["edges"]
    assert not any(str(d.get("osm_id")) == way for _, _, d in twin.graph.edges(data=True))
    # No cable runs along the forbidden way any more, and every site stands on the graph.
    for c in s.design.by_kind("feeder") + s.design.by_kind("distribution"):
        for key in c.openftth["edges"]:
            a, b = map(int, key.split("-"))
            assert twin.graph.has_edge(a, b)
    assert all(int(f.openftth["node"]) in twin.graph for f in _sited(s.design))
    assert r.data["reran"] == list(RERUN_CHAIN)
    # The street layer flags the way and the inputs remember it.
    streets = json.loads((s.project.twin_dir / "streets.geojson").read_text(encoding="utf-8"))
    flagged = [f for f in streets["features"] if f["properties"].get("no_dig")]
    assert flagged and all(str(f["properties"]["osm_id"]) == way for f in flagged)
    assert s.project.meta["inputs"]["no_dig"] == [
        {"osm_id": way, "reason": "fresh asphalt, no digging before 2028"}
    ]
    assert forbid_street(s, way, "again").summary.startswith("rejected")
    assert forbid_street(s, "w0", "x").summary.startswith("rejected: no street")


def test_a_fresh_session_rebuilds_the_twin_without_the_forbidden_way(session, rules):
    s = session
    twin = _twin(s)
    way = str(next(iter(twin.graph.edges(data=True)))[2]["osm_id"])
    forbid_street(s, way, "private road")
    s2 = open_session(s.project.dir, rules)
    twin2 = _twin(s2)
    assert not any(str(d.get("osm_id")) == way for _, _, d in twin2.graph.edges(data=True))
    assert twin2.graph.number_of_edges() == twin.graph.number_of_edges()
    assert bool(twin2.streets["no_dig"].any())
