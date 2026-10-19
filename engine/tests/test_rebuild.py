"""Rebuilding the twin after the solve must not undo the design (found on the Manchester
field project: the Surveyor re-imported after the solve and every home lost its FAT, PON and
drop; the next route_drops would have produced nothing)."""

from __future__ import annotations

import shutil

from openftth_engine.pipeline import (
    build_twin_tool,
    import_area_and_osm,
    open_session,
    route_drops,
)


def test_rebuilding_the_twin_keeps_the_solvers_work_on_the_homes(gent_run, rules, tmp_path):
    out, _ = gent_run
    dst = tmp_path / "project"
    shutil.copytree(out, dst)
    s = open_session(dst, rules)
    inputs = s.project.meta["inputs"]
    before = {h.element_uuid: dict(h.openftth) for h in s.design.by_kind("home")}
    assert all("fat_id" in o and "path_to_pop_m" in o for o in before.values())
    drops = len(s.design.by_kind("drop"))

    import_area_and_osm(s, inputs["area"], inputs["osm"])
    build_twin_tool(s)

    after = {h.element_uuid: h.openftth for h in s.design.by_kind("home")}
    assert after.keys() == before.keys()
    for uid, o in before.items():
        assert after[uid]["fat_id"] == o["fat_id"]
        assert after[uid]["path_to_pop_m"] == o["path_to_pop_m"]
        assert after[uid]["node"] == o["node"], "the street snap is the same for the same inputs"
    r = route_drops(s)
    assert len(s.design.by_kind("drop")) == drops, r.summary
    assert not any("lost their FAT link" in w for w in r.warnings)


def test_route_drops_recovers_links_a_rebuilt_home_lost(gent_run, rules, tmp_path):
    out, _ = gent_run
    dst = tmp_path / "project"
    shutil.copytree(out, dst)
    s = open_session(dst, rules)
    drops = len(s.design.by_kind("drop"))
    # A project written before the fix: homes without any solver keys.
    for h in s.design.by_kind("home"):
        for key in ("fat_id", "fat_ids", "fdh_id", "pon_id", "drop_m", "path_to_pop_m", "mdu"):
            h.openftth.pop(key, None)

    r = route_drops(s)

    assert len(s.design.by_kind("drop")) == drops
    assert any("lost their FAT link" in w for w in r.warnings)
    assert all("fat_id" in h.openftth for h in s.design.by_kind("home"))
