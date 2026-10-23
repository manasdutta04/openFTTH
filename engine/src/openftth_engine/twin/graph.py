"""The 2D digital twin: a street graph with trench costs, snapped homes and candidate sites.

Everything downstream (clustering, routing, drops, budgets) works on this graph in a local
metric CRS. Node ids are small integers; `twin.xy[node]` gives projected coordinates.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import geopandas as gpd
import networkx as nx
import numpy as np
import pandas as pd
from pyproj import CRS
from scipy.spatial import cKDTree
from shapely.geometry import LineString, Point

from ..results import Decision
from ..rules import Rulebook

DENSIFY_M = 25.0  # insert vertices so no edge is longer than this (better snapping/sites)
DUCT_LINK_M = 20.0  # an existing duct joins the street network where they come this close
SURFACES = ("asphalt", "pavement", "verge", "aerial", "facade", "existing_duct")
OVERHEAD = ("aerial", "facade")  # no digging: a no-dig zone does not stop them


@dataclass
class Twin:
    crs: CRS
    graph: nx.Graph  # nodes: int; edges: length_m, surface, cost, highway, osm_id
    xy: np.ndarray  # (n, 2) projected coordinates per node id
    homes: gpd.GeoDataFrame  # projected; columns osm_id, units, kind, node, offset_m, geometry
    streets: gpd.GeoDataFrame  # projected; highway, surface, cost_per_m, osm_id, geometry
    decisions: list[Decision] = field(default_factory=list)
    _tree: cKDTree | None = None
    _tree_nodes: np.ndarray | None = None

    @property
    def kdtree(self) -> cKDTree:
        """Spatial index over the nodes of the routable graph only (fragments are left out)."""
        if self._tree is None:
            self._tree_nodes = np.fromiter(sorted(self.graph.nodes), dtype=int)
            self._tree = cKDTree(self.xy[self._tree_nodes])
        return self._tree

    def nearest_node(self, x: float, y: float) -> tuple[int, float]:
        d, i = self.kdtree.query([x, y])
        assert self._tree_nodes is not None
        return int(self._tree_nodes[i]), float(d)

    def node_point(self, node: int) -> Point:
        return Point(self.xy[node])

    def path_line(self, nodes: list[int]) -> LineString:
        return LineString([tuple(self.xy[n]) for n in nodes])

    def edge_key(self, u: int, v: int) -> tuple[int, int]:
        return (u, v) if u < v else (v, u)

    def to_wgs84(self, gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
        return gdf.to_crs(4326)

    def total_units(self) -> int:
        return int(self.homes["units"].sum()) if not self.homes.empty else 0

    def forbid_ways(self, osm_ids: set[str]) -> tuple[int, float]:
        """Take the given OSM ways out of the route graph (the engineer's no-dig list), keep
        the largest connected part, and flag the streets. Node ids stay as they are so every
        sited element keeps its node unless that node left the graph.

        Returns the number of edges removed and their length in metres.
        """
        gone = [
            (u, v) for u, v, d in self.graph.edges(data=True) if str(d.get("osm_id")) in osm_ids
        ]
        length = float(sum(self.graph[u][v]["length_m"] for u, v in gone))
        if gone:
            self.graph.remove_edges_from(gone)
            comps = sorted(nx.connected_components(self.graph), key=len, reverse=True)
            if len(comps) > 1:
                self.graph = self.graph.subgraph(comps[0]).copy()
            self._tree = None
            self._tree_nodes = None
        self.streets["no_dig"] = self.streets["osm_id"].astype(str).isin(osm_ids)
        return len(gone), length

    def forbid_zones(self, zones: gpd.GeoDataFrame) -> tuple[int, float]:
        """Take every street edge whose middle lies in a no-dig zone out of the route graph.
        Existing ducts stay: cables can be pulled through them without digging. Aerial and
        facade spans need no digging either: only a keep-out zone stops them."""
        if zones.empty:
            return 0, 0.0
        z = zones.to_crs(self.crs)
        polys = z.geometry.union_all()
        kinds = z["kind"] if "kind" in z.columns else None
        keep_out = (
            z.geometry[kinds.astype(str) == "keep_out"].union_all() if kinds is not None else None
        )
        gone = []
        for u, v, d in self.graph.edges(data=True):
            if d.get("highway") == "existing_duct":
                continue
            mx = (self.xy[u][0] + self.xy[v][0]) / 2.0
            my = (self.xy[u][1] + self.xy[v][1]) / 2.0
            inside = keep_out if d.get("surface") in OVERHEAD else polys
            if inside is not None and not inside.is_empty and inside.contains(Point(mx, my)):
                gone.append((u, v))
        length = float(sum(self.graph[u][v]["length_m"] for u, v in gone))
        if gone:
            self.graph.remove_edges_from(gone)
            comps = sorted(nx.connected_components(self.graph), key=len, reverse=True)
            if len(comps) > 1:
                self.graph = self.graph.subgraph(comps[0]).copy()
            self._tree = None
            self._tree_nodes = None
        return len(gone), length

    def resnap_homes(self) -> int:
        """Snap the homes whose street node left the graph to the nearest remaining node."""
        if self.homes.empty:
            return 0
        stale = ~self.homes["node"].isin(list(self.graph.nodes))
        if not stale.any():
            return 0
        pts = np.column_stack(
            [self.homes.loc[stale].geometry.x.values, self.homes.loc[stale].geometry.y.values]
        )
        d, i = self.kdtree.query(pts)
        assert self._tree_nodes is not None
        self.homes.loc[stale, "node"] = self._tree_nodes[i].astype(int)
        self.homes.loc[stale, "offset_m"] = d.astype(float)
        return int(stale.sum())


def build_twin(
    streets_wgs: gpd.GeoDataFrame,
    homes_wgs: gpd.GeoDataFrame,
    rules: Rulebook,
    barriers_wgs: gpd.GeoDataFrame | None = None,
    buildings_wgs: gpd.GeoDataFrame | None = None,
    poles_wgs: gpd.GeoDataFrame | None = None,
) -> Twin:
    """Project to a metric CRS, build the street graph, offer priced crossings under the
    barriers (motorways, railways, rivers) and snap homes to it."""
    if streets_wgs.empty:
        raise ValueError("no streets in the area; cannot build a twin")
    crs = streets_wgs.estimate_utm_crs()
    decisions: list[Decision] = []
    excluded = set(rules.routing.exclude_highways)
    keep = ~streets_wgs["highway"].astype(str).isin(excluded)
    dropped_ways = int((~keep).sum())
    if dropped_ways:
        classes = sorted(set(streets_wgs.loc[~keep, "highway"].astype(str)))
        decisions.append(
            Decision(
                stage="twin",
                tool="build_twin",
                reason=f"{dropped_ways} way(s) of class {', '.join(classes)} left out of the route graph: "
                "no trench goes along a motorway, a staircase or a planned road.",
                values={"excluded_ways": dropped_ways, "classes": classes},
                source="rulebook",
            )
        )
    barriers = _barrier_lines(streets_wgs, barriers_wgs, crs)
    streets_wgs = streets_wgs[keep]
    if streets_wgs.empty:
        raise ValueError("no routable streets in the area; cannot build a twin")
    streets = streets_wgs.to_crs(crs).copy()
    homes = homes_wgs.to_crs(crs).copy() if not homes_wgs.empty else homes_wgs.copy()

    # A surface class given by the imported data wins over the one implied by the road class.
    given = (
        streets["surface_class"] if "surface_class" in streets.columns else [None] * len(streets)
    )
    streets["surface"] = [
        g if isinstance(g, str) and g in SURFACES else _surface_for(str(h), s, rules)
        for h, s, g in zip(
            streets["highway"],
            streets.get("surface", [None] * len(streets)),
            given,
            strict=False,
        )
    ]
    streets["cost_per_m"] = [rules.surfaces.cost(s) for s in streets["surface"]]

    G = nx.Graph()
    coords: dict[tuple[float, float], int] = {}
    xy: list[tuple[float, float]] = []

    def node_for(pt: tuple[float, float]) -> int:
        key = (round(pt[0], 2), round(pt[1], 2))
        n = coords.get(key)
        if n is None:
            n = len(xy)
            coords[key] = n
            xy.append(key)
            G.add_node(n)
        return n

    for row in streets.itertuples(index=False):
        line: LineString = row.geometry
        pts = _densify(list(line.coords), DENSIFY_M)
        for a, b in zip(pts[:-1], pts[1:], strict=False):
            u, v = node_for(a), node_for(b)
            if u == v:
                continue
            length = float(np.hypot(a[0] - b[0], a[1] - b[1]))
            cost = length * float(row.cost_per_m)
            if G.has_edge(u, v):
                if cost < G[u][v]["cost"]:
                    G[u][v].update(
                        length_m=length,
                        cost=cost,
                        surface=row.surface,
                        highway=str(row.highway),
                        osm_id=row.osm_id,
                    )
            else:
                G.add_edge(
                    u,
                    v,
                    length_m=length,
                    cost=cost,
                    surface=row.surface,
                    highway=str(row.highway),
                    osm_id=row.osm_id,
                )

    buildings = (
        buildings_wgs.to_crs(crs) if buildings_wgs is not None and not buildings_wgs.empty else None
    )
    poles = poles_wgs.to_crs(crs) if poles_wgs is not None and not poles_wgs.empty else None
    decisions.extend(_offer_overhead(G, xy, poles, buildings, rules))
    joined = _join_dead_ends(G, xy)
    if joined:
        decisions.append(
            Decision(
                stage="twin",
                tool="build_twin",
                reason=f"{joined} street end(s) stopping within {DEAD_END_SNAP_M:g} m of another street, "
                "without a shared vertex, were joined to it: street layers from CAD or GIS exports "
                "often leave junctions unsplit.",
                values={"joined_ends": joined},
                source="assumption",
            )
        )
    _link_ducts(G, xy, rules)
    offered = _add_crossings(G, xy, barriers, rules)
    if offered:
        kinds = sorted({k for k, _ in offered})
        decisions.append(
            Decision(
                stage="twin",
                tool="build_twin",
                reason=f"{len(offered)} crossing(s) offered under the {', '.join(kinds)} where streets on both sides are "
                f"within {rules.crossings.max_length_m:g} m; each is priced as a directional drill (a fixed cost for the "
                "permit and pits plus a cost per metre), so routing takes one only when it is cheaper than going round.",
                values={"crossings_offered": len(offered), "barriers": kinds},
                source="rulebook",
            )
        )
    found = _add_gap_crossings(G, xy, buildings, rules)
    gaps = [k for k, kind in found if kind == "unmapped"]
    joins = [k for k, kind in found if kind == "join"]
    if joins:
        decisions.append(
            Decision(
                stage="twin",
                tool="build_twin",
                reason=f"{len(joins)} place(s) where two streets come within {MAPPING_GAP_M:g} m without "
                "meeting in the map were joined as street: a gap that small is a slip in the map data.",
                values={"joins": len(joins)},
                source="assumption",
            )
        )
    if gaps:
        decisions.append(
            Decision(
                stage="twin",
                tool="build_twin",
                reason=f"{len(gaps)} crossing(s) offered where streets come within "
                f"{rules.crossings.max_length_m:g} m of each other but do not join within {GAP_REACH_M:g} m: "
                "something the map does not show lies between them (a canal, a railway, private land). "
                "Each is priced as a directional drill under an unmapped obstacle; check it on site.",
                values={"crossings_offered": len(gaps)},
                source="assumption",
            )
        )
    xy_arr = np.asarray(xy, dtype=float)

    # Keep the largest connected component; report the rest as a data gap.
    comps = sorted(nx.connected_components(G), key=len, reverse=True)
    if len(comps) > 1:
        dropped = sum(len(c) for c in comps[1:])
        decisions.append(
            Decision(
                stage="twin",
                tool="build_twin",
                reason=f"Street graph has {len(comps)} disconnected parts; routing uses the largest "
                f"({len(comps[0])} nodes) and ignores {dropped} nodes in fragments.",
                values={"components": len(comps), "dropped_nodes": dropped},
                source="solver",
            )
        )
    main_nodes = comps[0]
    if len(comps) > 1:
        G = G.subgraph(main_nodes).copy()  # routing algorithms need a connected graph

    twin = Twin(crs=crs, graph=G, xy=xy_arr, homes=homes, streets=streets, decisions=decisions)
    # Snap homes to the nearest node of the main component.
    if not homes.empty:
        main_idx = np.fromiter(sorted(main_nodes), dtype=int)
        tree = cKDTree(xy_arr[main_idx])
        pts = np.column_stack([homes.geometry.x.values, homes.geometry.y.values])
        d, i = tree.query(pts)
        homes["node"] = main_idx[i].astype(int)
        homes["offset_m"] = d.astype(float)
        far = int((d > rules.distances.max_drop_m).sum())
        if far:
            decisions.append(
                Decision(
                    stage="twin",
                    tool="build_twin",
                    reason=f"{far} homes are farther than max_drop_m ({rules.distances.max_drop_m:g} m) "
                    "from any street vertex; they will fail the drop-length check.",
                    values={"homes_far_from_street": far},
                )
            )
    twin.homes = homes
    return twin


MOTORWAY_CLASSES = ("motorway", "motorway_link", "trunk", "trunk_link")
CROSSING_STEP_M = 30.0  # how often along a barrier the engine looks for a crossing
CROSSING_SPACING_M = 150.0  # at most one crossing per this stretch of one barrier


def _barrier_lines(
    streets_wgs: gpd.GeoDataFrame, barriers_wgs: gpd.GeoDataFrame | None, crs: CRS
) -> gpd.GeoDataFrame:
    """Motorways from the streets and railways/rivers from the import, projected, in a fixed order."""
    roads = streets_wgs[streets_wgs["highway"].astype(str).isin(MOTORWAY_CLASSES)]
    parts = [
        gpd.GeoDataFrame(
            {"kind": "motorway", "osm_id": roads["osm_id"].astype(str).values},
            geometry=roads.geometry.values,
            crs=streets_wgs.crs,
        )
    ]
    if barriers_wgs is not None and not barriers_wgs.empty:
        parts.append(barriers_wgs[["kind", "osm_id", "geometry"]].astype({"osm_id": str}))
    out = gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), geometry="geometry", crs=4326)
    out = out[out.geometry.notna() & ~out.geometry.is_empty].explode(
        index_parts=False, ignore_index=True
    )
    return out.to_crs(crs).sort_values(["kind", "osm_id"], kind="stable").reset_index(drop=True)


def _add_crossings(
    G: nx.Graph, xy: list[tuple[float, float]], barriers: gpd.GeoDataFrame, rules: Rulebook
) -> list[tuple[str, tuple[int, int]]]:
    """Offer a bore under a barrier wherever street nodes face each other across it.

    Walking each barrier line every CROSSING_STEP_M, the closest pair of street nodes on opposite
    sides whose straight link crosses the line and is at most `crossings.max_length_m` long becomes
    a candidate. A candidate is dropped when the streets already join within a short walk (a bridge
    or underpass is close), and kept crossings of one kind of barrier are at least CROSSING_SPACING_M
    apart. Each kept one is an edge whose cost is the rulebook's drilling price.
    """
    if barriers.empty or G.number_of_nodes() == 0:
        return []
    max_len = rules.crossings.max_length_m
    nodes = np.fromiter(sorted(G.nodes), dtype=int)
    pts = np.asarray([xy[n] for n in nodes], dtype=float)
    tree = cKDTree(pts)
    offered: list[tuple[str, tuple[int, int]]] = []
    taken: set[tuple[int, int]] = set()
    kept: dict[str, list[tuple[float, float]]] = {k: [] for k in barriers["kind"].unique()}
    for row in barriers.itertuples(index=False):
        line: LineString = row.geometry
        kind = str(row.kind)
        if line.length == 0 or not hasattr(rules.crossings, kind):
            continue
        steps = max(1, int(line.length // CROSSING_STEP_M))
        for k in range(steps + 1):
            at = min(line.length, k * CROSSING_STEP_M)
            p = np.asarray(line.interpolate(at).coords[0])
            q = np.asarray(line.interpolate(min(line.length, at + 1.0)).coords[0])
            t = (
                q - p
                if at + 1.0 <= line.length
                else p - np.asarray(line.interpolate(at - 1.0).coords[0])
            )
            norm = np.hypot(*t)
            if norm == 0:
                continue
            n = np.array([-t[1], t[0]]) / norm
            near = tree.query_ball_point(p, max_len)
            side = [(int(nodes[i]), float(np.dot(pts[i] - p, n))) for i in near]
            left = [i for i, d in side if d > 1.0]
            right = [i for i, d in side if d < -1.0]
            best: tuple[float, int, int] | None = None
            for a in left:
                for b in right:
                    d = float(np.hypot(xy[a][0] - xy[b][0], xy[a][1] - xy[b][1]))
                    if d > max_len or (best is not None and d >= best[0]):
                        continue
                    if not LineString([xy[a], xy[b]]).intersects(line):
                        continue
                    best = (d, a, b)
            if best is None:
                continue
            d, a, b = best
            key = (min(a, b), max(a, b))
            if key in taken or G.has_edge(a, b):
                continue
            mid = ((xy[a][0] + xy[b][0]) / 2.0, (xy[a][1] + xy[b][1]) / 2.0)
            if any(np.hypot(mid[0] - m[0], mid[1] - m[1]) < CROSSING_SPACING_M for m in kept[kind]):
                continue
            # Already joined nearby (a bridge, an underpass): no bore needed.
            reach = nx.single_source_dijkstra_path_length(
                G, a, cutoff=3 * d + 100.0, weight="length_m"
            )
            if b in reach:
                continue
            G.add_edge(
                a,
                b,
                length_m=d,
                cost=rules.crossings.cost(kind, d),
                surface="crossing",
                highway=f"crossing_{kind}",
                osm_id=f"crossing:{kind}:{key[0]}-{key[1]}",
                crossing=kind,
            )
            taken.add(key)
            offered.append((kind, key))
            kept[kind].append(mid)
    return offered


GAP_CELL_M = 400.0  # unmapped obstacles are looked for cell by cell
GAP_REACH_M = 600.0  # streets that do not join within this of a cell count as separated
MAPPING_GAP_M = 8.0  # closer than this, separated streets are a slip in the map: joined as street


def _add_gap_crossings(
    G: nx.Graph,
    xy: list[tuple[float, float]],
    buildings: gpd.GeoDataFrame | None,
    rules: Rulebook,
) -> list[tuple[tuple[int, int], str]]:
    """Offer a bore where streets face each other across something the map does not show.

    Two street nodes at most `crossings.max_length_m` apart whose streets do not join within
    GAP_REACH_M of them are separated by an obstacle the data leaves out (a canal, a railway,
    private land). Per cell of GAP_CELL_M, for each pair of street pieces that come that close,
    the shortest such link that crosses no building is offered, priced as `crossings.unmapped`.
    """
    if G.number_of_nodes() == 0:
        return []
    max_len = rules.crossings.max_length_m
    nodes = np.fromiter(sorted(G.nodes), dtype=int)
    pts = np.asarray([xy[n] for n in nodes], dtype=float)
    tree = cKDTree(pts)
    blocked = None
    if buildings is not None and not buildings.empty:
        from shapely.strtree import STRtree

        polys = [g for g in buildings.geometry if g is not None and not g.is_empty]
        blocked = STRtree(polys) if polys else None
    offered: list[tuple[tuple[int, int], str]] = []
    mids: list[tuple[float, float]] = []
    (minx, miny), (maxx, maxy) = pts.min(axis=0), pts.max(axis=0)
    for cx in np.arange(minx, maxx + 1e-6, GAP_CELL_M):
        for cy in np.arange(miny, maxy + 1e-6, GAP_CELL_M):
            core = (pts[:, 0] >= cx) & (pts[:, 0] < cx + GAP_CELL_M)
            core &= (pts[:, 1] >= cy) & (pts[:, 1] < cy + GAP_CELL_M)
            if not core.any():
                continue
            win = (pts[:, 0] >= cx - GAP_REACH_M) & (pts[:, 0] < cx + GAP_CELL_M + GAP_REACH_M)
            win &= (pts[:, 1] >= cy - GAP_REACH_M) & (pts[:, 1] < cy + GAP_CELL_M + GAP_REACH_M)
            sub = G.subgraph(nodes[win].tolist())
            piece: dict[int, int] = {}
            size: dict[int, int] = {}
            for i, comp in enumerate(
                sorted(nx.connected_components(sub), key=lambda c: (-len(c), min(c)))
            ):
                size[i] = len(comp)
                for n in comp:
                    piece[n] = i
            best: dict[tuple[int, int], tuple[float, int, int]] = {}
            for ia in np.flatnonzero(core):
                a = int(nodes[ia])
                for ib in tree.query_ball_point(pts[ia], max_len):
                    b = int(nodes[ib])
                    if b <= a or b not in piece:
                        continue
                    pa, pb = piece[a], piece[b]
                    if pa == pb or min(size[pa], size[pb]) < 3:
                        continue
                    d = float(np.hypot(*(pts[ia] - pts[ib])))
                    key = (min(pa, pb), max(pa, pb))
                    if key in best and best[key][0] <= d:
                        continue
                    seg = LineString([xy[a], xy[b]])
                    if blocked is not None and any(
                        blocked.geometries.take(i).intersects(seg) for i in blocked.query(seg)
                    ):
                        continue
                    best[key] = (d, a, b)
            for _key, (d, a, b) in sorted(best.items()):
                mid = ((xy[a][0] + xy[b][0]) / 2.0, (xy[a][1] + xy[b][1]) / 2.0)
                if any(np.hypot(mid[0] - m[0], mid[1] - m[1]) < CROSSING_SPACING_M for m in mids):
                    continue
                if G.has_edge(a, b):
                    continue
                key = (min(a, b), max(a, b))
                if d <= MAPPING_GAP_M:
                    # Two streets that all but touch: a slip in the map, joined as street.
                    G.add_edge(
                        a,
                        b,
                        length_m=d,
                        cost=d * rules.surfaces.cost("pavement"),
                        surface="pavement",
                        highway="gap_link",
                        osm_id=f"gap:{key[0]}-{key[1]}",
                    )
                    offered.append((key, "join"))
                    continue
                G.add_edge(
                    a,
                    b,
                    length_m=d,
                    cost=rules.crossings.cost("unmapped", d),
                    surface="crossing",
                    highway="crossing_unmapped",
                    osm_id=f"crossing:unmapped:{key[0]}-{key[1]}",
                    crossing="unmapped",
                )
                offered.append((key, "unmapped"))
                mids.append(mid)
    return offered


DEAD_END_SNAP_M = 2.0  # a street end this close to another street meets it


def _join_dead_ends(G: nx.Graph, xy: list[tuple[float, float]]) -> int:
    """Split a street edge where another street's dead end stops on it, and join the two.

    Returns how many ends were joined. Ends are taken in node order and each splits the nearest
    edge it does not already touch, so the result is the same on every build.
    """
    ends = sorted(n for n in G.nodes if G.degree(n) == 1)
    if not ends:
        return 0
    from shapely.strtree import STRtree

    edges = sorted((min(u, v), max(u, v)) for u, v in G.edges)
    segs = [LineString([xy[u], xy[v]]) for u, v in edges]
    index = STRtree(segs)
    joined = 0
    split: set[tuple[int, int]] = set()
    for n in ends:
        if G.degree(n) != 1:
            continue
        pt = Point(xy[n])
        best: tuple[float, int] | None = None
        for i in index.query(pt.buffer(DEAD_END_SNAP_M)):
            u, v = edges[int(i)]
            if n in (u, v) or (u, v) in split or not G.has_edge(u, v):
                continue
            d = segs[int(i)].distance(pt)
            if d <= DEAD_END_SNAP_M and (best is None or d < best[0]):
                best = (d, int(i))
        if best is None:
            continue
        u, v = edges[best[1]]
        seg = segs[best[1]]
        at = seg.interpolate(seg.project(pt))
        data = dict(G[u][v])
        G.remove_edge(u, v)
        split.add((u, v))
        m = len(xy)
        xy.append((round(at.x, 2), round(at.y, 2)))
        G.add_node(m)
        total = max(seg.length, 1e-9)
        for a_, b_ in ((u, m), (m, v)):
            part = float(np.hypot(xy[a_][0] - xy[b_][0], xy[a_][1] - xy[b_][1]))
            G.add_edge(a_, b_, **{**data, "length_m": part, "cost": data["cost"] * part / total})
        gap = float(np.hypot(xy[n][0] - xy[m][0], xy[n][1] - xy[m][1]))
        per_m = data["cost"] / total
        G.add_edge(n, m, **{**data, "length_m": gap, "cost": gap * per_m, "osm_id": f"join:{n}"})
        joined += 1
    return joined


def _link_ducts(G: nx.Graph, xy: list[tuple[float, float]], rules: Rulebook) -> int:
    """Join existing ducts to the streets: every duct vertex that is not already on a street gets
    a short trench to the nearest street vertex within DUCT_LINK_M. Returns the links added."""
    duct_nodes, street_nodes = set(), set()
    for u, v, d in G.edges(data=True):
        (duct_nodes if d.get("highway") == "existing_duct" else street_nodes).update((u, v))
    lonely = sorted(duct_nodes - street_nodes)
    if not lonely or not street_nodes:
        return 0
    street_idx = np.fromiter(sorted(street_nodes), dtype=int)
    tree = cKDTree(np.asarray([xy[i] for i in street_idx], dtype=float))
    per_m = rules.surfaces.cost("pavement")
    added = 0
    for n in lonely:
        d, i = tree.query(xy[n])
        if d <= DUCT_LINK_M:
            m = int(street_idx[i])
            G.add_edge(
                n,
                m,
                length_m=float(d),
                cost=float(d) * per_m,
                surface="pavement",
                highway="duct_link",
                osm_id=f"link:{n}",
            )
            added += 1
    return added


def _offer_overhead(
    G: nx.Graph,
    xy: list[tuple[float, float]],
    poles: gpd.GeoDataFrame | None,
    buildings: gpd.GeoDataFrame | None,
    rules: Rulebook,
) -> list[Decision]:
    """Re-price street edges that need no trench: an edge whose ends and middle are each
    within half a span of an existing pole can be strung on the poles; an edge lined by
    building walls for at least `facade.min_cover` of its length can run on the facades.
    Each edge takes the cheapest of its trench, aerial and facade prices; routing then picks
    the cheapest route over them. The edge keeps its trench price in `trench_cost` so the
    saving can be reported."""
    import shapely
    from shapely import STRtree

    edges = [
        (u, v, d)
        for u, v, d in G.edges(data=True)
        if d.get("highway") not in ("existing_duct", "duct_link")
        and d["surface"] in SURFACES
        and d["surface"] not in ("existing_duct", *OVERHEAD)
    ]
    if not edges:
        return []
    offers: dict[str, dict[tuple[int, int], float]] = {"aerial": {}, "facade": {}}
    if rules.aerial.enabled and poles is not None:
        pts = np.column_stack([poles.geometry.x.values, poles.geometry.y.values])
        pts = pts[np.isfinite(pts).all(axis=1)]
        if len(pts):
            tree = cKDTree(pts)
            reach = rules.aerial.max_span_m / 2.0
            per_m = (
                rules.surfaces.cost("aerial") + rules.aerial.pole_attach / rules.aerial.max_span_m
            )
            for u, v, _ in edges:
                a, b = np.asarray(xy[u]), np.asarray(xy[v])
                d, _i = tree.query(np.vstack([a, b, (a + b) / 2.0]))
                if float(d.max()) <= reach:
                    offers["aerial"][(u, v)] = per_m
    if rules.facade.enabled and buildings is not None:
        allowed = set(rules.facade.highways)
        polys = [
            geom
            for geom in buildings.geometry
            if geom is not None and geom.geom_type in ("Polygon", "MultiPolygon")
        ]
        cand = [(u, v) for u, v, d in edges if d.get("highway") in allowed]
        if polys and cand:
            walls = shapely.buffer(np.asarray(polys, dtype=object), rules.facade.max_offset_m)
            tree = STRtree(walls)
            lines = np.asarray([LineString([xy[u], xy[v]]) for u, v in cand], dtype=object)
            hit_line, hit_wall = tree.query(lines, predicate="intersects")
            near: dict[int, list[int]] = {}
            for i, j in zip(hit_line.tolist(), hit_wall.tolist(), strict=True):
                near.setdefault(i, []).append(j)
            per_m = rules.surfaces.cost("facade")
            for i in sorted(near):
                line = lines[i]
                if line.length <= 0:
                    continue
                cover = line.intersection(shapely.union_all(walls[sorted(near[i])])).length
                if cover / line.length >= rules.facade.min_cover:
                    offers["facade"][cand[i]] = per_m
    decisions: list[Decision] = []
    choice: dict[tuple[int, int], tuple[str, float]] = {}
    for u, v, d in edges:
        best, price = d["surface"], d["cost"] / d["length_m"] if d["length_m"] else 0.0
        for kind in ("aerial", "facade"):
            p = offers[kind].get((u, v))
            if p is not None and p < price:
                best, price = kind, p
        if best != d["surface"]:
            choice[(u, v)] = (best, d["length_m"] * price)
    # A run of spans must save more than its two transitions into the ground cost; shorter
    # runs stay in the trench.
    dropped = 0
    for kind in ("aerial", "facade"):
        run = nx.Graph()
        run.add_edges_from(e for e, (k, _c) in choice.items() if k == kind)
        transition = getattr(rules, kind).transition
        for comp in sorted(nx.connected_components(run), key=min):
            part = run.subgraph(comp).edges
            keys = [(u, v) if (u, v) in choice else (v, u) for u, v in part]
            saving = sum(G[u][v]["cost"] - choice[(u, v)][1] for u, v in keys)
            if saving < 2 * transition:
                for e in keys:
                    del choice[e]
                dropped += 1
    taken = {"aerial": 0.0, "facade": 0.0}
    for (u, v), (kind, cost) in sorted(choice.items()):
        d = G[u][v]
        d["trench_cost"] = d["cost"]
        d["surface"] = kind
        d["cost"] = cost
        taken[kind] += d["length_m"]
    if taken["aerial"]:
        decisions.append(
            Decision(
                stage="twin",
                tool="build_twin",
                reason=f"{taken['aerial']:.0f} m of street have existing poles no more than "
                f"{rules.aerial.max_span_m / 2:g} m apart from the route (half a "
                f"{rules.aerial.max_span_m:g} m span); there the cable can be strung on the poles at "
                f"{rules.surfaces.cost('aerial'):g} per metre plus {rules.aerial.pole_attach:g} per pole, "
                "cheaper than a trench. Routing takes a span only where it is cheaper overall.",
                values={"aerial_m_offered": round(taken["aerial"], 1)},
                source="rulebook",
            )
        )
    if taken["facade"]:
        decisions.append(
            Decision(
                stage="twin",
                tool="build_twin",
                reason=f"{taken['facade']:.0f} m of street are lined by building walls (within "
                f"{rules.facade.max_offset_m:g} m, over at least {rules.facade.min_cover:.0%} of each "
                f"stretch); there the cable can run on the facades at {rules.surfaces.cost('facade'):g} "
                "per metre instead of a trench. Facade cabling needs the building owners' consent.",
                values={"facade_m_offered": round(taken["facade"], 1)},
                source="rulebook",
            )
        )
    if dropped:
        decisions.append(
            Decision(
                stage="twin",
                tool="build_twin",
                reason=f"{dropped} short run(s) of poles or walls were left in the trench: what they "
                "save over the trench does not pay for taking the cable up and back down "
                f"({rules.aerial.transition:g} per pole transition, {rules.facade.transition:g} per wall "
                "transition).",
                values={"short_runs": dropped},
                source="rulebook",
            )
        )
    return decisions


def _surface_for(highway: str, surface_tag, rules: Rulebook) -> str:
    return rules.highway_to_surface.get(highway, "pavement")


def _densify(pts: list[tuple[float, float]], max_len: float) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = [pts[0]]
    for a, b in zip(pts[:-1], pts[1:], strict=False):
        seg = float(np.hypot(b[0] - a[0], b[1] - a[1]))
        n = max(1, int(np.ceil(seg / max_len)))
        for k in range(1, n + 1):
            t = k / n
            out.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
    return out
