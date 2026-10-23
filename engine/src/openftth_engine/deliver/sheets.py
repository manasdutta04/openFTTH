"""Plan sheets: a printable atlas of the design (PDF) and the whole design as a CAD drawing (DXF).

The atlas starts with a key sheet (the whole area and the sheet grid), then one sheet per grid
cell that holds part of the network, at a fixed scale on A3 or A1 paper. Every sheet has a
legend, a scale bar, a north arrow and a title block. Labels that would overlap an earlier label
are left out and counted. Nothing in the output depends on the clock, so the same design gives
byte-identical files.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import geopandas as gpd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.backends.backend_pdf import PdfPages  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patches import Polygon as PolygonPatch  # noqa: E402
from matplotlib.patches import Rectangle  # noqa: E402

from ..interchange import Design  # noqa: E402
from ..twin import Twin  # noqa: E402
from .dxf import DxfDrawing  # noqa: E402

PAPER_MM = {"A3": (420.0, 297.0), "A1": (841.0, 594.0)}
MARGIN_MM = 10.0
SIDE_MM = {"A3": 82.0, "A1": 120.0}
NICE_SCALES = (500, 1000, 1250, 2000, 2500, 5000, 10000, 20000, 25000, 50000, 100000)
PT_MM = 25.4 / 72.0

#: kind → (colour, line width pt or marker size, marker, legend label, DXF colour index)
STYLE: dict[str, tuple[str, float, str | None, str, int]] = {
    "feeder": ("#8e24aa", 2.4, None, "Feeder cable", 6),
    "distribution": ("#1e88e5", 1.5, None, "Distribution cable", 5),
    "drop": ("#43a047", 0.5, None, "Drop cable", 3),
    "crossing": ("#00897b", 2.4, None, "Crossing (bore)", 4),
    "pop": ("#000000", 9.0, "*", "POP", 7),
    "fdh": ("#d81b60", 7.0, "s", "Cabinet (FDH)", 1),
    "fat": ("#fb8c00", 6.0, "^", "FAT", 30),
    "chamber": ("#6d4c41", 3.5, "s", "Chamber / closure", 34),
    "home": ("#2e7d32", 2.0, "o", "Home", 3),
}
LINE_KINDS = ("drop", "distribution", "feeder", "crossing")
POINT_KINDS = ("home", "chamber", "fat", "fdh", "pop")
LABEL_PT = {"pop": 7.0, "fdh": 6.5, "fat": 5.5, "chamber": 4.5, "cable": 4.5}


@dataclass
class Item:
    kind: str
    name: str
    geom: Any  # projected shapely geometry
    props: dict[str, Any]


def plan_sheets(
    design: Design,
    twin: Twin,
    pdf_path: str | Path,
    dxf_path: str | Path,
    info: dict[str, Any],
    scale: int = 1000,
    paper: str = "A3",
) -> dict[str, Any]:
    """Write the atlas and the drawing; returns what was written (sheets, labels, paths)."""
    if paper not in PAPER_MM:
        raise ValueError(f"paper must be one of {', '.join(PAPER_MM)}")
    if scale <= 0:
        raise ValueError("scale must be positive")
    items = _items(design, twin)
    net = [i for i in items if i.kind in LINE_KINDS + POINT_KINDS]
    if not net:
        raise ValueError("the design has no network yet; design it first")
    streets = [g for g in twin.streets.geometry if g is not None]
    buildings = [i for i in items if i.kind == "building"]
    zones = [i for i in items if i.kind == "zone"]

    W, H = PAPER_MM[paper]
    frame_w, frame_h = W - 2 * MARGIN_MM - SIDE_MM[paper] - 5.0, H - 2 * MARGIN_MM
    gw, gh = frame_w * scale / 1000.0, frame_h * scale / 1000.0  # ground metres per sheet
    minx, miny, maxx, maxy = _bounds(net)
    cols = max(1, math.ceil((maxx - minx) / gw))
    rows = max(1, math.ceil((maxy - miny) / gh))
    # Centre the grid on the network.
    x0 = (minx + maxx) / 2 - cols * gw / 2
    y0 = (miny + maxy) / 2 - rows * gh / 2
    cells: list[tuple[int, float, float]] = []
    for r in range(rows):  # top row first, left to right
        for c in range(cols):
            cx, cy = x0 + c * gw, y0 + (rows - 1 - r) * gh
            if any(_touches(i.geom, cx, cy, cx + gw, cy + gh) for i in net):
                cells.append((len(cells) + 1, cx, cy))

    placed = dropped = 0
    pdf_path, dxf_path = Path(pdf_path), Path(dxf_path)
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    meta = {"Creator": "openFTTH", "Producer": "openFTTH", "CreationDate": None}
    total = len(cells) + 1
    with PdfPages(pdf_path, metadata=meta) as pdf:
        # Key sheet: the whole area at a scale that fits, with the sheet grid.
        span = max((x0 + cols * gw - x0) / frame_w, (rows * gh) / frame_h) * 1000.0
        key_scale = next((s for s in NICE_SCALES if s >= span), NICE_SCALES[-1])
        kw, kh = frame_w * key_scale / 1000.0, frame_h * key_scale / 1000.0
        kx = (minx + maxx) / 2 - kw / 2
        ky = (miny + maxy) / 2 - kh / 2
        fig, ax = _page(paper, frame_w, frame_h)
        _draw(ax, streets, buildings, zones, net, key_scale, detail=False)
        for n, cx, cy in cells:
            ax.add_patch(
                Rectangle((cx, cy), gw, gh, fill=False, lw=0.8, ec="#c62828", ls="--", zorder=20)
            )
            ax.text(
                cx + gw / 2,
                cy + gh / 2,
                str(n),
                ha="center",
                va="center",
                fontsize=14,
                color="#c62828",
                alpha=0.85,
                zorder=21,
                fontweight="bold",
            )
        ax.set_xlim(kx, kx + kw)
        ax.set_ylim(ky, ky + kh)
        _furniture(fig, ax, paper, key_scale, info, "Key plan", 1, total, twin, kx, ky, kw)
        pdf.savefig(fig)
        plt.close(fig)
        for n, cx, cy in cells:
            fig, ax = _page(paper, frame_w, frame_h)
            box = (cx, cy, cx + gw, cy + gh)
            vis = [i for i in net if _touches(i.geom, *box)]
            _draw(ax, streets, buildings, zones, vis, scale, detail=True)
            p, d = _labels(ax, vis, scale, box)
            placed, dropped = placed + p, dropped + d
            ax.set_xlim(cx, cx + gw)
            ax.set_ylim(cy, cy + gh)
            _furniture(fig, ax, paper, scale, info, f"Sheet {n}", n + 1, total, twin, cx, cy, gw)
            pdf.savefig(fig)
            plt.close(fig)
    _write_dxf(dxf_path, streets, buildings, zones, net, cells, gw, gh)
    return {
        "pdf": pdf_path.name,
        "dxf": dxf_path.name,
        "paper": paper,
        "scale": scale,
        "sheets": total,
        "key_scale": key_scale,
        "grid": [cols, rows],
        "labels_placed": placed,
        "labels_dropped": dropped,
        "crs": twin.crs.to_string(),
    }


# ---- geometry ------------------------------------------------------------------------------


def _items(design: Design, twin: Twin) -> list[Item]:
    feats = [f for f in design if f.geometry is not None]
    if not feats:
        return []
    gdf = gpd.GeoDataFrame(geometry=[f.geometry for f in feats], crs=4326).to_crs(twin.crs)
    out = []
    for f, g in zip(feats, gdf.geometry, strict=True):
        kind = f.kind or f.element_type
        out.append(Item(kind=kind, name=_label(f), geom=g, props=dict(f.props)))
    return out


def _label(f: Any) -> str:
    for key in ("name", "manhole_id", "site_name"):
        v = f.props.get(key)
        if v:
            return str(v)
    return ""


def _bounds(items: list[Item]) -> tuple[float, float, float, float]:
    b = [i.geom.bounds for i in items if not i.geom.is_empty]
    return (
        min(x[0] for x in b),
        min(x[1] for x in b),
        max(x[2] for x in b),
        max(x[3] for x in b),
    )


def _touches(g: Any, x0: float, y0: float, x1: float, y1: float) -> bool:
    a, b, c, d = g.bounds
    return not (c < x0 or a > x1 or d < y0 or b > y1)


# ---- drawing -------------------------------------------------------------------------------


def _page(paper: str, frame_w: float, frame_h: float):
    W, H = PAPER_MM[paper]
    fig = plt.figure(figsize=(W / 25.4, H / 25.4))
    ax = fig.add_axes((MARGIN_MM / W, MARGIN_MM / H, frame_w / W, frame_h / H))
    ax.set_aspect("equal", adjustable="box")
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_linewidth(0.8)
    return fig, ax


def _draw(ax, streets, buildings, zones, items: list[Item], scale: int, detail: bool) -> None:
    thin = 1000.0 / scale  # line widths shrink on smaller-scale sheets
    for g in streets:
        for part in getattr(g, "geoms", [g]):
            xs, ys = part.xy
            ax.plot(xs, ys, color="#bdbdbd", lw=max(0.3, 0.6 * thin), zorder=1)
    if detail:
        for b in buildings:
            for poly in getattr(b.geom, "geoms", [b.geom]):
                if poly.geom_type == "Polygon":
                    ax.add_patch(
                        PolygonPatch(
                            list(poly.exterior.coords),
                            closed=True,
                            fc="#eeeae0",
                            ec="#d6d0c2",
                            lw=0.3,
                            zorder=2,
                        )
                    )
    for z in zones:
        for poly in getattr(z.geom, "geoms", [z.geom]):
            if poly.geom_type == "Polygon":
                ax.add_patch(
                    PolygonPatch(
                        list(poly.exterior.coords),
                        closed=True,
                        fill=False,
                        ec="#c62828",
                        lw=0.8,
                        ls=(0, (4, 2)),
                        zorder=3,
                    )
                )
    for kind in LINE_KINDS:
        if kind == "drop" and not detail:
            continue
        color, lw, _m, _l, _c = STYLE[kind]
        for i in items:
            if i.kind != kind:
                continue
            for part in getattr(i.geom, "geoms", [i.geom]):
                if part.geom_type != "LineString":
                    continue
                xs, ys = part.xy
                ax.plot(
                    xs,
                    ys,
                    color=color,
                    lw=lw * (1.0 if detail else 0.6),
                    zorder=4,
                    solid_capstyle="round",
                )
    for z, kind in enumerate(POINT_KINDS):
        if kind in ("home", "chamber") and not detail:
            continue
        color, size, marker, _l, _c = STYLE[kind]
        pts = [i.geom for i in items if i.kind == kind and i.geom.geom_type == "Point"]
        if pts:
            ax.scatter(
                [p.x for p in pts],
                [p.y for p in pts],
                s=size**2 * (1.0 if detail or kind in ("pop", "fdh") else 0.5),
                c=color,
                marker=marker,
                edgecolors="white",
                linewidths=0.4,
                zorder=6 + z,
            )


def _labels(ax, items: list[Item], scale: int, box) -> tuple[int, int]:
    """Names of sites and fibre counts of cables, largest first; one that would overlap an
    earlier label (or leave the sheet) is skipped."""
    m_per_pt = PT_MM * scale / 1000.0
    x0, y0, x1, y1 = box
    gw = x1 - x0
    # Keep clear of the scale bar (bottom left) and the north arrow (top right).
    taken: list[tuple[float, float, float, float]] = [
        (x0, y0, x0 + gw * 0.25, y0 + gw * 0.05),
        (x1 - gw * 0.07, y1 - (y1 - y0) * 0.14, x1, y1),
    ]
    sites = [
        (i.geom.x, i.geom.y)
        for i in items
        if i.kind in ("pop", "fdh", "fat") and i.geom.geom_type == "Point"
    ]
    placed = dropped = 0
    queue: list[tuple[int, str, float, float, str]] = []
    rank = {"pop": 0, "fdh": 1, "fat": 2, "cable": 3, "chamber": 4}
    for i in items:
        if i.kind in ("pop", "fdh", "fat", "chamber") and i.name and i.geom.geom_type == "Point":
            if i.kind == "chamber" and any(
                abs(i.geom.x - x) < 1.0 and abs(i.geom.y - y) < 1.0 for x, y in sites
            ):
                continue  # the chamber under a FAT or cabinet: the site's label names the spot
            queue.append((rank[i.kind], i.name, i.geom.x, i.geom.y, i.kind))
        elif i.kind in ("feeder", "distribution") and i.props.get("fiber_count"):
            line = i.geom
            if line.geom_type == "LineString" and line.length > 40 * m_per_pt:
                mid = line.interpolate(0.5, normalized=True)
                n = int(i.props.get("cable_count", 1))
                prefix = f"{n}×" if n > 1 else ""
                text = f"{prefix}{int(i.props['fiber_count'])}F"
                queue.append((rank["cable"], text, mid.x, mid.y, "cable"))
    queue.sort(key=lambda q: (q[0], q[1], q[2], q[3]))
    for _r, text, x, y, kind in queue:
        h = LABEL_PT[kind] * m_per_pt
        w = 0.6 * h * len(text)
        gap = 2.5 * m_per_pt
        for dx, dy, ha, va in (
            (gap, gap, "left", "bottom"),
            (-gap, gap, "right", "bottom"),
            (gap, -gap, "left", "top"),
            (-gap, -gap, "right", "top"),
        ):
            lx = x + dx if ha == "left" else x + dx - w
            ly = y + dy if va == "bottom" else y + dy - h
            rect = (lx, ly, lx + w, ly + h)
            if rect[0] < x0 or rect[2] > x1 or rect[1] < y0 or rect[3] > y1:
                continue
            if any(_overlap(rect, t) for t in taken):
                continue
            taken.append(rect)
            ax.text(
                x + dx,
                y + dy,
                text,
                ha=ha,
                va=va,
                fontsize=LABEL_PT[kind],
                zorder=30,
                color="#212121" if kind != "cable" else "#1565c0",
                bbox={"boxstyle": "square,pad=0.1", "fc": "white", "ec": "none", "alpha": 0.8},
            )
            placed += 1
            break
        else:
            dropped += 1
    return placed, dropped


def _overlap(a: tuple[float, ...], b: tuple[float, ...]) -> bool:
    return not (a[2] < b[0] or a[0] > b[2] or a[3] < b[1] or a[1] > b[3])


def _furniture(fig, ax, paper, scale, info, title, page, total, twin, x0, y0, gw) -> None:
    """Scale bar and north arrow in the map frame; legend and title block beside it."""
    # Scale bar: a round length near a fifth of the frame width, in alternating blocks.
    target = gw / 5.0
    step = 10 ** math.floor(math.log10(target))
    length = max(m * step for m in (1, 2, 5) if m * step <= target)
    bx, by = x0 + gw * 0.03, y0 + gw * 0.025
    hgt = gw * 0.006
    for k in range(4):
        ax.add_patch(
            Rectangle(
                (bx + k * length / 4, by),
                length / 4,
                hgt,
                fc="black" if k % 2 == 0 else "white",
                ec="black",
                lw=0.5,
                zorder=40,
            )
        )
    ax.text(bx, by + hgt * 1.6, "0", fontsize=6, ha="center", zorder=40)
    ax.text(bx + length, by + hgt * 1.6, f"{length:g} m", fontsize=6, ha="center", zorder=40)
    # North arrow (grid north of the projection).
    ax.annotate(
        "N",
        xy=(0.965, 0.95),
        xytext=(0.965, 0.89),
        xycoords="axes fraction",
        ha="center",
        va="center",
        fontsize=9,
        fontweight="bold",
        arrowprops={"arrowstyle": "-|>", "lw": 1.2, "color": "black"},
        zorder=40,
    )
    W, H = PAPER_MM[paper]
    side = SIDE_MM[paper]
    left = (W - MARGIN_MM - side) / W
    legend_ax = fig.add_axes((left, 0.42, side / W, 0.5))
    legend_ax.set_axis_off()
    handles = []
    for kind in ("feeder", "distribution", "drop", "crossing"):
        color, lw, _m, label, _c = STYLE[kind]
        handles.append(Line2D([], [], color=color, lw=lw, label=label))
    for kind in ("pop", "fdh", "fat", "chamber", "home"):
        color, size, marker, label, _c = STYLE[kind]
        handles.append(
            Line2D(
                [],
                [],
                color=color,
                marker=marker,
                ls="",
                markersize=size,
                label=label,
                markeredgecolor="white",
            )
        )
    handles.append(Line2D([], [], color="#c62828", ls="--", lw=0.8, label="Zone / sheet edge"))
    legend_ax.legend(
        handles=handles,
        loc="upper left",
        fontsize=7,
        frameon=False,
        title="Legend",
        title_fontsize=8,
        alignment="left",
    )
    tb = fig.add_axes((left, MARGIN_MM / H, side / W, 0.36))
    tb.set_xticks([])
    tb.set_yticks([])
    rows = [
        ("Project", info.get("project", "")),
        ("Drawing", title),
        ("Sheet", f"{page} of {total}"),
        ("Scale", f"1:{scale:,} at {paper}"),
        ("Grid", twin.crs.to_string()),
        ("Rulebook", info.get("rulebook", "")),
        ("Design id", info.get("version", "") or "-"),
        ("Work order", info.get("work_order", "") or "-"),
        ("Status", info.get("status", "Draft: for review")),
        ("Drawn by", "openFTTH"),
    ]
    n = len(rows)
    for k, (key, value) in enumerate(rows):
        y = 1 - (k + 0.5) / n
        tb.text(0.04, y, key, fontsize=6, color="#616161", va="center", transform=tb.transAxes)
        tb.text(0.38, y, str(value)[:38], fontsize=7, va="center", transform=tb.transAxes)
        if k:
            tb.axhline(1 - k / n, lw=0.3, color="#9e9e9e")


# ---- DXF -----------------------------------------------------------------------------------


def _write_dxf(path, streets, buildings, zones, items: list[Item], cells, gw, gh) -> None:
    dwg = DxfDrawing()
    dwg.layer("STREETS", 8)
    dwg.layer("BUILDINGS", 9)
    dwg.layer("ZONES", 1)
    dwg.layer("LABELS", 7)
    dwg.layer("SHEETS", 8)
    for kind, (_c, _w, _m, _l, aci) in STYLE.items():
        dwg.layer(kind, aci)
    for g in streets:
        for part in getattr(g, "geoms", [g]):
            dwg.polyline("STREETS", list(part.coords))
    for b in buildings:
        for poly in getattr(b.geom, "geoms", [b.geom]):
            if poly.geom_type == "Polygon":
                dwg.polyline("BUILDINGS", list(poly.exterior.coords), closed=True)
    for z in zones:
        for poly in getattr(z.geom, "geoms", [z.geom]):
            if poly.geom_type == "Polygon":
                dwg.polyline("ZONES", list(poly.exterior.coords), closed=True)
    radius = {"pop": 3.0, "fdh": 2.0, "fat": 1.2, "chamber": 0.8, "home": 0.5}
    for i in items:
        if i.kind in LINE_KINDS:
            for part in getattr(i.geom, "geoms", [i.geom]):
                if part.geom_type == "LineString":
                    dwg.polyline(i.kind, list(part.coords))
        elif i.geom.geom_type == "Point":
            dwg.circle(i.kind, i.geom.x, i.geom.y, radius.get(i.kind, 0.5))
            if i.name and i.kind in ("pop", "fdh", "fat", "chamber"):
                dwg.text("LABELS", i.geom.x + 1.5, i.geom.y + 1.5, 1.5, i.name)
    for n, cx, cy in cells:
        dwg.polyline("SHEETS", [(cx, cy), (cx + gw, cy), (cx + gw, cy + gh), (cx, cy + gh)], True)
        dwg.text("SHEETS", cx + 2, cy + gh - 6, 4.0, f"Sheet {n}")
    dwg.write(path)
