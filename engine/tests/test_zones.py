"""Keep-out and no-dig zones: drawn by the engineer, respected by the solvers and the checks."""

from __future__ import annotations

from pathlib import Path

import pytest

from openftth_engine import edit
from openftth_engine.pipeline import design_all
from openftth_engine.validate import run_all_checks

GENT = Path(__file__).resolve().parents[2] / "fixtures" / "gent"


@pytest.fixture(scope="module")
def s(rules, tmp_path_factory):
    from openftth_engine.pipeline import open_session

    out = tmp_path_factory.mktemp("zones") / "p"
    design_all(GENT / "area-hand.geojson", GENT / "gent.osm", rules, out, project_id="zones")
    return open_session(out, rules, project_id="zones")


def _square(lon: float, lat: float, half_deg: float = 0.0003) -> list[list[float]]:
    return [
        [lon - half_deg, lat - half_deg],
        [lon + half_deg, lat - half_deg],
        [lon + half_deg, lat + half_deg],
        [lon - half_deg, lat + half_deg],
    ]


def test_a_keep_out_zone_moves_equipment_out_and_undo_brings_it_back(s):
    fat = max(s.design.by_kind("fat"), key=lambda f: int(f.openftth.get("units", 0)))
    p = fat.geometry
    homes_before = len(s.design.by_kind("home"))
    out = edit.add_zone(s, "keep_out", _square(p.x, p.y), "park")
    assert out.version, out.summary
    zone = s.design.get(out.data["element"])
    for kind in ("fat", "fdh", "pop"):
        for f in s.design.by_kind(kind):
            if f.placement != "indoor":
                assert not zone.geometry.contains(f.geometry), f"{kind} still inside the zone"
    assert not [v for v in run_all_checks(s.design, s.rules)[0] if v.check == "zone"]
    assert len(s.design.by_kind("home")) == homes_before, "homes inside are still served"
    assert all(h.openftth.get("fat_id") for h in s.design.by_kind("home"))
    inside = edit.add_site(s, "fat", p.x, p.y, "try")
    assert inside.summary.startswith("rejected") and "keep-out" in inside.summary
    assert edit.undo_edit(s).version
    assert not s.design.by_kind("zone") or out.data["element"] not in s.design
    assert edit.redo_edit(s).version and out.data["element"] in s.design
    gone = edit.delete_element(s, out.data["element"], "park reopened")
    assert gone.version and out.data["element"] not in s.design


def test_a_zone_needs_an_area_and_a_known_kind(s):
    assert edit.add_zone(s, "keep_out", [[3.68, 51.07], [3.681, 51.07]], "x").summary.startswith(
        "rejected"
    )
    assert edit.add_zone(s, "lava", _square(3.686, 51.07), "x").summary.startswith("rejected")
