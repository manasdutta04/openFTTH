"""Phase P3: the engineer's own edits on a designed area, and undo and redo over them."""

from __future__ import annotations

from pathlib import Path

import pytest

from openftth_engine import edit
from openftth_engine.pipeline import (
    assign_fibers,
    assign_pon,
    build_twin_tool,
    cluster_homes,
    import_area_and_osm,
    open_session,
    place_chambers,
    place_fdh,
    route_distribution,
    route_drops,
    route_feeder,
    set_pop,
    size_cables,
)

GENT = Path(__file__).resolve().parents[2] / "fixtures" / "gent"


@pytest.fixture(scope="module")
def s(rules, tmp_path_factory):
    s = open_session(tmp_path_factory.mktemp("edit") / "p", rules, project_id="edit")
    import_area_and_osm(s, GENT / "area-small.geojson", GENT / "gent.osm")
    build_twin_tool(s)
    set_pop(s)
    cluster_homes(s, 6.0)
    place_fdh(s, 6.0)
    assign_pon(s)
    route_feeder(s)
    route_distribution(s)
    route_drops(s)
    place_chambers(s)
    size_cables(s)
    assign_fibers(s)
    return s


def _lonlat(f) -> tuple[float, float]:
    p = f.geometry.representative_point()
    return p.x, p.y


def test_every_edit_needs_a_reason(s):
    fat = s.design.by_kind("fat")[0]
    lon, lat = _lonlat(fat)
    for out in (
        edit.add_site(s, "fat", lon, lat, " "),
        edit.delete_element(s, fat.element_uuid, ""),
        edit.set_attribute(s, fat.element_uuid, "name", "X", ""),
    ):
        assert out.summary.startswith("rejected") and out.version is None


def test_a_placed_fat_is_pinned_and_takes_a_reassigned_home(s):
    home = sorted(s.design.by_kind("home"), key=lambda h: h.element_uuid)[0]
    old_fat = home.openftth["fat_id"]
    lon, lat = _lonlat(home)
    added = edit.add_site(s, "fat", lon + 0.0003, lat, "a pole-mounted FAT the operator asked for")
    assert added.version and "violations" in added.data
    fat = s.design.get(added.data["element"])
    assert fat.openftth["placed_by"] == "engineer" and fat.openftth["units"] == 0
    again = edit.add_site(s, "fat", lon + 0.0003, lat, "twice")
    assert again.summary.startswith("rejected")
    moved = edit.reassign(s, home.element_uuid, fat.element_uuid, "the owner asked for this FAT")
    assert moved.version, moved.summary
    assert s.design.get(home.element_uuid).openftth["fat_id"] == fat.element_uuid
    assert s.design.get(fat.element_uuid).openftth["units"] == int(home.openftth["units"])
    assert "route_drops" in moved.data["reran"]
    refused = edit.delete_element(s, fat.element_uuid, "not needed")
    assert refused.summary.startswith("rejected") and "reassign" in refused.summary
    back = edit.reassign(s, home.element_uuid, old_fat, "back again")
    assert back.version
    gone = edit.delete_element(s, fat.element_uuid, "not needed after all")
    assert gone.version and fat.element_uuid not in s.design


def test_a_drawn_cable_follows_the_streets_and_survives_routing(s):
    fdh, fat = s.design.by_kind("fdh")[0], s.design.by_kind("fat")[-1]
    out = edit.draw_cable(s, "distribution", [_lonlat(fdh), _lonlat(fat)], "spare ring for later")
    assert out.version, out.summary
    cable = s.design.get(out.data["element"])
    assert cable.openftth["drawn_by"] == "engineer" and cable.props["length_m"] > 0
    assert cable.props["fiber_count"] == min(s.rules.fibers.distribution)
    bad = edit.draw_cable(s, "distribution", [_lonlat(fdh), _lonlat(fat)], "x", fiber_count=7)
    assert bad.summary.startswith("rejected")
    route_distribution(s)
    assert cable.element_uuid in s.design, "routing keeps what the engineer drew"


def test_split_set_and_delete(s):
    cable = max(s.design.by_kind("distribution"), key=lambda c: len(c.geometry.coords))
    mid = cable.geometry.coords[len(cable.geometry.coords) // 2]
    out = edit.split_cable(s, cable.element_uuid, mid[0], mid[1], "a joint at the new estate")
    assert out.version, out.summary
    a, b = (s.design.get(u) for u in out.data["parts"])
    assert a.geometry.coords[-1] == b.geometry.coords[0]
    assert any(
        c.props.get("manhole_id") == f"CH-{out.data['node']}" for c in s.design.by_kind("chamber")
    )
    named = edit.set_attribute(s, a.element_uuid, "name", "DST-NEW-1", "naming per the as-built")
    assert named.version and s.design.get(a.element_uuid).props["name"] == "DST-NEW-1"
    assert edit.set_attribute(s, a.element_uuid, "fiber_count", 13, "x").summary.startswith(
        "rejected"
    )
    assert edit.set_attribute(s, a.element_uuid, "status", "bogus", "x").summary.startswith(
        "rejected"
    )
    top = max(s.rules.fibers.distribution)
    sized = edit.set_attribute(s, a.element_uuid, "fiber_count", top, "future growth")
    assert sized.version and s.design.get(a.element_uuid).props["fiber_count"] == top
    conns = len(s.design.tables.get("fiber_connection", []))
    gone = edit.delete_element(s, b.element_uuid, "replaced by aerial")
    assert gone.version and b.element_uuid not in s.design
    assert len(s.design.tables.get("fiber_connection", [])) <= conns


def test_undo_and_redo_step_through_the_engineers_edits(s):
    pop = s.design.by_kind("pop")[0]
    taken = {int(c.openftth["node"]) for c in s.design.by_kind("chamber")}
    spot = next(
        h
        for h in sorted(s.design.by_kind("home"), key=lambda h: h.element_uuid)
        if int(h.openftth["node"]) not in taken
    )
    added = edit.add_site(s, "chamber", *_lonlat(spot), "access for the cabinet")
    assert added.version, added.summary
    uuid = added.data["element"]
    assert uuid in s.design and edit.edit_history(s)["can_undo"]
    undone = edit.undo_edit(s)
    assert undone.version and uuid not in s.design
    assert s.design.get(pop.element_uuid), "the rest of the design is untouched"
    assert edit.edit_history(s)["can_redo"]
    redone = edit.redo_edit(s)
    assert redone.version and uuid in s.design
    assert edit.redo_edit(s).summary.startswith("rejected"), "nothing left to redo"
    # A solver run after an edit means undo would throw it away: refused.
    place_chambers(s)
    assert edit.undo_edit(s).summary.startswith("rejected")


def test_undo_and_redo_walk_back_over_several_edits(s):
    a, b = sorted(s.design.by_kind("fat"), key=lambda f: f.element_uuid)[:2]
    names = (a.props.get("name"), b.props.get("name"))
    assert edit.set_attribute(s, a.element_uuid, "name", "FAT-A", "survey").version
    assert edit.set_attribute(s, b.element_uuid, "name", "FAT-B", "survey").version
    assert edit.undo_edit(s).version and edit.undo_edit(s).version, "two undos in a row"
    assert (
        s.design.get(a.element_uuid).props.get("name"),
        s.design.get(b.element_uuid).props.get("name"),
    ) == names
    assert edit.redo_edit(s).version and edit.redo_edit(s).version, "two redos in a row"
    assert s.design.get(b.element_uuid).props["name"] == "FAT-B"
    assert edit.edit_history(s)["can_undo"] and not edit.edit_history(s)["can_redo"]


def _through_cable(s):
    """A distribution cable with an inner street node and a connected home behind it."""
    from openftth_engine.network.trace import build_connectivity, homes_behind, trace_home

    conn = build_connectivity(s.design)
    for c in sorted(s.design.by_kind("distribution"), key=lambda c: c.element_uuid):
        if len(edit._cable_nodes(s, c)) < 4:
            continue
        for h in homes_behind(s.design, c.element_uuid, conn):
            if trace_home(s.design, s.rules, h)["reaches_olt"]:
                return c, h
    pytest.skip("no distribution cable with an inner node and a traced home")


def test_split_and_join_keep_every_fibre_continuous(s):
    from openftth_engine.network.trace import trace_home

    cable, home = _through_cable(s)
    uuid = cable.element_uuid
    name_before = cable.props["name"]
    nodes = edit._cable_nodes(s, cable)
    twin = edit._twin(s)
    mid = edit._wgs(twin, twin.node_point(nodes[len(nodes) // 2]))
    out = edit.split_cable(s, uuid, mid.x, mid.y, "closure for a later branch")
    assert out.version, out.summary
    second = out.data["parts"][1]
    assert "spliced straight through" in out.summary
    traced = trace_home(s.design, s.rules, home)
    assert traced["reaches_olt"], "the split cable still carries the home's fibre"
    joined = edit.join_cables(s, uuid, second, "the branch is not needed")
    assert joined.version, joined.summary
    assert second not in s.design and "straight splice(s) at the joint removed" in joined.summary
    assert s.design.get(uuid).props["name"] == name_before, "the halves get their old name back"
    assert any("holds no splice" in w for w in joined.warnings)
    assert trace_home(s.design, s.rules, home)["reaches_olt"]
    rows = s.design.tables["fiber_connection"]
    assert not any(second in (r.get("source_cable_uuid"), r.get("dest_cable_uuid")) for r in rows)


def test_a_joint_that_crosses_fibres_is_refused(s):
    cable, _home = _through_cable(s)
    nodes = edit._cable_nodes(s, cable)
    twin = edit._twin(s)
    mid = edit._wgs(twin, twin.node_point(nodes[len(nodes) // 2]))
    out = edit.split_cable(s, cable.element_uuid, mid.x, mid.y, "test closure")
    second = out.data["parts"][1]
    row = next(
        r
        for r in s.design.tables["fiber_connection"]
        if r.get("dest_cable_uuid") in (cable.element_uuid, second)
        and r.get("source_cable_uuid") in (cable.element_uuid, second)
    )
    capacity = int(cable.props["fiber_count"]) * int(cable.props.get("cable_count", 1))
    used = {
        (r.get("dest_tube"), r.get("dest_fiber"))
        for r in s.design.tables["fiber_connection"]
        if r.get("dest_cable_uuid") == row["dest_cable_uuid"]
    }
    free = next(
        n for n in range(1, capacity + 1) if edit.tube_fiber(n, edit.FIBERS_PER_TUBE) not in used
    )
    taken = next(
        n for n in range(1, capacity + 1) if edit.tube_fiber(n, edit.FIBERS_PER_TUBE) in used
    )
    clash = edit.edit_splice(s, row["uuid"], "re-patch", dest_fiber=taken)
    assert clash.summary.startswith("rejected") and "already used" in clash.summary
    assert edit.edit_splice(s, row["uuid"], "re-patch", dest_fiber=capacity + 1).summary.startswith(
        "rejected"
    )
    moved = edit.edit_splice(s, row["uuid"], "damaged fibre", dest_fiber=free)
    assert moved.version, moved.summary
    refused = edit.join_cables(s, cable.element_uuid, second, "undo the branch")
    assert refused.summary.startswith("rejected") and "re-patch it straight" in refused.summary
    patched = edit.edit_splice(s, row["uuid"], "connector", connection_type="patch", tray_ref="T2")
    assert patched.version
    now = next(r for r in s.design.tables["fiber_connection"] if r["uuid"] == row["uuid"])
    assert now["connection_type"] == "patch" and now["loss_db"] == s.rules.loss_db.connector
    assert now["tray_ref"] == "T2"
    gone = edit.edit_splice(s, row["uuid"], "not needed", remove=True)
    assert gone.version and not any(
        r["uuid"] == row["uuid"] for r in s.design.tables["fiber_connection"]
    )
    assert edit.undo_edit(s).version, "a splice edit is undone like any other"
    assert any(r["uuid"] == row["uuid"] for r in s.design.tables["fiber_connection"])


def test_delete_several_is_one_edit_and_violations_are_listed(s):
    taken = {int(c.openftth["node"]) for c in s.design.by_kind("chamber")}
    spots = [
        h
        for h in sorted(s.design.by_kind("home"), key=lambda h: h.element_uuid)
        if int(h.openftth["node"]) not in taken
    ]
    made = []
    for h in spots:
        out = edit.add_site(s, "chamber", *_lonlat(h), "survey")
        if out.version:
            made.append(out.data["element"])
        if len(made) == 2:
            break
    home = spots[0].element_uuid
    out = edit.delete_elements(s, [*made, home], "not needed")
    assert out.version, out.summary
    assert all(u not in s.design for u in made) and home in s.design
    assert any("left as they are" in w for w in out.warnings)
    assert edit.undo_edit(s).version and all(u in s.design for u in made), (
        "one undo brings both back"
    )
    fat = next(f for f in s.design.by_kind("fat") if int(f.openftth.get("units", 0)) > 0)
    assert edit.delete_elements(s, [fat.element_uuid], "x").summary.startswith("rejected")
    head = s.project.head()
    listed = edit.list_violations(s)
    assert set(listed) == {"count", "by_check", "items"} and s.project.head() == head
