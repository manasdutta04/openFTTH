"""Tests of the Phase 1 tools: set_rule, size_cables, assign_fibers, splice schedule,
GeoPackage export and scenarios (on the shared Gent run)."""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

import pyogrio

from openftth_engine.interchange import Design
from openftth_engine.pipeline import generate_scenarios, open_session, set_rule
from openftth_engine.rules import load_rulebook
from openftth_engine.solve.fibers import Section, assign_tree_fibers, color_name, tube_fiber


def test_set_rule_validates_and_persists(tmp_path):
    s = open_session(tmp_path / "p", load_rulebook("generic-gpon"), project_id="rules-test")
    r = set_rule(s, "fat.max_homes", "24")
    assert r.data["old"] == 32 and r.data["new"] == 24
    assert s.rules.fat.max_homes == 24
    assert "max_homes: 24" in (tmp_path / "p" / "rulebook.yaml").read_text(encoding="utf-8")
    assert r.version, "set_rule commits a version"
    bad = set_rule(s, "pon.class", "Z")
    assert bad.summary.startswith("rejected") and bad.version is None
    assert s.rules.pon.pon_class == "B+", "a rejected change leaves the rulebook untouched"
    unknown = set_rule(s, "fat.no_such_rule", "1")
    assert unknown.summary.startswith("rejected")
    cascade = set_rule(s, "pon.cascade", "[4, 8]")
    assert cascade.data["new"] == [4, 8] and s.rules.cascade_stages() == [4, 8]


def test_decisions_round_trip_through_the_log(tmp_path):
    s = open_session(tmp_path / "p", load_rulebook("generic-gpon"), project_id="decisions-test")
    set_rule(s, "fat.max_homes", "24")
    logged = s.project.decisions()
    assert logged, "set_rule records a decision"
    assert all(d.tool == "set_rule" for d in logged)
    assert any(d.source == "user" for d in logged)


def test_tree_fibers_are_contiguous_per_subtree():
    #      1 -a-> 2 -b-> 3 (demand 2)
    #        -c-> 4 (demand 3) -d-> 5 (demand 1)
    secs = [Section("a", 1, 2), Section("b", 2, 3), Section("c", 1, 4), Section("d", 4, 5)]
    tf = assign_tree_fibers(1, secs, {3: 2, 4: 3, 5: 1})
    assert tf.total == 6
    assert tf.section_range["a"] == (1, 2) and tf.section_range["b"] == (1, 2)
    assert tf.section_range["c"] == (3, 6) and tf.section_range["d"] == (6, 6)
    assert tf.terminal_range == {3: (1, 2), 4: (3, 5), 5: (6, 6)}
    assert tf.local("c", 6) == 4 and tube_fiber(13) == (2, 1) and color_name(12) == "aqua"


def test_fiber_assignment_invariants(gent_run, rules):
    out, _ = gent_run
    d = Design.read(Path(out) / "design")
    homes = d.by_kind("home")
    units = sum(int(h.openftth["units"]) for h in homes)
    for h in homes:
        if h.openftth.get("mdu"):
            continue
        assert h.openftth["fiber_end"] - h.openftth["fiber_start"] + 1 == h.openftth["units"]
        assert 1 <= h.openftth["splitter_port"] <= rules.pon.split_ratio
    for f in d.by_kind("fat"):
        assert f.openftth["fiber_end"] - f.openftth["fiber_start"] + 1 == f.openftth["units"]
    for c in d.by_kind("distribution"):
        assert c.openftth["fibers_used"] <= c.props["fiber_count"] * c.props["cable_count"]
        assert c.props["tube_count"] * c.props["fibers_per_tube"] >= c.props["fiber_count"]
    rows = d.tables["fiber_connection"]
    by_type = {}
    for r in rows:
        by_type[r["connection_type"]] = by_type.get(r["connection_type"], 0) + 1
    assert by_type["splitter"] == units, "one splitter port per unit"
    pons = sum(int(f.openftth.get("pons", 0)) for f in d.by_kind("fdh"))
    assert by_type["patch"] == pons, "one OLT patch per PON"
    # every fibre entering a distribution section is spliced in exactly once
    entering = {}
    for r in rows:
        if r["dest_cable_uuid"]:
            entering[r["dest_cable_uuid"]] = entering.get(r["dest_cable_uuid"], 0) + 1
    for c in d.by_kind("distribution") + d.by_kind("feeder"):
        assert entering.get(c.element_uuid, 0) == c.openftth["fibers_used"], c.props["name"]
    points = {p["uuid"] for p in d.tables["splice_point"]}
    assert all(r["splice_point_uuid"] in points for r in rows)
    assert all(r["loss_db"] > 0 for r in rows)


def test_splice_schedule_and_gpkg(gent_run):
    out, _ = gent_run
    d = Design.read(Path(out) / "design")
    csv_path = Path(out) / "deliverables" / "splice_schedule.csv"
    with csv_path.open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == len(d.tables["fiber_connection"])
    assert {"closure", "type", "from", "to", "loss_db"} <= set(rows[0])
    gpkg = Path(out) / "deliverables" / "design.gpkg"
    layers = {name for name, _ in pyogrio.list_layers(gpkg)}
    assert {"tb_outdoor", "to_indoor", "cable_underground", "otb_outdoor", "manhole"} <= layers
    fats = pyogrio.read_dataframe(gpkg, layer="tb_outdoor")
    assert len(fats) == len(d.by_type("tb")) - len(d.by_kind("fat")) + len(fats)  # same layer split
    assert (
        "extra_json" in fats.columns
        and json.loads(fats.iloc[0]["extra_json"])["openftth"]["kind"] == "fat"
    )
    with sqlite3.connect(gpkg) as con:
        kinds = dict(con.execute("SELECT table_name, data_type FROM gpkg_contents").fetchall())
        assert (
            kinds["fiber_connection"] == "attributes"
            and kinds["_openftth_metadata"] == "attributes"
        )
        n = con.execute("SELECT count(*) FROM fiber_connection").fetchone()[0]
        assert n == len(d.tables["fiber_connection"])
        fmt = con.execute("SELECT value FROM _openftth_metadata WHERE key='format'").fetchone()[0]
        assert fmt == "openftth-design"


def test_scenarios_price_variants_without_changing_the_design(gent_run, rules):
    out, _ = gent_run
    s = open_session(out, rules)  # fresh session: the twin is rebuilt lazily from the inputs
    before = s.design.copy()
    r = generate_scenarios(s, n=2, time_limit_s=4.0)
    table = r.data["scenarios"]
    assert [t["name"] for t in table] == ["baseline", "larger-fats"]
    assert all(t["boq_total"] > 0 and t["fats"] > 0 and t["fdhs"] > 0 for t in table)
    assert table[1]["overrides"]["fat.max_homes"] > rules.fat.max_homes
    assert before.diff(s.design).is_empty(), "scenarios never touch the design"
    assert (Path(out) / "deliverables" / "scenarios.json").exists()
