"""Rulebook model checks: PON technologies and classes."""

import pytest


def test_xgspon_classes_wavelength_and_coexistence():
    from pydantic import ValidationError

    from openftth_engine.rules import load_rulebook
    from openftth_engine.validate.checks import path_loss_db

    xgs = load_rulebook("generic-xgspon")
    assert (xgs.pon.technology, xgs.pon.pon_class, xgs.pon.worst_case_nm) == ("xgspon", "N1", 1270)
    assert xgs.budget_db() == 29.0
    gpon = load_rulebook("generic-gpon")
    a = path_loss_db(gpon, 5000, 4)
    b = path_loss_db(xgs, 5000, 4)
    assert b["fiber"] > a["fiber"], "1270 nm attenuates more than 1310 nm"
    assert a["coexistence"] == 0 and b["coexistence"] == 0
    both = xgs.model_copy(deep=True)
    both.pon.technology = "gpon+xgspon"
    c = path_loss_db(both, 5000, 4)
    assert round(c["total"] - b["total"], 6) == xgs.loss_db.coexistence
    data = xgs.model_dump(by_alias=True)
    data["pon"]["technology"] = "gpon"
    with pytest.raises(ValidationError, match="XGS-PON class"):
        type(xgs).model_validate(data)
    data = xgs.model_dump(by_alias=True)
    data["pon"]["budget_db"] = 31.0
    with pytest.raises(ValidationError, match="above class N1"):
        type(xgs).model_validate(data)


PRESETS = ("generic-gpon", "generic-xgspon", "belgium", "norway")


@pytest.mark.parametrize("name", PRESETS)
def test_every_preset_value_has_a_source_and_the_preset_is_beta(name):
    from openftth_engine.rules import load_rulebook
    from openftth_engine.rules.sources import unsourced

    rules = load_rulebook(name)
    assert unsourced(rules) == []
    assert rules.review.status == "beta"


def test_the_sources_table_in_the_docs_is_current():
    from pathlib import Path

    from openftth_engine.rules.sources import BEGIN, END, sources_markdown

    doc = (Path(__file__).resolve().parents[2] / "docs" / "RULEBOOK.md").read_text(encoding="utf-8")
    table = doc.split(BEGIN, 1)[1].split(END, 1)[0].strip()
    assert table == sources_markdown().strip(), "run `openftth-engine rules sources --out`"


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (("loss_db", "splitter", 32), 12.0, "ideal split"),
        (("loss_db", "fiber_per_km", 1310), 0.05, "single-mode fibre"),
        (("loss_db", "connector"), 2.0, "less than or equal"),
        (("pon", "split_ratio"), 30, "does not exist"),
        (("pon", "min_loss_db"), 5.0, "class minimum"),
        (("fat", "max_homes"), 48, "more than one PON"),
        (("fat", "min_homes"), 40, "above max_homes"),
        (("fibers", "distribution"), [48, 12, 24], "smallest first"),
        (("fibers", "take_rate"), 1.5, "less than or equal"),
        (("distances", "max_reach_km"), 90, "less than or equal"),
        (("units", "fat"), -1.0, "cannot be negative"),
        (("surfaces", "asphalt", "trench_per_m"), -5.0, "cannot be negative"),
        (("currency",), "euro", "pattern"),
    ],
)
def test_impossible_values_are_refused(path, value, message):
    from pydantic import ValidationError

    from openftth_engine.rules import Rulebook, load_rulebook

    data = load_rulebook("generic-gpon").model_dump(by_alias=True)
    cur = data
    for key in path[:-1]:
        cur = cur[key]
    cur[path[-1]] = value
    with pytest.raises(ValidationError, match=message):
        Rulebook.model_validate(data)


def test_a_reviewed_rulebook_names_who_reviewed_it_and_when():
    from pydantic import ValidationError

    from openftth_engine.rules import Rulebook, load_rulebook

    data = load_rulebook("generic-gpon").model_dump(by_alias=True)
    data["review"]["status"] = "reviewed"
    with pytest.raises(ValidationError, match="reviewed_by"):
        Rulebook.model_validate(data)
    data["review"].update(reviewed_by="A. Engineer, network design", date="2026-10-23")
    assert Rulebook.model_validate(data).review.status == "reviewed"


@pytest.mark.parametrize("name", PRESETS)
def test_a_preset_in_another_currency_prices_every_section_itself(name):
    """The model's default prices are EUR: a preset in NOK (say) that leaves a price section
    out would silently price it in EUR."""
    from pathlib import Path

    import yaml

    path = Path(__file__).resolve().parents[2] / "rulebooks" / f"{name}.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if raw.get("currency", "EUR") == "EUR":
        return
    for section in ("surfaces", "crossings", "units", "labour", "aerial", "facade", "inside"):
        assert section in raw, f"{name} prices in {raw['currency']} but leaves {section} out"
    assert "facade" in raw["surfaces"], "the facade price per metre"
