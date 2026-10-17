"""Capacitated site placement (FATs over homes, FDHs over FATs) with OR-Tools CP-SAT.

Two-level for scale: demand points are partitioned into cells (deterministic k-means) and
CP-SAT solves each cell with every point limited to its k nearest candidate sites by street
distance. Interleaved search with a fixed worker count and seed keeps results reproducible; a greedy
fallback covers time-outs.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import networkx as nx
import numpy as np
from ortools.sat.python import cp_model
from scipy.cluster.vq import kmeans2

CELL_SIZE = 500  # demand points per CP-SAT cell
K_NEAREST = 40  # candidate sites per demand point (vertices every ≤25 m → ~500 m of street)


@dataclass
class Demand:
    key: str  # caller's id (home uuid / FAT uuid)
    node: int  # graph node the demand sits on
    units: int
    offset_m: float = 0.0  # extra distance from the node (home → street)


@dataclass
class Site:
    node: int
    members: list[Demand] = field(default_factory=list)
    distances_m: dict[str, float] = field(default_factory=dict)  # member key → street distance
    slot: int = 0  # >0 when the fallback had to open a second site on the same node

    @property
    def units(self) -> int:
        return sum(m.units for m in self.members)


@dataclass
class ClusterStats:
    cells: int
    status: list[str]
    objective: float
    candidates: int
    fallback_cells: int


def capacitated_sites(
    G: nx.Graph,
    xy: np.ndarray,
    demands: list[Demand],
    capacity: int,
    max_dist_m: float,
    open_cost: float,
    per_m_cost: float,
    *,
    candidate_nodes: set[int] | None = None,
    time_limit_s: float = 20.0,
    seed: int = 0,
    k_nearest: int = K_NEAREST,
    min_units: int = 0,
    short_unit_cost: float = 0.0,
) -> tuple[list[Site], ClusterStats]:
    """Place sites on graph nodes so every demand is served within `max_dist_m` street
    distance by exactly one site, no site exceeds `capacity` units, and the sum of
    site opening cost and distance × per_m_cost is minimal (per cell)."""
    if not demands:
        return [], ClusterStats(0, [], 0.0, 0, 0)

    cells = _partition(demands, xy, seed)
    sites: list[Site] = []
    statuses: list[str] = []
    objective = 0.0
    n_cand = 0
    fallbacks = 0
    for cell in cells:
        reach = _reach(G, cell, max_dist_m, candidate_nodes, k_nearest)
        n_cand += len({c for r in reach.values() for c in r})
        cell_sites, status, obj = _solve_cell(
            cell,
            reach,
            capacity,
            open_cost,
            per_m_cost,
            time_limit_s,
            seed,
            min_units,
            short_unit_cost,
        )
        if status == "FALLBACK":
            fallbacks += 1
        statuses.append(status)
        objective += obj
        sites.extend(cell_sites)
    sites = _merge_same_node(sites, capacity)
    sites.sort(key=lambda s: (s.node, s.slot, s.units))
    return sites, ClusterStats(len(cells), statuses, objective, n_cand, fallbacks)


def _partition(demands: list[Demand], xy: np.ndarray, seed: int) -> list[list[Demand]]:
    if len(demands) <= CELL_SIZE:
        return [demands]
    k = int(np.ceil(len(demands) / CELL_SIZE))
    pts = np.array([xy[d.node] for d in demands], dtype=float)
    _, labels = kmeans2(pts, k, minit="++", seed=seed)
    cells: dict[int, list[Demand]] = {}
    for d, lab in zip(demands, labels, strict=True):
        cells.setdefault(int(lab), []).append(d)
    return [cells[k] for k in sorted(cells)]


def _reach(
    G: nx.Graph,
    cell: list[Demand],
    max_dist_m: float,
    candidate_nodes: set[int] | None,
    k_nearest: int,
) -> dict[str, dict[int, float]]:
    """Street distance from each demand to its K nearest candidate nodes within reach."""
    reach: dict[str, dict[int, float]] = {}
    for d in cell:
        budget = max(0.0, max_dist_m - d.offset_m)
        lengths = nx.single_source_dijkstra_path_length(G, d.node, cutoff=budget, weight="length_m")
        items = [
            (n, dist + d.offset_m)
            for n, dist in lengths.items()
            if candidate_nodes is None or n in candidate_nodes
        ]
        if not items:
            # Nothing within reach: use the own node (will violate the distance rule, reported later).
            items = [(d.node, d.offset_m)]
        items.sort(key=lambda t: (t[1], t[0]))
        reach[d.key] = dict(items[:k_nearest])
    return reach


def _solve_cell(
    cell: list[Demand],
    reach: dict[str, dict[int, float]],
    capacity: int,
    open_cost: float,
    per_m_cost: float,
    time_limit_s: float,
    seed: int,
    min_units: int = 0,
    short_unit_cost: float = 0.0,
) -> tuple[list[Site], str, float]:
    candidates = sorted({c for r in reach.values() for c in r})
    model = cp_model.CpModel()
    open_v = {c: model.NewBoolVar(f"open_{c}") for c in candidates}
    x: dict[tuple[str, int], cp_model.IntVar] = {}
    for d in cell:
        for c in reach[d.key]:
            x[(d.key, c)] = model.NewBoolVar(f"x_{d.key}_{c}")
        model.AddExactlyOne(x[(d.key, c)] for c in reach[d.key])
    for c in candidates:
        model.Add(
            sum(d.units * x[(d.key, c)] for d in cell if c in reach[d.key]) <= capacity * open_v[c]
        )
    # Redundant lower bound on the number of sites tightens the relaxation a lot.
    total_units = sum(d.units for d in cell)
    model.Add(sum(open_v.values()) >= -(-total_units // capacity))
    # Start from the greedy solution so the solver only has to improve it.
    for site in _greedy(cell, reach, capacity):
        if site.slot or site.node not in open_v:
            continue
        model.AddHint(open_v[site.node], 1)
        for m in site.members:
            model.AddHint(x[(m.key, site.node)], 1)
    # Soft minimum fill: an open site with fewer than `min_units` pays for each missing unit.
    short: dict[int, cp_model.IntVar] = {}
    if min_units > 0 and short_unit_cost > 0:
        for c in candidates:
            short[c] = model.NewIntVar(0, min_units, f"short_{c}")
            assigned = sum(d.units * x[(d.key, c)] for d in cell if c in reach[d.key])
            model.Add(short[c] >= min_units * open_v[c] - assigned)
    scale = 100  # cents
    model.Minimize(
        sum(int(round(open_cost * scale)) * open_v[c] for c in candidates)
        + sum(int(round(short_unit_cost * scale)) * v for v in short.values())
        + sum(
            int(round(dist * per_m_cost * scale)) * x[(d.key, c)]
            for d in cell
            for c, dist in reach[d.key].items()
        )
    )
    solver = cp_model.CpSolver()
    # Interleaved search with a fixed worker count and seed is deterministic in CP-SAT.
    solver.parameters.num_workers = 8
    solver.parameters.interleave_search = True
    solver.parameters.random_seed = seed
    solver.parameters.max_time_in_seconds = time_limit_s
    status = solver.Solve(model)
    name = solver.StatusName(status)
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        sites: dict[int, Site] = {}
        for d in cell:
            for c, dist in reach[d.key].items():
                if solver.Value(x[(d.key, c)]):
                    s = sites.setdefault(c, Site(node=c))
                    s.members.append(d)
                    s.distances_m[d.key] = dist
                    break
        for s in sites.values():
            s.members.sort(key=lambda m: m.key)
        return list(sites.values()), name, solver.ObjectiveValue() / scale
    return _greedy(cell, reach, capacity), "FALLBACK", float("nan")


def _greedy(cell: list[Demand], reach: dict[str, dict[int, float]], capacity: int) -> list[Site]:
    """Capacity-respecting first-fit by distance. Never exceeds `capacity`; if every reachable
    node already hosts a full site, it opens a second site (slot 1, 2, …) on the nearest node."""
    sites: dict[tuple[int, int], Site] = {}
    for d in sorted(cell, key=lambda d: (-d.units, d.key)):
        ordered = sorted(reach[d.key].items(), key=lambda t: (t[1], t[0]))
        target: Site | None = None
        for c, _dist in ordered:  # an open site with room
            for (node, _slot), s in sites.items():
                if node == c and s.units + d.units <= capacity:
                    target = s
                    break
            if target:
                break
        if target is None:  # a node with no site yet
            for c, _dist in ordered:
                if not any(node == c for (node, _slot) in sites):
                    target = sites.setdefault((c, 0), Site(node=c))
                    break
        if target is None:  # everything open and full: another slot on the nearest node
            c = ordered[0][0]
            slot = 1 + max(s for (node, s) in sites if node == c)
            target = sites.setdefault((c, slot), Site(node=c, slot=slot))
        target.members.append(d)
        target.distances_m[d.key] = reach[d.key][target.node]
    return list(sites.values())


def _merge_same_node(sites: list[Site], capacity: int) -> list[Site]:
    """Cells are solved independently, so two cells may open a site on the same node. Merge
    them while the capacity allows; otherwise keep separate sites with distinct slots."""
    by_node: dict[int, list[Site]] = {}
    for s in sites:
        by_node.setdefault(s.node, []).append(s)
    out: list[Site] = []
    for node in sorted(by_node):
        group = sorted(by_node[node], key=lambda s: (-s.units, s.slot))
        bins: list[Site] = []
        for s in group:
            target = next((b for b in bins if b.units + s.units <= capacity), None)
            if target is None:
                bins.append(
                    Site(
                        node=node,
                        members=list(s.members),
                        distances_m=dict(s.distances_m),
                        slot=len(bins),
                    )
                )
            else:
                target.members.extend(s.members)
                target.distances_m.update(s.distances_m)
        for b in bins:
            b.members.sort(key=lambda m: m.key)
        out.extend(bins)
    return out
