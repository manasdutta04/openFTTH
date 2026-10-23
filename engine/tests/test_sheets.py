"""Phase P4: plan sheets. A printable PDF atlas and a DXF drawing of the design, identical on
every run, and a DXF that GIS software (GDAL, which QGIS uses) reads back layer by layer."""

from __future__ import annotations

import re
from pathlib import Path

import geopandas as gpd
import pytest

from openftth_engine.deliver.dxf import DxfDrawing
from openftth_engine.pipeline import design_all, open_session, plan_sheets

GENT = Path(__file__).resolve().parents[2] / "fixtures" / "gent"


@pytest.fixture(scope="module")
def s(rules, tmp_path_factory):
    out = tmp_path_factory.mktemp("sheets") / "p"
    design_all(GENT / "area-hand.geojson", GENT / "gent.osm", rules, out, project_id="sheets")
    return open_session(out, rules, project_id="sheets")


def test_the_plan_set_has_a_key_plan_and_sheets_and_is_identical_every_run(s):
    first = plan_sheets(s, 1000, "A3")
    assert first.data["sheets"] >= 2, first.summary
    d = s.project.deliver_dir
    pdf, dxf = (d / "plans.pdf").read_bytes(), (d / "plans.dxf").read_bytes()
    assert pdf.startswith(b"%PDF")
    pages = len(re.findall(rb"/Type\s*/Page[^s]", pdf))
    assert pages == first.data["sheets"]
    assert "sheet(s) on A3 at 1:1,000" in first.decisions[0].reason
    again = plan_sheets(s, 1000, "A3")
    assert again.data == first.data
    assert (d / "plans.pdf").read_bytes() == pdf, "the atlas changed between two runs"
    assert (d / "plans.dxf").read_bytes() == dxf, "the drawing changed between two runs"


def test_a1_at_a_smaller_scale_needs_fewer_sheets(s):
    a3 = plan_sheets(s, 1000, "A3").data["sheets"]
    a1 = plan_sheets(s, 2000, "A1").data["sheets"]
    assert a1 <= a3


def test_gis_software_reads_the_drawing_layer_by_layer(s):
    plan_sheets(s, 1000, "A3")
    dxf = s.project.deliver_dir / "plans.dxf"
    text = dxf.read_text(encoding="cp1252")
    assert "AC1009" in text and text.rstrip().endswith("EOF")
    gdf = gpd.read_file(dxf)
    layers = set(gdf["Layer"])
    for layer in ("STREETS", "BUILDINGS", "FEEDER", "DISTRIBUTION", "DROP", "FAT", "FDH", "POP"):
        assert layer in layers, layer
    feeder = gdf[gdf["Layer"] == "FEEDER"]
    assert not feeder.empty and (feeder.geometry.length > 0).all()
    # Metres in the twin's projection: the feeder is as long in the drawing as in the design.
    design_m = sum(float(c.props.get("length_m", 0)) for c in s.design.by_kind("feeder"))
    assert feeder.geometry.length.sum() == pytest.approx(design_m, rel=0.02)
    labels = gdf[gdf["Layer"] == "LABELS"]
    assert set(labels["Text"]) >= {f.props["name"] for f in s.design.by_kind("fdh")}


def test_bad_paper_or_scale_is_refused(s):
    assert plan_sheets(s, 1000, "A0").summary.startswith("rejected")
    assert plan_sheets(s, 10, "A3").summary.startswith("rejected")


def test_the_dxf_writer_keeps_r12_names_and_text(tmp_path):
    dwg = DxfDrawing()
    dwg.layer("fat drop/x", 3)
    dwg.polyline("fat drop/x", [(0.0, 0.0), (10.0, 0.0)])
    dwg.circle("fat drop/x", 5.0, 5.0, 1.0)
    dwg.text("fat drop/x", 1.0, 1.0, 2.0, "Straße  12\nnext")
    path = dwg.write(tmp_path / "t.dxf")
    text = path.read_text(encoding="cp1252")
    assert "FAT_DROP_X" in text and "Straße 12 next" in text
    assert text.count("POLYLINE") == 1 and text.count("SEQEND") == 1
    gdf = gpd.read_file(path)
    assert set(gdf["Layer"]) == {"FAT_DROP_X"}
