from __future__ import annotations

from pathlib import Path

import pytest

from openftth_engine.pipeline import design_all
from openftth_engine.rules import load_rulebook

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "fixtures" / "gent"


@pytest.fixture(scope="session")
def rules():
    return load_rulebook(REPO / "rulebooks" / "belgium.yaml")


@pytest.fixture(scope="session")
def gent_run(tmp_path_factory, rules):
    """Run the whole pipeline once on the Gent fixture; shared by the property tests."""
    out = tmp_path_factory.mktemp("gent") / "project"
    results = design_all(
        FIXTURE / "area.geojson",
        FIXTURE / "gent.osm",
        rules,
        out,
        project_id="gent-test",
        time_limit_s=8.0,
    )
    return out, results
