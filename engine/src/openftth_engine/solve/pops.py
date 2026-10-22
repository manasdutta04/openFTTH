"""Several POPs: where they go and which homes each one serves.

A POP (the OLT site) serves at most `pops.max_units` units. When an area needs more than one,
the sites come from a weighted k-means over the homes, and every home is then assigned to a POP
by a min-cost flow that keeps each POP within its capacity and the homes as close to their POP
as possible. Both steps are deterministic: fixed initial centres, a fixed number of rounds and
integer costs.
"""

from __future__ import annotations

from dataclasses import dataclass

import networkx as nx
import numpy as np

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
    routed whole by assigning the home to the POP that takes most of its flow), within capacity."""
    G = nx.DiGraph()
    total = int(units.sum())
    G.add_node("src", demand=-total)
    G.add_node("sink", demand=total)
    for j in range(len(centres)):
        G.add_edge(f"p{j}", "sink", capacity=cap, weight=0)
    for i in range(len(xy)):
        u = int(units[i])
        G.add_edge("src", f"h{i}", capacity=u, weight=0)
        dist = np.hypot(*(centres - xy[i]).T)
        for j in range(len(centres)):
            G.add_edge(f"h{i}", f"p{j}", capacity=u, weight=int(round(dist[j])))
    flow = nx.min_cost_flow(G)
    owner = []
    for i in range(len(xy)):
        out = flow[f"h{i}"]
        owner.append(max(range(len(centres)), key=lambda j: (out.get(f"p{j}", 0), -j)))
    return owner
