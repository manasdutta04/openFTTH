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
    grafted: list[int] = field(default_factory=list)  # terminals moved onto their shortest path

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
    G: nx.Graph,
    root: int,
    terminals: dict[int, int],
    weight: str = "cost",
    max_path_m: float | None = None,
) -> RoutedTree:
    """Connect `root` to every terminal node with an approximate min-cost Steiner tree.

    With `max_path_m`, a terminal whose path along the tree is longer than that is grafted onto
    its shortest street path from the root, so the cheapest tree never breaks a distance rule
    the shortest path keeps.
    """
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
    grafted: list[int] = []
    if max_path_m is not None and T.number_of_nodes() > 1:
        grafted = _graft_long_paths(G, T, root, reachable, max_path_m)
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
        grafted,
    )


def _graft_long_paths(
    G: nx.Graph, T: nx.Graph, root: int, terminals: set[int], max_path_m: float
) -> list[int]:
    """Rewire `T` in place so each terminal over `max_path_m` follows its shortest path.

    Walking that path from the root, every node takes its predecessor on the path as its new
    parent; a node already in the tree is cut from its old parent first. Nodes are fixed in
    path order, so the tree stays a tree, and since a shortest path's prefix is shortest too,
    no other terminal's path gets longer. Branches left leading nowhere are pruned.
    """

    def length(u: int, v: int, d: dict) -> float:
        # A bore is the last resort for a graft: priced in, not measured as its short length.
        return float(d["length_m"]) + (10_000.0 if d.get("crossing") else 0.0)

    grafted: list[int] = []
    for t in sorted(terminals):
        along = nx.shortest_path_length(T, root, t, weight="length_m")
        if along <= max_path_m + 1e-6:
            continue
        path = nx.shortest_path(G, root, t, weight=length)
        shortest = sum(G[u][v]["length_m"] for u, v in zip(path[:-1], path[1:], strict=False))
        if shortest >= along - 1e-6:
            continue  # the tree already follows a shortest path: nothing to gain
        parent = dict(nx.bfs_predecessors(T, root))
        for prev, node in zip(path[:-1], path[1:], strict=False):
            old = parent.get(node)
            if node in T and old == prev:
                continue
            if node in T and old is not None:
                T.remove_edge(old, node)
            T.add_edge(prev, node, **G[prev][node])
            parent = dict(nx.bfs_predecessors(T, root))
        grafted.append(t)
    if grafted:
        keep = set(terminals) | {root}
        leaves = [n for n in T.nodes if T.degree(n) <= 1 and n not in keep]
        while leaves:
            T.remove_nodes_from(leaves)
            leaves = [n for n in T.nodes if T.degree(n) <= 1 and n not in keep]
    return grafted


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
