"""Fibre assignment on a routed cable tree.

Fibres are numbered globally per tree (1..N) in depth-first order from the root, so the fibres
serving any subtree form a contiguous range. A cable section carries exactly the range of its
subtree; its local fibre numbering starts at 1, which gives the splice mapping at every node.
"""

from __future__ import annotations

from dataclasses import dataclass, field

TIA_598_COLORS = [
    "blue",
    "orange",
    "green",
    "brown",
    "slate",
    "white",
    "red",
    "black",
    "yellow",
    "violet",
    "rose",
    "aqua",
]
FIBERS_PER_TUBE = 12


def tube_fiber(local: int, fibers_per_tube: int = FIBERS_PER_TUBE) -> tuple[int, int]:
    """1-based (tube, fibre-in-tube) for a 1-based local fibre number."""
    return (local - 1) // fibers_per_tube + 1, (local - 1) % fibers_per_tube + 1


def color_name(n: int) -> str:
    return TIA_598_COLORS[(n - 1) % len(TIA_598_COLORS)]


@dataclass
class Section:
    uuid: str
    up: int
    down: int


@dataclass
class TreeFibers:
    root: int
    #: section uuid → (first, last) global fibre carried
    section_range: dict[str, tuple[int, int]] = field(default_factory=dict)
    #: terminal node → (first, last) global fibre terminating there
    terminal_range: dict[int, tuple[int, int]] = field(default_factory=dict)
    #: node → incoming section uuid (None for the root)
    incoming: dict[int, str | None] = field(default_factory=dict)
    #: node → outgoing section uuids in DFS order
    outgoing: dict[int, list[str]] = field(default_factory=dict)
    total: int = 0

    def local(self, section_uuid: str, g: int) -> int:
        start = self.section_range[section_uuid][0]
        return g - start + 1


def assign_tree_fibers(root: int, sections: list[Section], demand_at: dict[int, int]) -> TreeFibers:
    """Allocate contiguous global fibre ranges to terminals and sections of a tree.

    `sections` are oriented (up → down). `demand_at[node]` fibres terminate at `node`.
    Terminals with zero demand get no range; sections whose subtree has no demand get (0, -1).
    Demand at the root itself is allocated first (equipment standing at the root node).
    """
    children: dict[int, list[Section]] = {}
    for s in sections:
        children.setdefault(s.up, []).append(s)
    for lst in children.values():
        lst.sort(key=lambda s: (s.down, s.uuid))

    tf = TreeFibers(root=root)
    tf.incoming[root] = None
    counter = 0

    def visit(node: int, via: Section | None) -> tuple[int, int]:
        nonlocal counter
        first = counter + 1
        if via is not None:
            tf.incoming[node] = via.uuid
        d = demand_at.get(node, 0)
        if d > 0:
            tf.terminal_range[node] = (counter + 1, counter + d)
            counter += d
        tf.outgoing[node] = [c.uuid for c in children.get(node, [])]
        for c in children.get(node, []):
            lo, hi = visit(c.down, c)
            tf.section_range[c.uuid] = (lo, hi)
        return first, counter

    visit(root, None)
    tf.total = counter
    return tf
