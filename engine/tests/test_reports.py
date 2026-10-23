"""Phase P4: reports. The cable schedule, the port report and the materials by build phase are
read straight from the design and add up to it."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from openftth_engine.pipeline import design_all, open_session, plan_phases, reports

GENT = Path(__file__).resolve().parents[2] / "fixtures" / "gent"


@pytest.fixture(scope="module")
def s(rules, tmp_path_factory):
    out = tmp_path_factory.mktemp("reports") / "p"
    design_all(GENT / "area-hand.geojson", GENT / "gent.osm", rules, out, project_id="reports")
    return open_session(out, rules, project_id="reports")


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def test_the_cable_schedule_lists_every_cable_with_its_ends_and_route(s):
    out = reports(s)
    assert out.version and "cable_schedule.csv" in out.summary
    rows = _rows(s.project.deliver_dir / "cable_schedule.csv")
    cables = s.design.by_kind("feeder") + s.design.by_kind("distribution")
    assert len(rows) == len(cables)
    assert sum(float(r["length_m"]) for r in rows) == pytest.approx(
        sum(float(c.props["length_m"]) for c in cables), abs=0.1 * len(cables)
    )
    for r in rows:
        on_routes = sum(
            float(r[k])
            for k in (
                "trench_asphalt_m",
                "trench_pavement_m",
                "trench_verge_m",
                "existing_duct_m",
                "aerial_m",
                "facade_m",
                "bore_m",
            )
        )
        assert on_routes == pytest.approx(float(r["length_m"]), abs=0.5), r["cable"]
    fdh_names = {f.props["name"] for f in s.design.by_kind("fdh")}
    feeders = [r for r in rows if r["type"] == "feeder"]
    assert feeders and all(r["to"] in fdh_names or r["from"] in fdh_names for r in feeders)


def test_the_port_report_counts_used_and_free_ports(s):
    reports(s)
    rows = _rows(s.project.deliver_dir / "port_report.csv")
    fats = [r for r in rows if r["type"] == "FAT"]
    assert len(fats) == len(s.design.by_kind("fat"))
    for r in fats:
        assert int(r["used"]) + int(r["free"]) == int(r["ports"]), r["site"]
        assert r["cabinet"], "every FAT names its cabinet"
    cabinets = [r for r in rows if r["type"] == "cabinet"]
    assert {r["site"] for r in cabinets} == {f.props["name"] for f in s.design.by_kind("fdh")}
    assert all(r["over_limit"] == "False" for r in cabinets)


def test_materials_by_phase_add_up_to_the_design(s):
    assert not (s.project.deliver_dir / "materials_by_phase.csv").exists()
    plan_phases(s, 2, "density")
    out = reports(s)
    assert "materials_by_phase.csv" in out.summary
    rows = {r["item"]: r for r in _rows(s.project.deliver_dir / "materials_by_phase.csv")}
    fat = rows["FAT"]
    phases = [k for k in fat if k.startswith("phase_")]
    assert phases, "one column per build phase"
    assert sum(float(fat[k]) for k in phases) == float(fat["design_qty"])
    assert all(float(fat[k]) > 0 for k in phases)
