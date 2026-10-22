"""Phase P2: several POPs, each with its own catchment of homes, cabinets, feeder and fibres."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from openftth_engine.network import trace_home
from openftth_engine.pipeline import (
    _pops,
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
    validate_design,
)
from openftth_engine.solve.pops import split_catchments

GENT = Path(__file__).resolve().parents[2] / "fixtures" / "gent"


def test_catchments_respect_capacity_and_are_the_same_every_time():
    rng = np.random.default_rng(7)
    xy = np.vstack([rng.normal((0, 0), 150, (60, 2)), rng.normal((2000, 0), 150, (40, 2))])
    units = np.ones(len(xy), dtype=int)
    a = split_catchments(xy, units, 2, capacity=60)
    b = split_catchments(xy, units, 2, capacity=60)
    assert a.owner == b.owner and np.allclose(a.centres, b.centres)
    assert sorted(a.units) == [40, 60]
    # Each town keeps to its own POP.
    assert (
        len({a.owner[i] for i in range(60)}) == 1 and len({a.owner[i] for i in range(60, 100)}) == 1
    )
    # Too little capacity: shared evenly rather than infeasible.
    tight = split_catchments(xy, units, 2, capacity=10)
    assert sorted(tight.units) == [50, 50]
    # The engineer's sites: only the assignment is solved.
    fixed = split_catchments(
        xy, units, 0, capacity=100, fixed=np.array([[2000.0, 0.0], [0.0, 0.0]])
    )
    assert fixed.units == [40, 60]


def _design(s):
    cluster_homes(s, 6.0)
    place_fdh(s, 6.0)
    assign_pon(s)
    feeder = route_feeder(s)
    route_distribution(s)
    route_drops(s)
    place_chambers(s)
    size_cables(s)
    assign_fibers(s)
    return feeder, validate_design(s)


@pytest.fixture(scope="module")
def three(rules, tmp_path_factory):
    small = rules.model_copy(update={"pops": rules.pops.model_copy(update={"max_units": 600})})
    s = open_session(tmp_path_factory.mktemp("pops") / "p", small, project_id="three")
    import_area_and_osm(s, GENT / "area.geojson", GENT / "gent.osm")
    build_twin_tool(s)
    placed = set_pop(s)
    feeder, check = _design(s)
    return s, placed, feeder, check


def test_an_area_beyond_one_pop_gets_several_with_their_own_catchments(three):
    s, placed, feeder, check = three
    pops = _pops(s)
    assert len(pops) == 3 and placed.data["pops"] == 3
    total = sum(int(h.openftth["units"]) for h in s.design.by_kind("home"))
    assert all(int(p.openftth["units"]) <= max(600, -(-total // 3)) for p in pops)
    assert any("more than one POP serves" in d.reason for d in placed.decisions)
    ids = {p.element_uuid for p in pops}
    homes = s.design.by_kind("home")
    assert all(h.openftth.get("pop_id") in ids for h in homes)
    fdh_pop = {f.element_uuid: f.openftth["pop_id"] for f in s.design.by_kind("fdh")}
    for fat in s.design.by_kind("fat"):
        assert fdh_pop[fat.openftth["fdh_id"]] == fat.openftth["pop_id"], "no cabinet straddles"
    for cable in s.design.by_kind("feeder"):
        assert cable.openftth["pop_id"] in ids
    assert feeder.summary.count("POP-") == 3
    assert "no violations" in check.summary, check.summary


def test_every_home_traces_to_its_own_pop(three, rules):
    s, *_ = three
    pops = {p.element_uuid: p for p in _pops(s)}
    conns = s.design.tables.get("fiber_connection", [])
    assert len({c["uuid"] for c in conns}) == len(conns), "fibre connection ids are unique"
    by_pop: dict[str, str] = {}
    for h in sorted(s.design.by_kind("home"), key=lambda h: h.element_uuid):
        by_pop.setdefault(h.openftth["pop_id"], h.element_uuid)
    assert len(by_pop) == 3
    for pid, home in by_pop.items():
        out = trace_home(s.design, rules, home)
        assert out["reaches_olt"], out
        olt = out["steps"][0]
        assert olt["element"] == pid and olt["name"] == pops[pid].props["name"]


def test_one_pop_stays_as_it_was(rules, tmp_path):
    s = open_session(tmp_path / "p", rules, project_id="one")
    import_area_and_osm(s, GENT / "area.geojson", GENT / "gent.osm")
    build_twin_tool(s)
    placed = set_pop(s)
    assert placed.summary.startswith("POP at street node")
    (pop,) = _pops(s)
    assert pop.element_uuid == s.uid("odf", "pop") and pop.props["name"] == "POP"
    _feeder, check = _design(s)
    assert not any("pop_id" in h.openftth for h in s.design.by_kind("home"))
    assert all(c.props["name"].startswith("FDR-") for c in s.design.by_kind("feeder"))
    assert "no violations" in check.summary, check.summary
