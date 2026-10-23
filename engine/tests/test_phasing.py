"""Take-rate and build phases: connection costs at the take rate, a BOQ per phase."""

from __future__ import annotations

import json

from openftth_engine.deliver.boq import build_boq
from openftth_engine.interchange import Design
from openftth_engine.pipeline import deliver, open_session, plan_phases


def test_take_rate_scales_only_the_connections(gent_run, rules):
    out, _ = gent_run
    design = Design.read(out / "design")
    full = build_boq(design, rules)
    half = rules.model_copy(deep=True)
    half.fibers.take_rate = 0.5
    part = build_boq(design, half)
    item = {line.item: line for line in full.lines}
    item_half = {line.item: line for line in part.lines}
    drop = next(k for k in item if k.startswith("drop cable"))
    assert item_half[drop].qty == round(item[drop].qty * 0.5, 2)
    assert item_half["OTDR test"].qty == round(item["OTDR test"].qty * 0.5, 2)
    assert item_half["FAT install"].qty == item["FAT install"].qty, "the network is built for all"
    assert part.units_served == full.units_served
    assert part.units_connected == round(full.units_served * 0.5)
    assert part.grand_total < full.grand_total


def test_phases_split_the_design_and_its_boq(gent_run, rules, tmp_path):
    import shutil

    src, _ = gent_run
    proj = tmp_path / "p"
    shutil.copytree(src, proj)
    s = open_session(proj, rules, project_id="gent-test")
    r = plan_phases(s, 3, "density")
    assert r.version, r.summary
    phases = {int(f.openftth["phase"]) for f in s.design.by_kind("fdh")}
    assert phases <= {1, 2, 3} and len(phases) >= 2
    for h in s.design.by_kind("home"):
        fat = s.design.get(h.openftth["fat_id"])
        assert h.openftth["phase"] <= fat.openftth["phase"]
    # A feeder section is built no later than every cabinet below it.
    for f in s.design.by_kind("fdh"):
        node = f"N{f.openftth['node']}"
        up = {str(c.props["to_label"]): c for c in s.design.by_kind("feeder")}
        while node in up:
            assert int(up[node].openftth["phase"]) <= int(f.openftth["phase"])
            node = str(up[node].props["from_label"])
    deliver(s)
    summary = json.loads((proj / "deliverables" / "summary.json").read_text(encoding="utf-8"))
    assert [p["phase"] for p in summary["phases"]] == sorted(
        {p["phase"] for p in summary["phases"]}
    )
    assert sum(p["units"] for p in summary["phases"]) == summary["units"]
    assert sum(p["total"] for p in summary["phases"]) <= summary["boq_total"] * 1.0001
    assert (proj / "deliverables" / "boq_phase_1.csv").exists()
