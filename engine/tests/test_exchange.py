"""Phase P4: export profiles. Every shipped profile writes the design in its schema, and reading
the files back through the same profile gives every mapped value and geometry back."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from openftth_engine.deliver.exchange import (
    ExportProfile,
    expected_values,
    list_export_profiles,
    load_export_profile,
    read_with_profile,
)
from openftth_engine.pipeline import design_all, export_profile, open_session

GENT = Path(__file__).resolve().parents[2] / "fixtures" / "gent"


@pytest.fixture(scope="module")
def s(rules, tmp_path_factory):
    out = tmp_path_factory.mktemp("exchange") / "p"
    design_all(GENT / "area-hand.geojson", GENT / "gent.osm", rules, out, project_id="exchange")
    return open_session(out, rules, project_id="exchange")


def test_profiles_ship_for_each_format():
    names = list_export_profiles()
    assert {"generic-gis", "generic-shapefile", "generic-csv"} <= set(names)
    assert {load_export_profile(n).driver for n in names} >= {"gpkg", "shapefile", "csv"}


@pytest.mark.parametrize("name", ["generic-gis", "generic-shapefile", "generic-csv"])
def test_an_export_round_trips_through_its_profile(s, name):
    out = export_profile(s, name)
    assert out.version, out.summary
    profile = load_export_profile(name)
    folder = s.project.deliver_dir / "exports" / name
    assert (folder / "export.json").exists()
    back = read_with_profile(profile, folder)
    want = expected_values(s.design, profile)
    assert set(back) == set(want), "every element comes back, nothing extra"
    for uid, w in want.items():
        got = back[uid]
        assert got["kind"] == w["kind"]
        for source, value in w["values"].items():
            if isinstance(value, float):
                assert got["values"][source] == pytest.approx(value, abs=1e-6), (uid, source)
            else:
                assert got["values"][source] == value, (uid, source)
        # Within a millimetre after the trip through the profile's CRS.
        assert got["geometry"].equals_exact(w["geometry"], tolerance=1e-8), uid


def test_the_shapefile_profile_codes_the_status(s):
    export_profile(s, "generic-shapefile")
    import geopandas as gpd

    fat = gpd.read_file(s.project.deliver_dir / "exports" / "generic-shapefile" / "FAT.shp")
    assert set(fat["STATUS"]) == {"PLN"}
    assert fat.crs.to_epsg() == 32631, "the design's own UTM zone"


def test_a_bad_profile_is_refused(s, tmp_path):
    with pytest.raises(ValidationError, match="10 characters"):
        ExportProfile.model_validate(
            {
                "name": "x",
                "driver": "shapefile",
                "layers": [
                    {"kind": "fat", "layer": "F", "fields": {"IDENTIFIER_X": {"from": "uuid"}}}
                ],
            }
        )
    with pytest.raises(ValidationError, match="read back"):
        ExportProfile.model_validate(
            {
                "name": "x",
                "driver": "csv",
                "layers": [{"kind": "fat", "layer": "F", "fields": {"n": {"from": "props.name"}}}],
            }
        )
    assert export_profile(s, "no-such-profile").summary.startswith("rejected")
