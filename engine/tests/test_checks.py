from __future__ import annotations

import pytest
from shapely.geometry import LineString, Point

from openftth_engine.interchange import Design, Feature
from openftth_engine.rules import load_rulebook
from openftth_engine.validate.checks import check_distances, path_loss_db, violation_summary


def test_power_budget_hand_computed_b_plus():
    rb = load_rulebook("generic-gpon")
    # 3 km at 1310 nm (0.35 dB/km) = 1.05; 6 connectors × 0.3 = 1.8; 4 splices × 0.1 = 0.4;
    # 1:32 splitter 17.1; margin 2.0 → 22.35 dB, under the 28 dB class B+ budget.
    loss = path_loss_db(rb, 3000.0, 4)
    assert loss["fiber"] == pytest.approx(1.05)
    assert loss["connectors"] == pytest.approx(1.8)
    assert loss["splices"] == pytest.approx(0.4)
    assert loss["splitter"] == pytest.approx(17.1)
    assert loss["total"] == pytest.approx(22.35)
    assert loss["total"] < rb.pon.budget_db


def test_power_budget_fails_when_too_far():
    rb = load_rulebook("generic-gpon")
    # 20 km at 1310 nm → 7 dB fibre + 1.8 + 0.4 + 17.1 + 2.0 = 28.3 > 28
    assert path_loss_db(rb, 20000.0, 4)["total"] > rb.pon.budget_db


def test_cascaded_splitter_loss_norway():
    rb = load_rulebook("norway")
    assert rb.splitter_loss() == pytest.approx(7.2 + 10.5)
    assert rb.pon.budget_db == 32.0


def _drop_design(offset_m: float, length_m: float) -> Design:
    d = Design()
    d.add(
        Feature(
            element_uuid="home-1",
            element_type="to",
            placement="indoor",
            geometry=Point(0.0, 0.0),
            props={"name": "home 1"},
            openftth={"kind": "home", "units": 1, "offset_m": offset_m},
        )
    )
    d.add(
        Feature(
            element_uuid="drop-1",
            element_type="cable.underground",
            placement="outdoor",
            geometry=LineString([(0.0, 0.0), (0.001, 0.0)]),
            props={"length_m": length_m, "cable_type": "drop"},
            openftth={"kind": "drop", "home": "home-1"},
        )
    )
    return d


def test_a_long_drop_a_closer_fat_could_fix_is_an_error():
    rb = load_rulebook("generic-gpon")
    v = check_distances(_drop_design(offset_m=20.0, length_m=rb.distances.max_drop_m + 40.0), rb)
    assert [x.severity for x in v] == ["error"]
    assert violation_summary(v) == {"distance": 1}
    assert violation_summary(v, "warning") == {}


def test_a_home_farther_from_any_street_than_the_rule_is_a_warning_not_an_error():
    rb = load_rulebook("generic-gpon")
    far = rb.distances.max_drop_m + 50.0
    v = check_distances(_drop_design(offset_m=far, length_m=far + 10.0), rb)
    assert [x.severity for x in v] == ["warning"]
    assert "no FAT placement" in v[0].message
    assert violation_summary(v) == {}
    assert violation_summary(v, "warning") == {"distance": 1}
