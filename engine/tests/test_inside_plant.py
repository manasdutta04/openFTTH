"""Buildings with several flats: a riser on every flat's path and the inside plant in the BOQ."""

from __future__ import annotations

from openftth_engine.deliver.boq import build_boq
from openftth_engine.interchange import Design


def test_flats_get_a_riser_on_their_path_and_the_boq_prices_it(gent_run, rules):
    out, _results = gent_run
    design = Design.read(out / "design")
    mdus = [h for h in design.by_kind("home") if int(h.openftth.get("units", 1)) > 1]
    houses = [h for h in design.by_kind("home") if int(h.openftth.get("units", 1)) == 1]
    assert mdus and houses
    for h in mdus:
        assert int(h.openftth["floors"]) >= 2
        assert (
            float(h.openftth["riser_m"]) == int(h.openftth["floors"]) * rules.inside.floor_height_m
        )
        assert float(h.openftth["path_to_pop_m"]) >= float(h.openftth["riser_m"])
    assert all("riser_m" not in h.openftth for h in houses)
    boq = build_boq(design, rules)
    inside = {line.item: line for line in boq.lines if line.category == "inside plant"}
    assert set(inside) == {"riser cable", "floor box", "flat cabling and outlet"}
    assert inside["riser cable"].qty == round(sum(float(h.openftth["riser_m"]) for h in mdus), 2)
    assert inside["flat cabling and outlet"].qty == sum(int(h.openftth["units"]) for h in mdus)
    assert inside["floor box"].qty >= sum(int(h.openftth["floors"]) for h in mdus)


def test_floors_come_from_levels_or_the_flats():
    from openftth_engine.rules.model import InsidePlant

    ins = InsidePlant()
    assert ins.floors(20, 6) == 6
    assert ins.floors(20, None) == 5
    assert ins.floors(3, None) == 2
