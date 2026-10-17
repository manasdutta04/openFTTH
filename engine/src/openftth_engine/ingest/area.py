"""Design-area polygon loading."""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
from shapely.geometry import MultiPolygon, Polygon
from shapely.ops import unary_union


def load_area(path: str | Path) -> Polygon | MultiPolygon:
    """Load a polygon (GeoJSON/GeoPackage/Shapefile) in EPSG:4326 and union its parts."""
    gdf = gpd.read_file(path)
    if gdf.crs is not None and gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(4326)
    geom = unary_union(gdf.geometry.values)
    if geom.geom_type not in ("Polygon", "MultiPolygon"):
        raise ValueError(f"area must be a polygon, got {geom.geom_type}")
    return geom
