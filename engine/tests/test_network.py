"""Phase N1: lifecycle and provenance, operator pack checks, work orders and fibre trace."""

from __future__ import annotations

import shutil

import pytest

from openftth_engine.network import check_operator, load_operator_pack
from openftth_engine.network.operator import Lifecycle, OperatorPack
from openftth_engine.pipeline import (
    check_operator_tool,
    homes_behind_tool,
    move_site,
    open_session,
    open_work_order,
    post_work_order,
    set_lifecycle,
    trace_fibre,
    work_order_status,
)


@pytest.fixture
def session(gent_run, rules, tmp_path):
    out, _ = gent_run
    dst = tmp_path / "project"
    shutil.copytree(out, dst)
    return open_session(dst, rules)


def test_every_object_has_a_lifecycle_state_and_provenance(gent_run):
    from pathlib import Path

    from openftth_engine.interchange import Design

    out, _ = gent_run
    d = Design.read(Path(out) / "design")
    assert all(f.openftth.get("lifecycle") == "planned" for f in d)
    sources = {f.kind: f.openftth["provenance"]["source"] for f in d}
    assert sources["building"] == "osm" and sources["home"] == "osm"
    assert sources["fat"] == "solver" and sources["feeder"] == "solver"
    fat = d.by_kind("fat")[0]
    assert fat.openftth["provenance"]["created_by"] == "cluster_homes"


def test_the_generic_pack_accepts_a_finished_design(session):
    r = check_operator_tool(session)
    assert r.data["errors"] == 0, r.summary
    assert r.version is None, "checking commits nothing"


def test_operator_rules_catch_bad_ids_missing_attributes_and_dead_homes(session):
    s = session
    pack = load_operator_pack("generic")
    fat = s.design.by_kind("fat")[0]
    fat.props["name"] = "fat one"
    fdh = s.design.by_kind("fdh")[0]
    del fdh.props["port_capacity"]
    s.design.tables["fiber_connection"] = [
        r for r in s.design.tables["fiber_connection"] if r.get("source_side") != "OLT"
    ]
    rules = {v.rule for v in check_operator(s.design, pack)}
    assert {"id_pattern", "required", "home_reaches_olt"} <= rules


def test_a_pack_with_an_unknown_lifecycle_state_is_refused():
    with pytest.raises(ValueError):
        Lifecycle(transitions={"planned": ["built"]})
    with pytest.raises(ValueError):
        OperatorPack.model_validate(
            {"name": "x", "objects": {"fat": {"label": "F", "id_pattern": "("}}}
        )


def test_every_home_traces_to_an_olt_port_with_its_loss(session):
    s = session
    homes = s.design.by_kind("home")
    for h in [homes[0], homes[len(homes) // 2], next(x for x in homes if x.openftth.get("mdu"))]:
        r = trace_fibre(s, h.element_uuid)
        assert r.data["reaches_olt"], r.summary
        steps = r.data["steps"]
        assert steps[0]["kind"] == "pop" and steps[0]["side"] == "OLT"
        kinds = [x["kind"] for x in steps]
        assert "feeder" in kinds and "fdh" in kinds
        assert 0 < r.data["fibre_and_joint_loss_db"] < float(h.openftth["odn_loss_db"])
    fdh = s.design.by_kind("fdh")[0]
    assert trace_fibre(s, fdh.element_uuid).summary.startswith("rejected")


def test_homes_behind_a_cabinet_are_the_homes_it_serves(session):
    s = session
    fdh = s.design.by_kind("fdh")[0]
    r = homes_behind_tool(s, fdh.element_uuid)
    served = {
        h.element_uuid
        for h in s.design.by_kind("home")
        if h.openftth.get("fdh_id") == fdh.element_uuid
    }
    assert set(r.data["home_ids"]) == served
    feeder_all = homes_behind_tool(s, s.design.by_kind("pop")[0].element_uuid)
    assert feeder_all.data["homes"] == len(s.design.by_kind("home"))


def test_lifecycle_moves_only_along_the_pack_transitions(session):
    s = session
    bad = set_lifecycle(s, "in_service", "too early", kinds=["fat"])
    assert bad.summary.startswith("rejected") and "may go to" in bad.summary
    ok = set_lifecycle(s, "in_construction", "civil works started", kinds=["fat", "fdh"])
    assert ok.version and ok.data["changed"] == len(s.design.by_kind("fat")) + len(
        s.design.by_kind("fdh")
    )
    assert all(f.openftth["lifecycle"] == "in_construction" for f in s.design.by_kind("fat"))
    assert all(f.openftth["lifecycle"] == "planned" for f in s.design.by_kind("home"))
    assert any(d.source == "user" for d in ok.decisions)


def test_a_work_order_posts_to_the_network_of_record_and_tracks_changes(session):
    s = session
    assert post_work_order(s).summary.startswith("rejected: no work order")
    wo = open_work_order(s, "Gent first build")
    assert wo.data["id"] == "WO-0001" and wo.data["base"] is None
    assert open_work_order(s, "second").summary.startswith("rejected")

    status = work_order_status(s)
    assert status.data["network_of_record"] is None
    assert status.data["totals"]["added"] > 0

    posted = post_work_order(s)
    assert not posted.summary.startswith("rejected"), posted.summary
    assert s.project.record() == posted.version
    assert work_order_status(s).data["totals"] == {"added": 0, "removed": 0, "modified": 0}

    open_work_order(s, "move a cabinet")
    assert post_work_order(s).summary.startswith("rejected: WO-0002 changes nothing")
    fdh = s.design.by_kind("fdh")[0]
    r = move_site(s, fdh.element_uuid, fdh.geometry.x + 0.0004, fdh.geometry.y, "public side")
    assert not r.summary.startswith("rejected"), r.summary
    changes = work_order_status(s).data["changes_by_kind"]
    assert changes["fdh"]["modified"] >= 1
    second = post_work_order(s)
    assert s.project.record() == second.version
    assert [w["state"] for w in s.project.meta["work_orders"]] == ["posted", "posted"]


def test_posting_is_refused_while_the_operator_pack_reports_errors(session):
    s = session
    open_work_order(s, "broken")
    s.design.by_kind("fat")[0].props["name"] = "not a FAT id"
    r = post_work_order(s)
    assert r.summary.startswith("rejected") and "operator" in r.summary
    assert s.project.record() is None


def test_objects_made_before_provenance_are_labelled_by_kind_not_by_the_next_tool(session):
    s = session
    for f in s.design:
        f.openftth.pop("lifecycle", None)
        f.openftth.pop("provenance", None)
    open_work_order(s, "legacy design")
    fat = s.design.by_kind("fat")[0]
    home = s.design.by_kind("home")[0]
    assert fat.openftth["provenance"] == {"source": "solver", "created_by": "unrecorded"}
    assert home.openftth["provenance"]["source"] == "osm"
    assert fat.openftth["lifecycle"] == "planned"
