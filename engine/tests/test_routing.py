"""The tree router: cheapest trees, and a path-length limit the cheapest tree must not break."""

from __future__ import annotations

import networkx as nx

from openftth_engine.solve.routing import route_tree


def _ladder() -> nx.Graph:
    """A cheap long way round (0 → 1 → 2 → 3, cost 1 per m) and a dear short way (0 → 3 directly,
    cost 5 per m), with a second terminal 4 hanging off node 2."""
    G = nx.Graph()
    for u, v, m, per_m in [
        (0, 1, 400, 1),
        (1, 2, 400, 1),
        (2, 3, 400, 1),
        (0, 3, 500, 5),
        (2, 4, 50, 1),
    ]:
        G.add_edge(u, v, length_m=float(m), cost=float(m * per_m), surface="pavement")
    return G


def test_the_cheapest_tree_goes_the_long_way_round():
    t = route_tree(_ladder(), 0, {3: 4, 4: 2})
    assert t.path_to_root(3) == [3, 2, 1, 0] and not t.grafted


def test_a_terminal_beyond_the_limit_is_grafted_onto_its_shortest_path():
    t = route_tree(_ladder(), 0, {3: 4, 4: 2}, max_path_m=1000)
    assert t.grafted == [3]
    assert t.path_to_root(3) == [3, 0]
    assert t.path_to_root(4) == [4, 2, 1, 0], "the other terminal keeps its path"
    assert nx.is_tree(t.tree)
    assert t.downstream_units[(0, 3)] == 4 and t.downstream_units[(0, 1)] == 2
    # Within the limit nothing changes.
    assert not route_tree(_ladder(), 0, {3: 4, 4: 2}, max_path_m=1300).grafted
