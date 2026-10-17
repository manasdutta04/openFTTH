"""Rooted Steiner-tree routing over the street graph.

`route_tree` returns the tree oriented away from the root with per-edge downstream demand,
and splits it into cable sections between branch points / terminals so each section gets
its own fibre count.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import networkx as nx
from networkx.algorithms.approximation import steiner_tree


@dataclass
class Section:
    nodes: list[int]  # from upstream to downstream
    length_m: float
    cost: float
    downstream_units: int
    surfaces: dict[str, float] = field(default_factory=dict)  # surface → metres

    @property
    def upstream(self) -> int:
        return self.nodes[0]

    @property
    def downstream(self) -> int:
        return self.nodes[-1]


@dataclass
class RoutedTree:
    root: int
    terminals: dict[int, int]  # node → demand units at that terminal
    tree: nx.Graph
    parent: dict[int, int | None]
    downstream_units: dict[tuple[int, int], int]  # oriented edge (parent, child) → units
    sections: list[Section]
    unreachable: list[int] = field(default_factory=list)

    @property
    def edges(self) -> set[tuple[int, int]]:
        return {(u, v) if u < v else (v, u) for u, v in self.tree.edges}

    def total_length_m(self) -> float:
        return sum(d["length_m"] for _, _, d in self.tree.edges(data=True))

    def path_to_root(self, node: int) -> list[int]:
        out = [node]
        while self.parent.get(out[-1]) is not None:
            out.append(self.parent[out[-1]])  # type: ignore[arg-type]
        return out


def route_tree(
    G: nx.Graph, root: int, terminals: dict[int, int], weight: str = "cost"
) -> RoutedTree:
    """Connect `root` to every terminal node with an approximate min-cost Steiner tree."""
    reachable = {t for t in terminals if nx.has_path(G, root, t)}
    unreachable = sorted(set(terminals) - reachable)
    nodes = sorted(reachable | {root})
    if len(nodes) == 1:
        T = nx.Graph()
        T.add_node(root)
    else:
        T = nx.Graph(steiner_tree(G, nodes, weight=weight, method="mehlhorn"))
        # Make sure it is a tree (the approximation returns one; guard anyway).
        if not nx.is_tree(T):
            T = nx.minimum_spanning_tree(T, weight=weight)
    parent: dict[int, int | None] = {root: None}
    order: list[int] = []
    for u, v in nx.bfs_edges(T, root):
        parent[v] = u
        order.append(v)
    # Downstream demand per oriented edge.
    down: dict[int, int] = {n: 0 for n in T.nodes}
    for t in reachable:
        down[t] += terminals[t]
    for v in reversed(order):
        down[parent[v]] += down[v]  # type: ignore[index]
    downstream_units = {(parent[v], v): down[v] for v in order}  # type: ignore[misc]
    sections = _sections(G, T, root, parent, downstream_units, set(reachable))
    return RoutedTree(
        root,
        {t: terminals[t] for t in reachable},
        T,
        parent,
        downstream_units,
        sections,
        unreachable,
    )


def _sections(
    G: nx.Graph,
    T: nx.Graph,
    root: int,
    parent: dict[int, int | None],
    downstream_units: dict[tuple[int, int], int],
    terminals: set[int],
) -> list[Section]:
    children: dict[int, list[int]] = {}
    for v, p in parent.items():
        if p is not None:
            children.setdefault(p, []).append(v)
    for p in children:
        children[p].sort()
    sections: list[Section] = []
    stack = [root]
    while stack:
        start = stack.pop()
        for first in children.get(start, []):
            nodes = [start, first]
            cur = first
            # Extend while the node is a pass-through (one child, not a terminal).
            while len(children.get(cur, [])) == 1 and cur not in terminals:
                nxt = children[cur][0]
                nodes.append(nxt)
                cur = nxt
            length = 0.0
            cost = 0.0
            surfaces: dict[str, float] = {}
            for u, v in zip(nodes[:-1], nodes[1:], strict=False):
                d = G[u][v]
                length += d["length_m"]
                cost += d["cost"]
                surfaces[d["surface"]] = surfaces.get(d["surface"], 0.0) + d["length_m"]
            sections.append(
                Section(nodes, length, cost, downstream_units[(start, first)], surfaces)
            )
            stack.append(cur)
    sections.sort(key=lambda s: (s.nodes[0], s.nodes[-1]))
    return sections
