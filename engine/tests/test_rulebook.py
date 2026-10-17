from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from openftth_engine.rules import Rulebook, list_presets, load_rulebook
from tests.conftest import REPO


def test_all_presets_load_and_validate():
    names = list_presets()
    assert {"generic-gpon", "belgium", "norway"} <= set(names)
    for n in names:
        rb = load_rulebook(n)
        assert rb.name == n


def test_schema_file_matches_model():
    """rulebooks/schema.json is generated; it must be in sync with the pydantic model."""
    generated = Rulebook.model_json_schema(by_alias=True)
    on_disk = json.loads((REPO / "rulebooks" / "schema.json").read_text(encoding="utf-8"))
    assert on_disk == generated


def test_cascade_must_multiply_to_split_ratio():
    with pytest.raises(ValidationError):
        Rulebook.model_validate({"name": "x", "pon": {"split_ratio": 32, "cascade": [4, 4]}})
    rb = Rulebook.model_validate({"name": "x", "pon": {"split_ratio": 32, "cascade": [4, 8]}})
    assert rb.cascade_stages() == [4, 8]
    assert rb.splitter_loss() == pytest.approx(7.2 + 10.5)


def test_unknown_keys_are_rejected():
    with pytest.raises(ValidationError):
        Rulebook.model_validate({"name": "x", "typo": 1})


def test_ladder_size_with_spare():
    rb = Rulebook.model_validate(
        {"name": "x", "fibers": {"spare_pct": 20, "distribution": [12, 24, 48]}}
    )
    assert rb.ladder_size(rb.fibers.distribution, 10) == 12  # 10 + 2 spare = 12
    assert rb.ladder_size(rb.fibers.distribution, 11) == 24  # 11 + 3 = 14 > 12
    assert rb.ladder_size(rb.fibers.distribution, 100) == 48  # over the ladder → largest
    assert rb.ladder_cables(rb.fibers.distribution, 100) == (3, 48)  # 120 fibres in 3 × 48F
    assert rb.ladder_cables(rb.fibers.distribution, 10) == (1, 12)


def test_generic_b_plus_budget_values():
    rb = load_rulebook("generic-gpon")
    assert rb.pon.pon_class == "B+"
    assert rb.pon.budget_db == 28.0
    assert rb.connectors_on_path() == 6
