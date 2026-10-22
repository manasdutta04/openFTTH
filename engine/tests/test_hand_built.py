"""The P3 gate in the engine: a small network built entirely from the engineer's edits (sites,
assignments, drawn cables), completed, validated and posted as a work order."""

from __future__ import annotations

from pathlib import Path

import pytest

from openftth_engine import edit
from openftth_engine.network import check_operator
from openftth_engine.network.trace import trace_home
from openftth_engine.pipeline import (
    _pack,
    build_twin_tool,
    import_area_and_osm,
    open_session,
    open_work_order,
    post_work_order,
)
from openftth_engine.validate import run_all_checks

GENT = Path(__file__).resolve().parents[2] / "fixtures" / "gent"


@pytest.fixture(scope="module")
def s(rules, tmp_path_factory):
    s = open_session(tmp_path_factory.mktemp("hand") / "p", rules, project_id="hand")
    import_area_and_osm(s, GENT / "area-hand.geojson", GENT / "gent.osm")
    build_twin_tool(s)
    return s


def _centre(features) -> tuple[float, float]:
    xs = [f.geometry.representative_point().x for f in features]
    ys = [f.geometry.representative_point().y for f in features]
    return sum(xs) / len(xs), sum(ys) / len(ys)


def _ok(out):
    assert out.version, out.summary
    return out


def _errors(s) -> list[str]:
    rules = [v.message for v in run_all_checks(s.design, s.rules)[0] if v.severity == "error"]
    ops = [v.message for v in check_operator(s.design, _pack(s)) if v.severity == "error"]
    return rules + ops


def _draw_to(s, kind: str, a: str, b: str, why: str) -> set[str]:
    """Draw a cable by hand from site a to site b; the street edges it was drawn along."""
    fa, fb = s.design.get(a), s.design.get(b)
    if fa.openftth["node"] == fb.openftth["node"]:
        return set()
    pts = [[fa.geometry.x, fa.geometry.y], [fb.geometry.x, fb.geometry.y]]
    out = _ok(edit.draw_cable(s, kind, pts, why))
    return set(s.design.get(out.data["element"]).openftth["edges"])


def _street_groups(s, homes, reach_m: float, size: int) -> list[tuple[int, list]]:
    """How an engineer groups homes by eye: start at the street node with the most homes and
    take the homes within `reach_m` along the street, up to `size`; repeat with the rest."""
    import networkx as nx

    graph = edit._twin(s).graph
    left = {h.element_uuid: h for h in homes}
    groups = []
    while left:
        at: dict[int, list] = {}
        for h in left.values():
            at.setdefault(int(h.openftth["node"]), []).append(h)
        seed = max(sorted(at), key=lambda n: len(at[n]))
        near = nx.single_source_dijkstra_path_length(graph, seed, cutoff=reach_m, weight="length_m")
        pick = sorted(
            (
                h
                for n in sorted(near, key=near.get)
                for h in sorted(at.get(n, []), key=lambda h: h.element_uuid)
            ),
            key=lambda h: near[int(h.openftth["node"])],
        )[:size]
        for h in pick:
            del left[h.element_uuid]
        groups.append((seed, pick))
    return groups


def _node_lonlat(s, node: int) -> tuple[float, float]:
    twin = edit._twin(s)
    p = edit._wgs(twin, twin.node_point(node))
    return p.x, p.y


def _assign(s, to: str, ids: list[str], why: str) -> None:
    """Reassign what is not on `to` yet (an engineer leaves the rest alone)."""
    key = f"{s.design.get(to).kind}_id"
    todo = [u for u in ids if s.design.get(u).openftth.get(key) != to]
    if todo:
        _ok(edit.reassign(s, None, to, why, element_uuids=todo))


def _fat_at(s, lon: float, lat: float, why: str) -> str:
    """A FAT at the street node nearest the point: the one standing there, or a new one."""
    node, _ = edit._snap(s, lon, lat)
    there = [f for f in s.design.by_kind("fat") if int(f.openftth["node"]) == node]
    if there:
        return there[0].element_uuid
    return _ok(edit.add_site(s, "fat", lon, lat, why)).data["element"]


def test_a_network_built_by_hand_is_completed_validated_and_posted(s):
    homes = sorted(s.design.by_kind("home"), key=lambda h: h.geometry.representative_point().x)
    assert 10 <= len(homes) <= 120, len(homes)
    why = "hand-built layout"
    # Sites, each placed by hand: POP, cabinet, and a first guess of two FATs.
    pop = _ok(edit.add_site(s, "pop", *_centre(homes), why)).data["element"]
    fdh_at = _centre(homes[len(homes) // 3 : 2 * len(homes) // 3])
    fdh = _ok(edit.add_site(s, "fdh", *fdh_at, why)).data["element"]
    halves = [homes[: len(homes) // 2], homes[len(homes) // 2 :]]
    first = [_ok(edit.add_site(s, "fat", *_centre(h), why)).data["element"] for h in halves]
    # Assignments, a whole selection at a time.
    out = _ok(
        edit.reassign(s, None, first[0], why, element_uuids=[h.element_uuid for h in halves[0]])
    )
    assert out.summary.startswith(f"{len(halves[0])} homes moved")
    _ok(edit.reassign(s, None, first[1], why, element_uuids=[h.element_uuid for h in halves[1]]))
    _ok(edit.reassign(s, None, fdh, why, element_uuids=first))
    # Cables drawn by hand: POP to cabinet, cabinet to each FAT.
    drawn = _draw_to(s, "feeder", pop, fdh, why)
    for f in first:
        drawn |= _draw_to(s, "distribution", fdh, f, why)
    done = _ok(edit.complete_network(s, "finish the hand layout"))
    assert done.data["unserved"] == 0
    # The routes are the engineer's: every feeder and distribution edge is one they drew.
    for kind in ("feeder", "distribution"):
        for c in s.design.by_kind(kind):
            assert set(c.openftth["edges"]) <= drawn, (kind, c.props["name"])
            assert c.openftth.get("drawn_by") == "engineer"
            assert int(c.openftth["downstream_units"]) > 0, "sized from the assignments"
    # Every home reaches the OLT over the fibres the engine numbered.
    for h in homes:
        assert trace_home(s.design, s.rules, h.element_uuid)["reaches_olt"], h.props["name"]
    # Two FATs are too few: the checks say so and the work order cannot be posted.
    assert _errors(s)
    _ok(open_work_order(s, "Hand-built two blocks"))
    refused = post_work_order(s)
    assert refused.summary.startswith("rejected") and "must be fixed" in refused.summary
    # The engineer fixes it: smaller groups, one FAT each, then the first two FATs go.
    groups = _street_groups(s, homes, reach_m=80.0, size=20)
    fats = [_fat_at(s, *_node_lonlat(s, seed), why) for seed, _g in groups]
    groups = [g for _seed, g in groups]
    for f, g in zip(fats, groups, strict=True):
        _assign(s, f, [h.element_uuid for h in g], "split the group")
        drawn |= _draw_to(s, "distribution", fdh, f, why)
    _assign(s, fdh, fats, why)
    spare = [f for f in first if f not in fats]
    if spare:
        _ok(edit.delete_elements(s, spare, "replaced by smaller groups"))
    _ok(edit.complete_network(s, "finish the corrected layout"))
    assert not _errors(s), _errors(s)[:3]
    for c in s.design.by_kind("distribution"):
        assert set(c.openftth["edges"]) <= drawn, c.props["name"]
    for h in homes:
        assert trace_home(s.design, s.rules, h.element_uuid)["reaches_olt"], h.props["name"]
    posted = _ok(post_work_order(s))
    assert s.project.record() == posted.version
