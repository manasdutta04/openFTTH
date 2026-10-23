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
