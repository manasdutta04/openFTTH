"""Quick PNG map of a design, for the CLI and the deliverables."""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from ..interchange import Design  # noqa: E402
from ..twin import Twin  # noqa: E402

COLORS = {
    "street": "#b8b8b8",
    "building": "#e6e2d8",
    "feeder": "#8e24aa",
    "distribution": "#1e88e5",
    "drop": "#43a047",
    "home": "#2e7d32",
    "fat": "#fb8c00",
    "fdh": "#d81b60",
    "pop": "#000000",
    "chamber": "#6d4c41",
}


def render_map(
    design: Design, twin: Twin, out_path: str | Path, title: str = "openFTTH design"
) -> Path:
    out_path = Path(out_path)
    fig, ax = plt.subplots(figsize=(14, 12), dpi=110)
    ax.set_aspect("equal")
    ax.set_axis_off()

    for geom in twin.streets.geometry:
        xs, ys = geom.xy
        ax.plot(xs, ys, color=COLORS["street"], linewidth=0.8, zorder=1)

    proj = _project(design, twin)
    for f in proj.get("building", []):
        g = f["geom"]
        polys = [g] if g.geom_type == "Polygon" else list(g.geoms)
        for p in polys:
            xs, ys = p.exterior.xy
            ax.fill(xs, ys, color=COLORS["building"], linewidth=0, zorder=2)

    for kind, lw in (("drop", 0.6), ("distribution", 1.8), ("feeder", 3.0)):
        for f in proj.get(kind, []):
            xs, ys = f["geom"].xy
            ax.plot(xs, ys, color=COLORS[kind], linewidth=lw, zorder=3 if kind == "drop" else 4)

    _scatter(ax, proj.get("home", []), COLORS["home"], 6, "o", 5, "homes")
    _scatter(ax, proj.get("chamber", []), COLORS["chamber"], 14, "s", 6, "chambers")
    _scatter(ax, proj.get("fat", []), COLORS["fat"], 60, "^", 7, "FAT")
    _scatter(ax, proj.get("fdh", []), COLORS["fdh"], 140, "s", 8, "FDH")
    _scatter(ax, proj.get("pop", []), COLORS["pop"], 260, "*", 9, "POP")

    ax.set_title(title)
    ax.legend(loc="lower left", fontsize=8, frameon=True)
    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path)
    plt.close(fig)
    return out_path


def _scatter(ax, feats, color, size, marker, z, label):
    if not feats:
        return
    xs = [f["geom"].x for f in feats]
    ys = [f["geom"].y for f in feats]
    ax.scatter(
        xs,
        ys,
        s=size,
        c=color,
        marker=marker,
        zorder=z,
        label=f"{label} ({len(feats)})",
        edgecolors="white",
        linewidths=0.4,
    )


def _project(design: Design, twin: Twin) -> dict[str, list[dict]]:
    import geopandas as gpd

    out: dict[str, list[dict]] = {}
    feats = list(design)
    if not feats:
        return out
    gdf = gpd.GeoDataFrame(
        {"kind": [f.kind or f.element_type for f in feats]},
        geometry=[f.geometry for f in feats],
        crs=4326,
    ).to_crs(twin.crs)
    for kind, geom in zip(gdf["kind"], gdf.geometry, strict=True):
        out.setdefault(kind, []).append({"geom": geom})
    return out
