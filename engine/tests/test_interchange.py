from __future__ import annotations

import json

from shapely.geometry import LineString, Point

from openftth_engine.interchange import Design, Feature, Metadata, stable_uuid


def _design() -> Design:
    d = Design(Metadata(produced_at="2026-01-01T00:00:00Z"))
    d.add(
        Feature(
            element_uuid=stable_uuid("p", "tb", "fat:1"),
            element_type="tb",
            geometry=Point(3.68, 51.07),
            props={"name": "FAT-001"},
            openftth={"kind": "fat", "units": 12},
        )
    )
    d.add(
        Feature(
            element_uuid=stable_uuid("p", "cable.underground", "d:1"),
            element_type="cable.underground",
            geometry=LineString([(3.68, 51.07), (3.681, 51.071)]),
            props={"cable_type": "distribution", "fiber_count": 24},
            openftth={"kind": "distribution"},
        )
    )
    return d


def test_stable_uuid_is_deterministic_and_short():
    a = stable_uuid("proj", "tb", "fat:42")
    assert a == stable_uuid("proj", "tb", "fat:42")
    assert a != stable_uuid("proj2", "tb", "fat:42")
    assert len(a) <= 64


def test_bundle_round_trip_and_canonical_layout(tmp_path):
    d = _design()
    files = d.write(tmp_path)
    names = sorted(p.name for p in files)
    assert names == ["_openftth_metadata.json", "cable.underground.geojson", "tb.outdoor.geojson"]
    text = (tmp_path / "tb.outdoor.geojson").read_text(encoding="utf-8")
    obj = json.loads(text)
    props = obj["features"][0]["properties"]
    assert props["element_type"] == "tb" and props["placement"] == "outdoor"
    assert json.loads(props["extra_json"])["openftth"]["kind"] == "fat"
    # one feature per line, keys sorted
    assert text.count("\n") == 3
    assert text.index('"geometry"') < text.index('"properties"')
    back = Design.read(tmp_path)
    assert back.diff(d).is_empty()
    meta = json.loads((tmp_path / "_openftth_metadata.json").read_text())
    assert meta["format"] == "openftth-design" and meta["format_version"] == "1.0"
    assert meta["crs_epsg"] == "4326"


def test_writing_twice_is_byte_identical(tmp_path):
    d = _design()
    d.write(tmp_path / "a")
    d.write(tmp_path / "b")
    for p in (tmp_path / "a").glob("*.geojson"):
        assert p.read_bytes() == (tmp_path / "b" / p.name).read_bytes()


def test_semantic_diff_by_identity():
    a = _design()
    b = a.copy()
    fat = b.by_kind("fat")[0]
    fat.openftth["units"] = 13
    b.add(
        Feature(
            element_uuid=stable_uuid("p", "to", "h1"),
            element_type="to",
            placement="indoor",
            geometry=Point(3.6, 51.0),
            openftth={"kind": "home"},
        )
    )
    b.remove(a.by_kind("distribution")[0].element_uuid)
    diff = a.diff(b)
    assert diff.summary() == {"added": 1, "removed": 1, "modified": 1}
    assert diff.modified == [fat.element_uuid]


def test_unknown_major_version_is_refused(tmp_path):
    d = _design()
    d.write(tmp_path)
    meta = json.loads((tmp_path / "_openftth_metadata.json").read_text())
    meta["format_version"] = "2.0"
    (tmp_path / "_openftth_metadata.json").write_text(json.dumps(meta))
    try:
        Design.read(tmp_path)
    except ValueError as e:
        assert "major" in str(e)
    else:
        raise AssertionError("expected a ValueError")
