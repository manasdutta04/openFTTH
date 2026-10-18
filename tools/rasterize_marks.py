"""Rasterise the engine marks (monochrome SVG icons) into alpha masks the desktop app embeds.

The app paints the masks tinted with the theme's text colour, so only coverage matters. No
image library is needed: paths are flattened, fills use a scanline with the SVG fill rule and
strokes use the distance to the polyline. Run from the repo root:

    python tools/rasterize_marks.py

Input:  desktop/crates/openftth-app/assets/engines/svg/<kind>.svg
Output: desktop/crates/openftth-app/assets/engines/<kind>.a8  (SIZE x SIZE bytes, row-major)
"""

from __future__ import annotations

import math
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

SIZE = 96  # output pixels per side
SS = 4  # supersamples per pixel per axis
MARGIN = 0.04  # of the square, so marks do not touch the tile edge

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "desktop" / "crates" / "openftth-app" / "assets" / "engines"

Point = tuple[float, float]


# ---- path data ---------------------------------------------------------------------------------

_TOKEN = re.compile(r"[MmZzLlHhVvCcSsQqTtAa]|[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


def flatten_path(d: str) -> list[list[Point]]:
    """SVG path data -> list of subpaths, each a polyline (curves flattened)."""
    tokens = _TOKEN.findall(d)
    i = 0
    cmd = ""
    cur: Point = (0.0, 0.0)
    start: Point = (0.0, 0.0)
    last_ctrl: Point | None = None
    last_cmd = ""
    subpaths: list[list[Point]] = []
    poly: list[Point] = []

    def num() -> float:
        nonlocal i
        v = float(tokens[i])
        i += 1
        return v

    def flag() -> float:
        """Arc flags are single characters and may run into the next number ("013.046")."""
        nonlocal i
        t = tokens[i]
        if len(t) > 1 and t[0] in "01":
            tokens[i] = t[1:]
            return float(t[0])
        i += 1
        return float(t)

    def begin(p: Point) -> None:
        nonlocal poly, start
        if poly:
            subpaths.append(poly)
        poly = [p]
        start = p

    while i < len(tokens):
        t = tokens[i]
        if t.isalpha():
            cmd = t
            i += 1
            if cmd in "Zz":
                if poly:
                    poly.append(start)
                    subpaths.append(poly)
                    poly = []
                cur = start
                last_cmd = cmd
                continue
        rel = cmd.islower()
        c = cmd.upper()

        def pt(x: float, y: float, cur: Point = cur, rel: bool = rel) -> Point:
            return (cur[0] + x, cur[1] + y) if rel else (x, y)

        if c == "M":
            p = pt(num(), num())
            begin(p)
            cur = p
            cmd = "l" if rel else "L"  # subsequent pairs are line-tos
            last_ctrl = None
        elif c == "L":
            p = pt(num(), num())
            poly.append(p)
            cur = p
            last_ctrl = None
        elif c == "H":
            x = num()
            p = (cur[0] + x if rel else x, cur[1])
            poly.append(p)
            cur = p
            last_ctrl = None
        elif c == "V":
            y = num()
            p = (cur[0], cur[1] + y if rel else y)
            poly.append(p)
            cur = p
            last_ctrl = None
        elif c in "CS":
            if c == "C":
                c1 = pt(num(), num())
            else:
                c1 = reflect(cur, last_ctrl) if last_cmd.upper() in "CS" else cur
            c2 = pt(num(), num())
            p = pt(num(), num())
            poly.extend(cubic(cur, c1, c2, p))
            last_ctrl = c2
            cur = p
        elif c in "QT":
            if c == "Q":
                c1 = pt(num(), num())
            else:
                c1 = reflect(cur, last_ctrl) if last_cmd.upper() in "QT" else cur
            p = pt(num(), num())
            poly.extend(quad(cur, c1, p))
            last_ctrl = c1
            cur = p
        elif c == "A":
            rx, ry, rot = num(), num(), num()
            large, sweep = flag(), flag()
            p = pt(num(), num())
            poly.extend(arc(cur, rx, ry, rot, large != 0, sweep != 0, p))
            cur = p
            last_ctrl = None
        else:
            raise ValueError(f"unsupported path command {cmd}")
        last_cmd = cmd
    if poly:
        subpaths.append(poly)
    return subpaths


def reflect(cur: Point, ctrl: Point | None) -> Point:
    if ctrl is None:
        return cur
    return (2 * cur[0] - ctrl[0], 2 * cur[1] - ctrl[1])


def cubic(p0: Point, p1: Point, p2: Point, p3: Point, n: int = 24) -> list[Point]:
    out = []
    for k in range(1, n + 1):
        t = k / n
        u = 1 - t
        x = u * u * u * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t * t * t * p3[0]
        y = u * u * u * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t * t * t * p3[1]
        out.append((x, y))
    return out


def quad(p0: Point, p1: Point, p2: Point, n: int = 16) -> list[Point]:
    out = []
    for k in range(1, n + 1):
        t = k / n
        u = 1 - t
        out.append(
            (
                u * u * p0[0] + 2 * u * t * p1[0] + t * t * p2[0],
                u * u * p0[1] + 2 * u * t * p1[1] + t * t * p2[1],
            )
        )
    return out


def arc(
    p0: Point,
    rx: float,
    ry: float,
    rot_deg: float,
    large: bool,
    sweep: bool,
    p1: Point,
    n: int = 32,
) -> list[Point]:
    """Endpoint to centre parametrisation (SVG implementation notes, appendix B)."""
    if rx == 0 or ry == 0 or p0 == p1:
        return [p1]
    rot = math.radians(rot_deg)
    cos_r, sin_r = math.cos(rot), math.sin(rot)
    dx, dy = (p0[0] - p1[0]) / 2, (p0[1] - p1[1]) / 2
    x1 = cos_r * dx + sin_r * dy
    y1 = -sin_r * dx + cos_r * dy
    rx, ry = abs(rx), abs(ry)
    lam = (x1 * x1) / (rx * rx) + (y1 * y1) / (ry * ry)
    if lam > 1:
        rx *= math.sqrt(lam)
        ry *= math.sqrt(lam)
    num = rx * rx * ry * ry - rx * rx * y1 * y1 - ry * ry * x1 * x1
    den = rx * rx * y1 * y1 + ry * ry * x1 * x1
    coef = math.sqrt(max(0.0, num / den)) if den else 0.0
    if large == sweep:
        coef = -coef
    cx1 = coef * rx * y1 / ry
    cy1 = -coef * ry * x1 / rx
    cx = cos_r * cx1 - sin_r * cy1 + (p0[0] + p1[0]) / 2
    cy = sin_r * cx1 + cos_r * cy1 + (p0[1] + p1[1]) / 2

    def ang(ux: float, uy: float, vx: float, vy: float) -> float:
        d = math.hypot(ux, uy) * math.hypot(vx, vy)
        a = math.acos(max(-1.0, min(1.0, (ux * vx + uy * vy) / d)))
        return -a if ux * vy - uy * vx < 0 else a

    t1 = ang(1, 0, (x1 - cx1) / rx, (y1 - cy1) / ry)
    dt = ang((x1 - cx1) / rx, (y1 - cy1) / ry, (-x1 - cx1) / rx, (-y1 - cy1) / ry)
    if not sweep and dt > 0:
        dt -= 2 * math.pi
    elif sweep and dt < 0:
        dt += 2 * math.pi
    out = []
    for k in range(1, n + 1):
        t = t1 + dt * k / n
        x = rx * math.cos(t)
        y = ry * math.sin(t)
        out.append((cos_r * x - sin_r * y + cx, sin_r * x + cos_r * y + cy))
    return out


# ---- scene --------------------------------------------------------------------------------------


def parse_svg(path: Path) -> tuple[tuple[float, float, float, float], list[dict]]:
    tree = ET.parse(path)
    root = tree.getroot()
    vb = [float(v) for v in root.get("viewBox", "0 0 24 24").replace(",", " ").split()]
    shapes: list[dict] = []

    def walk(el: ET.Element, inherited: dict) -> None:
        attrs = dict(inherited)
        for k in ("fill", "stroke", "stroke-width", "fill-rule"):
            if el.get(k) is not None:
                attrs[k] = el.get(k)
        tag = el.tag.split("}")[-1]
        if tag in ("defs", "clipPath", "title", "mask"):
            return
        if tag == "path" and el.get("d"):
            shapes.append({"subpaths": flatten_path(el.get("d", "")), **attrs})
        elif tag == "circle":
            cx, cy, r = float(el.get("cx", 0)), float(el.get("cy", 0)), float(el.get("r", 0))
            pts = [
                (cx + r * math.cos(2 * math.pi * k / 64), cy + r * math.sin(2 * math.pi * k / 64))
                for k in range(65)
            ]
            shapes.append({"subpaths": [pts], **attrs})
        elif tag == "rect":
            x, y = float(el.get("x", 0)), float(el.get("y", 0))
            w, h = float(el.get("width", 0)), float(el.get("height", 0))
            shapes.append(
                {"subpaths": [[(x, y), (x + w, y), (x + w, y + h), (x, y + h), (x, y)]], **attrs}
            )
        for child in el:
            walk(child, attrs)

    walk(root, {"fill": "black", "stroke": "none", "stroke-width": "1", "fill-rule": "nonzero"})
    return (vb[0], vb[1], vb[2], vb[3]), shapes


# ---- rasterisation ----------------------------------------------------------------------------


def rasterise(viewbox: tuple[float, float, float, float], shapes: list[dict]) -> bytes:
    vx, vy, vw, vh = viewbox
    n = SIZE * SS
    usable = 1.0 - 2 * MARGIN
    scale = n * usable / max(vw, vh)
    ox = (n - vw * scale) / 2 - vx * scale
    oy = (n - vh * scale) / 2 - vy * scale

    def to_px(p: Point) -> Point:
        return (p[0] * scale + ox, p[1] * scale + oy)

    cover = bytearray(n * n)  # 0/1 per sample
    for shape in shapes:
        polys = [[to_px(p) for p in sp] for sp in shape["subpaths"] if len(sp) >= 2]
        if shape.get("fill", "black") != "none":
            fill_polys(cover, n, polys, shape.get("fill-rule", "nonzero") == "evenodd")
        stroke = shape.get("stroke", "none")
        width = float(shape.get("stroke-width", "1"))
        if stroke != "none" and width > 0:
            stroke_polys(cover, n, polys, width * scale / 2)

    out = bytearray(SIZE * SIZE)
    total = SS * SS
    for py in range(SIZE):
        for px in range(SIZE):
            s = 0
            for sy in range(SS):
                row = (py * SS + sy) * n + px * SS
                s += sum(cover[row : row + SS])
            out[py * SIZE + px] = round(255 * s / total)
    return bytes(out)


def fill_polys(cover: bytearray, n: int, polys: list[list[Point]], evenodd: bool) -> None:
    edges = []
    for poly in polys:
        pts = poly if poly[0] == poly[-1] else poly + [poly[0]]
        for a, b in zip(pts, pts[1:], strict=False):
            if a[1] != b[1]:
                edges.append((a, b))
    if not edges:
        return
    ymin = max(0, int(min(min(a[1], b[1]) for a, b in edges)))
    ymax = min(n - 1, int(max(max(a[1], b[1]) for a, b in edges)) + 1)
    for row in range(ymin, ymax + 1):
        y = row + 0.5
        xs: list[tuple[float, int]] = []
        for a, b in edges:
            (x0, y0), (x1, y1) = a, b
            if (y0 <= y < y1) or (y1 <= y < y0):
                t = (y - y0) / (y1 - y0)
                xs.append((x0 + t * (x1 - x0), 1 if y1 > y0 else -1))
        if not xs:
            continue
        xs.sort()
        winding = 0
        for (x, d), (x_next, _) in zip(xs, xs[1:], strict=False):
            winding += d
            inside = (winding % 2 == 1) if evenodd else (winding != 0)
            if inside:
                lo = max(0, int(math.ceil(x - 0.5)))
                hi = min(n - 1, int(math.floor(x_next - 0.5)))
                if hi >= lo:
                    base = row * n
                    cover[base + lo : base + hi + 1] = b"\x01" * (hi - lo + 1)


def stroke_polys(cover: bytearray, n: int, polys: list[list[Point]], half: float) -> None:
    h2 = half * half
    for poly in polys:
        for (x0, y0), (x1, y1) in zip(poly, poly[1:], strict=False):
            dx, dy = x1 - x0, y1 - y0
            len2 = dx * dx + dy * dy
            bx0 = max(0, int(min(x0, x1) - half) - 1)
            bx1 = min(n - 1, int(max(x0, x1) + half) + 1)
            by0 = max(0, int(min(y0, y1) - half) - 1)
            by1 = min(n - 1, int(max(y0, y1) + half) + 1)
            for row in range(by0, by1 + 1):
                y = row + 0.5
                base = row * n
                for col in range(bx0, bx1 + 1):
                    if cover[base + col]:
                        continue
                    x = col + 0.5
                    t = ((x - x0) * dx + (y - y0) * dy) / len2 if len2 else 0.0
                    t = max(0.0, min(1.0, t))
                    ex, ey = x0 + t * dx - x, y0 + t * dy - y
                    if ex * ex + ey * ey <= h2:
                        cover[base + col] = 1


def main() -> int:
    svgs = sorted((ASSETS / "svg").glob("*.svg"))
    if not svgs:
        print(f"no SVGs under {ASSETS / 'svg'}", file=sys.stderr)
        return 1
    for svg in svgs:
        viewbox, shapes = parse_svg(svg)
        mask = rasterise(viewbox, shapes)
        out = ASSETS / f"{svg.stem}.a8"
        out.write_bytes(mask)
        filled = sum(1 for b in mask if b) / len(mask)
        print(f"{svg.stem:22} {len(shapes)} shapes -> {out.name} ({filled:.0%} covered)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
