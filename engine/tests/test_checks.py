from __future__ import annotations

import pytest

from openftth_engine.rules import load_rulebook
from openftth_engine.validate.checks import path_loss_db


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
