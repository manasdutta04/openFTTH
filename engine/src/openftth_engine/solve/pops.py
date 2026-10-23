"""Several POPs: where they go and which homes each one serves.

A POP (the OLT site) serves at most `pops.max_units` units. When an area needs more than one,
the sites come from a weighted k-means over the homes, and every home is then assigned to a POP
by a min-cost flow that keeps each POP within its capacity and the homes as close to their POP
as possible. Both steps are deterministic: fixed initial centres, a fixed number of rounds and
integer costs.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from ortools.graph.python import min_cost_flow

ROUNDS = 12  # alternations of assignment and re-centring


@dataclass
class Catchments:
    centres: np.ndarray  # (k, 2) metric coordinates of each POP
    owner: list[int]  # per home: index of its POP
    units: list[int]  # per POP: units served


def split_catchments(
    xy: np.ndarray,
    units: np.ndarray,
    count: int,
    capacity: int,
    fixed: np.ndarray | None = None,
) -> Catchments:
    """Split homes at `xy` (metric) with `units` into `count` catchments of at most `capacity`
    units. With `fixed` centres (POPs the engineer placed) only the assignment is solved."""
    n = len(xy)
    k = len(fixed) if fixed is not None else count
    if n == 0 or k <= 0:
        return Catchments(np.zeros((0, 2)), [], [])
    total = int(units.sum())
    cap = max(capacity, -(-total // k))  # never infeasible: share the excess evenly
    if fixed is not None:
        centres = np.asarray(fixed, dtype=float)
        owner = _assign(xy, units, centres, cap)
        return Catchments(centres, owner, _loads(owner, units, k))
    centres = _initial(xy, units, k)
    owner = _assign(xy, units, centres, cap)
    for _ in range(ROUNDS):
        moved = np.array(
            [
                np.average(
                    xy[np.asarray(owner) == j], axis=0, weights=units[np.asarray(owner) == j]
                )
                if (np.asarray(owner) == j).any()
                else centres[j]
                for j in range(k)
            ]
        )
        new_owner = _assign(xy, units, moved, cap)
        centres = moved
        if new_owner == owner:
            break
        owner = new_owner
    return Catchments(centres, owner, _loads(owner, units, k))


def _loads(owner: list[int], units: np.ndarray, k: int) -> list[int]:
    loads = [0] * k
    for o, u in zip(owner, units, strict=False):
        loads[o] += int(u)
    return loads


def _initial(xy: np.ndarray, units: np.ndarray, k: int) -> np.ndarray:
    """Farthest-point seeding from the unit-weighted centre: spread out, and the same every time."""
    centre = np.average(xy, axis=0, weights=units)
    first = int(np.argmin(((xy - centre) ** 2).sum(axis=1)))
    picks = [first]
    d = ((xy - xy[first]) ** 2).sum(axis=1)
    while len(picks) < k:
        nxt = int(np.argmax(d * units))
        picks.append(nxt)
        d = np.minimum(d, ((xy - xy[nxt]) ** 2).sum(axis=1))
    return xy[picks].astype(float)


def _assign(xy: np.ndarray, units: np.ndarray, centres: np.ndarray, cap: int) -> list[int]:
    """Min-cost flow: each home's units go to one POP (they never split: a home's units are
    routed whole by assigning the home to the POP that takes most of its flow), within capacity.
    Solved with OR-Tools' network simplex on integer costs, so it is exact, fast on tens of
    thousands of homes, and the same every run."""
    n, k = len(xy), len(centres)
    total = int(units.sum())
    src, sink = 0, n + k + 1
    flow = min_cost_flow.SimpleMinCostFlow()
    for i in range(n):
        u = int(units[i])
        flow.add_arc_with_capacity_and_unit_cost(src, 1 + i, u, 0)
        dist = np.hypot(*(centres - xy[i]).T)
        for j in range(k):
            flow.add_arc_with_capacity_and_unit_cost(1 + i, 1 + n + j, u, int(round(dist[j])))
    for j in range(k):
        flow.add_arc_with_capacity_and_unit_cost(1 + n + j, sink, cap, 0)
    flow.set_node_supply(src, total)
    flow.set_node_supply(sink, -total)
    status = flow.solve()
    if status != flow.OPTIMAL:
        raise RuntimeError(f"POP catchment flow not solved (status {status})")
    took = np.zeros((n, k), dtype=np.int64)
    for arc in range(flow.num_arcs()):
        t, h = flow.tail(arc), flow.head(arc)
        if 1 <= t <= n and n < h <= n + k:
            took[t - 1, h - 1 - n] = flow.flow(arc)
    # The POP that takes most of a home's flow; ties go to the lower index.
    return [int(np.argmax(row)) for row in took]
