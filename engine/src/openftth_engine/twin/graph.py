"""The 2D digital twin: a street graph with trench costs, snapped homes and candidate sites.

Everything downstream (clustering, routing, drops, budgets) works on this graph in a local
metric CRS. Node ids are small integers; `twin.xy[node]` gives projected coordinates.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import geopandas as gpd
import networkx as nx
import numpy as np
from pyproj import CRS
from scipy.spatial import cKDTree
from shapely.geometry import LineString, Point

from ..results import Decision
from ..rules import Rulebook

DENSIFY_M = 25.0  # insert vertices so no edge is longer than this (better snapping/sites)


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


def build_twin(streets_wgs: gpd.GeoDataFrame, homes_wgs: gpd.GeoDataFrame, rules: Rulebook) -> Twin:
    """Project to a metric CRS, build the street graph, snap homes to it."""
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
    streets_wgs = streets_wgs[keep]
    if streets_wgs.empty:
        raise ValueError("no routable streets in the area; cannot build a twin")
    streets = streets_wgs.to_crs(crs).copy()
    homes = homes_wgs.to_crs(crs).copy() if not homes_wgs.empty else homes_wgs.copy()

    streets["surface"] = [
        _surface_for(str(h), s, rules)
        for h, s in zip(
            streets["highway"], streets.get("surface", [None] * len(streets)), strict=False
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
