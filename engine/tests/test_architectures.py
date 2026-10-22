"""Phase P2: centralised, cascaded and point-to-point designs of the same area."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from openftth_engine.deliver.boq import build_boq
from openftth_engine.network import trace_home
from openftth_engine.pipeline import (
    assign_fibers,
    assign_pon,
    build_twin_tool,
    cluster_homes,
    generate_scenarios,
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
from openftth_engine.rules import Rulebook

GENT = Path(__file__).resolve().parents[2] / "fixtures" / "gent"


def _with(rules: Rulebook, **pon) -> Rulebook:
    data = rules.model_dump(by_alias=True)
    data["pon"].update(pon)
    return Rulebook.model_validate(data)


def test_the_rulebook_says_which_architecture_and_checks_it(rules):
    assert rules.pon.arch() == "centralised"
    assert _with(rules, cascade=[4, 8]).pon.arch() == "cascaded", "a cascade implies it"
    p2p = _with(rules, architecture="p2p")
    assert p2p.splitter_loss() == 0.0 and p2p.budget_db() == p2p.pon.p2p_budget_db
    with pytest.raises(ValidationError, match="two `cascade` stages"):
        _with(rules, architecture="cascaded")
    with pytest.raises(ValidationError, match="no `cascade`"):
        _with(rules, architecture="centralised", cascade=[4, 8])
    assert _with(rules, cascade=[4, 8]).fat_splitters(17) == 3


def _design(rules: Rulebook, root: Path, pid: str):
    s = open_session(root / pid, rules, project_id=pid)
    import_area_and_osm(s, GENT / "area-small.geojson", GENT / "gent.osm")
    build_twin_tool(s)
    set_pop(s)
    cluster_homes(s, 6.0)
    place_fdh(s, 6.0)
    pon = assign_pon(s)
    route_feeder(s)
    route_distribution(s)
    route_drops(s)
    place_chambers(s)
    size_cables(s)
    assign_fibers(s)
    return s, pon, validate_design(s)


@pytest.fixture(scope="module")
def cascaded(rules, tmp_path_factory):
    return _design(_with(rules, cascade=[4, 8]), tmp_path_factory.mktemp("cascaded"), "cascaded")


@pytest.fixture(scope="module")
def p2p(rules, tmp_path_factory):
    return _design(_with(rules, architecture="p2p"), tmp_path_factory.mktemp("p2p"), "p2p")


def test_a_cascade_splits_in_the_fdh_and_again_in_each_fat(cascaded):
    s, pon, check = cascaded
    r = s.rules
    assert "1:4 in the FDH × 1:8 in the FAT" in pon.summary
    assert "no violations" in check.summary, check.summary
    fats = s.design.by_kind("fat")
    for f in fats:
        assert f.openftth["splitters"] == -(-int(f.openftth["units"]) // 8)
    # A PON is one first-stage splitter: at most 4 FAT splitters under it.
    per_pon: dict[str, int] = {}
    for f in fats:
        per_pon[f.openftth["pon_id"]] = (
            per_pon.get(f.openftth["pon_id"], 0) + f.openftth["splitters"]
        )
    assert max(per_pon.values()) <= 4
    # The distribution cable carries one fibre per FAT splitter, not one per home.
    splitters = sum(f.openftth["splitters"] for f in fats)
    units = sum(int(f.openftth["units"]) for f in fats)
    reaching = sum(int(f.openftth["fiber_end"]) - int(f.openftth["fiber_start"]) + 1 for f in fats)
    assert reaching == splitters < units
    boq = {line.item: line for line in build_boq(s.design, r).lines}
    assert boq["splitter 1:8 (FAT)"].qty == splitters
    assert boq["splitter 1:4 (FDH)"].qty == len(per_pon)
    home = sorted(s.design.by_kind("home"), key=lambda h: h.element_uuid)[0]
    out = trace_home(s.design, r, home.element_uuid)
    assert out["reaches_olt"], out
    hops = [st["via"] for st in out["steps"] if st.get("via")]
    splits = [h for h in hops if h["connection"] == "splitter"]
    assert sorted(st["loss_db"] for st in splits) == sorted(
        [r.loss_db.splitter[4], r.loss_db.splitter[8]]
    )


def test_point_to_point_gives_every_unit_its_own_fibre(p2p):
    s, pon, check = p2p
    r = s.rules
    assert pon.summary.startswith("point-to-point")
    assert "no violations" in check.summary and f"of {r.budget_db()} dB" in check.summary
    units = sum(int(h.openftth["units"]) for h in s.design.by_kind("home"))
    assert sum(int(f.openftth["pons"]) for f in s.design.by_kind("fdh")) == units
    assert not any(h.openftth.get("pon_id") for h in s.design.by_kind("home"))
    conns = s.design.tables["fiber_connection"]
    assert not any(c["connection_type"] == "splitter" for c in conns)
    boq = {line.item: line for line in build_boq(s.design, r).lines}
    assert boq["ODF ports"].qty == units
    assert not any(item.startswith("splitter") for item in boq)
    home = sorted(s.design.by_kind("home"), key=lambda h: h.element_uuid)[-1]
    out = trace_home(s.design, r, home.element_uuid)
    assert out["reaches_olt"], out


def test_the_architectures_are_priced_side_by_side(p2p):
    s, *_ = p2p
    out = generate_scenarios(
        s, names=["centralised", "cascaded", "point-to-point"], time_limit_s=4.0
    )
    rows = {row["name"]: row for row in out.data["scenarios"]}
    assert set(rows) == {"centralised", "cascaded", "point-to-point"}
    assert all(row["boq_total"] > 0 and not row["violations"] for row in rows.values())
    assert generate_scenarios(s, names=["nope"]).summary.startswith("rejected")
