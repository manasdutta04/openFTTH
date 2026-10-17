"""Property tests of the solver on the Gent fixture (one pipeline run, session-scoped)."""

from __future__ import annotations

import json
from pathlib import Path

import networkx as nx

from openftth_engine.interchange import Design
from openftth_engine.pipeline import design_all
from openftth_engine.project import Project
from tests.conftest import FIXTURE


def _design(gent_run) -> Design:
    out, _ = gent_run
    return Design.read(Path(out) / "design")


def test_every_home_is_assigned_exactly_once(gent_run):
    d = _design(gent_run)
    homes = d.by_kind("home")
    fats = {f.element_uuid for f in d.by_kind("fat")}
    fdhs = {f.element_uuid for f in d.by_kind("fdh")}
    assert homes
    for h in homes:
        assert h.openftth["fat_id"] in fats
        assert h.openftth["fdh_id"] in fdhs
        assert h.openftth["pon_id"]
    drops = d.by_kind("drop")
    street_homes = [h for h in homes if not h.openftth.get("mdu")]
    assert len(drops) == len(street_homes)
    assert len({x.openftth["home"] for x in drops}) == len(street_homes)


def test_capacities_never_exceeded(gent_run, rules):
    d = _design(gent_run)
    homes = d.by_kind("home")
    fats = d.by_kind("fat")
    # Units are conserved: homes → FATs → FDHs.
    assert sum(int(h.openftth["units"]) for h in homes) == sum(
        int(f.openftth["units"]) for f in fats
    )
    per_fat: dict[str, int] = {}
    for h in homes:
        if h.openftth.get("mdu"):
            continue
        per_fat[h.openftth["fat_id"]] = per_fat.get(h.openftth["fat_id"], 0) + int(
            h.openftth["units"]
        )
    for f in fats:
        assert int(f.openftth["units"]) <= rules.fat.max_homes
        if not f.openftth.get("mdu"):
            assert per_fat[f.element_uuid] == int(f.openftth["units"])
    per_pon: dict[str, int] = {}
    per_fdh: dict[str, int] = {}
    for f in fats:
        per_pon[f.openftth["pon_id"]] = per_pon.get(f.openftth["pon_id"], 0) + int(
            f.openftth["units"]
        )
        per_fdh[f.openftth["fdh_id"]] = per_fdh.get(f.openftth["fdh_id"], 0) + int(
            f.openftth["units"]
        )
    assert max(per_pon.values()) <= rules.pon.split_ratio
    for fdh in d.by_kind("fdh"):
        assert per_fdh.get(fdh.element_uuid, 0) == fdh.openftth["units"]
        assert fdh.openftth["units"] <= fdh.openftth["capacity"] * rules.fdh.max_fill_pct / 100


def test_cable_trees_are_rooted_trees(gent_run):
    d = _design(gent_run)
    for kind in ("feeder", "distribution"):
        cables = d.by_kind(kind)
        assert cables, f"no {kind} cables"
        by_root: dict[str | None, nx.Graph] = {}
        for c in cables:
            g = by_root.setdefault(c.openftth.get("fdh_id"), nx.Graph())
            g.add_edge(c.props["from_label"], c.props["to_label"])
        for root, g in by_root.items():
            assert nx.is_tree(g), (
                kind,
                root,
                g.number_of_nodes(),
                g.number_of_edges(),
                nx.number_connected_components(g),
                sorted(g.edges)[:12],
            )
        for c in cables:
            assert c.props["fiber_count"] * c.props["cable_count"] >= c.openftth["downstream_units"]


def test_drop_lengths_respect_the_rule_or_are_reported(gent_run, rules):
    out, results = gent_run
    d = _design(gent_run)
    over = [x for x in d.by_kind("drop") if x.props["length_m"] > rules.distances.max_drop_m]
    validation = json.loads((Path(out) / "deliverables" / "validation.json").read_text())
    reported = [
        v
        for v in validation["violations"]
        if v["check"] == "distance" and v["element"] in {x.element_uuid for x in over}
    ]
    assert len(reported) == len(over)
    # The vast majority must comply.
    assert len(over) <= 0.05 * len(d.by_kind("drop"))


def test_power_budget_within_class(gent_run, rules):
    out, _ = gent_run
    validation = json.loads((Path(out) / "deliverables" / "validation.json").read_text())
    pb = validation["power_budget"]
    assert pb["worst_db"] <= rules.pon.budget_db
    assert not [v for v in validation["violations"] if v["check"] == "power_budget"]


def test_boq_and_versions_exist(gent_run):
    out, results = gent_run
    boq = json.loads((Path(out) / "deliverables" / "boq.json").read_text())
    assert boq["grand_total"] > 0 and boq["units_served"] > 0
    assert set(boq["totals"]) >= {"civil", "cables", "passives", "labour"}
    assert (Path(out) / "deliverables" / "map.png").stat().st_size > 10_000
    log = Project(out, create=False).log()
    assert len(log) >= len(results) - 1  # deliver may not change the bundle
    assert all(r.version for r in results[:-1])


def test_determinism_same_inputs_same_bundle(gent_run, rules, tmp_path):
    out, _ = gent_run
    again = tmp_path / "again"
    design_all(
        FIXTURE / "area.geojson",
        FIXTURE / "gent.osm",
        rules,
        again,
        project_id="gent-test",
        time_limit_s=8.0,
    )
    for p in sorted((Path(out) / "design").glob("*.geojson")):
        assert p.read_bytes() == (again / "design" / p.name).read_bytes(), p.name
