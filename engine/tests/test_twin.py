"""The twin keeps cables off roads you cannot trench, and cabinets keep their FATs in reach."""

from __future__ import annotations

import geopandas as gpd
import networkx as nx
from shapely.geometry import LineString, Point

from openftth_engine.rules import load_rulebook
from openftth_engine.solve.cluster import Demand, Site, repair_reach
from openftth_engine.twin.graph import build_twin


def _streets(rows: list[tuple[str, list[tuple[float, float]]]]) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {
            "highway": [h for h, _ in rows],
            "osm_id": list(range(len(rows))),
            "geometry": [LineString(pts) for _, pts in rows],
        },
        crs="EPSG:4326",
    )


def test_excluded_highway_classes_never_enter_the_route_graph():
    rules = load_rulebook("generic-gpon")
    lon, lat = 3.70, 51.05
    d = 0.002
    streets = _streets(
        [
            ("residential", [(lon, lat), (lon + d, lat)]),
            ("residential", [(lon + d, lat), (lon + d, lat + d)]),
            ("motorway", [(lon, lat + d), (lon + d, lat + d)]),
            ("steps", [(lon, lat), (lon, lat + d)]),
        ]
    )
    homes = gpd.GeoDataFrame({"units": [1], "geometry": [Point(lon + d / 2, lat)]}, crs="EPSG:4326")
    twin = build_twin(streets, homes, rules)
    classes = {data["highway"] for _, _, data in twin.graph.edges(data=True)}
    assert classes == {"residential"}, classes
    assert any("left out of the route graph" in d.reason for d in twin.decisions)


def test_nearest_node_ignores_street_fragments_outside_the_main_graph():
    rules = load_rulebook("generic-gpon")
    lon, lat = 3.70, 51.05
    d = 0.002
    streets = _streets(
        [
            ("residential", [(lon, lat), (lon + d, lat)]),
            ("residential", [(lon + d, lat), (lon + d, lat + d)]),
            # A lone fragment far to the north-east, like an unconnected service road.
            ("residential", [(lon + 3 * d, lat + 3 * d), (lon + 4 * d, lat + 3 * d)]),
        ]
    )
    homes = gpd.GeoDataFrame({"units": [1], "geometry": [Point(lon, lat)]}, crs="EPSG:4326")
    twin = build_twin(streets, homes, rules)
    assert any("disconnected" in d.reason for d in twin.decisions)
    # Ask for the node nearest the fragment: the answer must still be a routable node.
    frag = gpd.GeoSeries([Point(lon + 3.5 * d, lat + 3 * d)], crs="EPSG:4326").to_crs(twin.crs)[0]
    node, _ = twin.nearest_node(frag.x, frag.y)
    assert node in twin.graph


def test_repair_reach_moves_a_far_fat_to_a_cabinet_with_room():
    G = nx.path_graph(5)
    for u, v in G.edges:
        G[u][v]["length_m"] = 100.0
    near = Site(node=0, members=[Demand("a", 0, 10), Demand("far", 4, 10)])
    other = Site(node=4, members=[Demand("b", 4, 10)])
    sites, moved, stranded = repair_reach(G, [near, other], max_dist_m=250.0, capacity=50)
    assert moved == 1 and stranded == 0
    assert [m.key for m in other.members] == ["b", "far"]
    assert other.distances_m["far"] == 0.0
    assert len(sites) == 2


def test_repair_reach_reports_a_fat_nobody_can_reach():
    G = nx.path_graph(5)
    for u, v in G.edges:
        G[u][v]["length_m"] = 100.0
    only = Site(node=0, members=[Demand("far", 4, 10)])
    sites, moved, stranded = repair_reach(G, [only], max_dist_m=250.0, capacity=50)
    assert (moved, stranded) == (0, 1)
    assert sites[0].members[0].key == "far"
