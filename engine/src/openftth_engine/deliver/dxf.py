"""A minimal DXF writer (AutoCAD R12 ASCII, `AC1009`): layers, polylines, circles and text in
model space. R12 is the DXF dialect every CAD and GIS program reads (LibreCAD, QGIS, AutoCAD,
BricsCAD), and it needs no object handles, so the output is plain and byte-stable."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class DxfDrawing:
    """Entities by layer. Coordinates are in the drawing's units (metres in a projected CRS)."""

    layers: dict[str, int] = field(default_factory=dict)  # name → AutoCAD colour index
    _body: list[str] = field(default_factory=list)
    _min: list[float] = field(default_factory=lambda: [float("inf"), float("inf")])
    _max: list[float] = field(default_factory=lambda: [float("-inf"), float("-inf")])

    def layer(self, name: str, color: int) -> None:
        self.layers.setdefault(_name(name), color)

    def polyline(self, layer: str, pts: list[tuple[float, float]], closed: bool = False) -> None:
        if len(pts) < 2:
            return
        lay = _name(layer)
        self._pairs(0, "POLYLINE", 8, lay, 66, 1, 10, 0.0, 20, 0.0, 30, 0.0, 70, 1 if closed else 0)
        for x, y in pts:
            self._grow(x, y)
            self._pairs(0, "VERTEX", 8, lay, 10, x, 20, y, 30, 0.0)
        self._pairs(0, "SEQEND", 8, lay)

    def circle(self, layer: str, x: float, y: float, r: float) -> None:
        self._grow(x, y)
        self._pairs(0, "CIRCLE", 8, _name(layer), 10, x, 20, y, 30, 0.0, 40, r)

    def text(self, layer: str, x: float, y: float, height: float, value: str) -> None:
        self._grow(x, y)
        self._pairs(0, "TEXT", 8, _name(layer), 10, x, 20, y, 30, 0.0, 40, height, 1, _text(value))

    def write(self, path: str | Path) -> Path:
        path = Path(path)
        lo = [0.0 if v == float("inf") else v for v in self._min]
        hi = [0.0 if v == float("-inf") else v for v in self._max]
        out: list[str] = []

        def pairs(*items: object) -> None:
            out.extend(_pair(items[i], items[i + 1]) for i in range(0, len(items), 2))

        pairs(0, "SECTION", 2, "HEADER")
        pairs(9, "$ACADVER", 1, "AC1009")
        pairs(9, "$DWGCODEPAGE", 3, "ANSI_1252")
        pairs(9, "$INSBASE", 10, 0.0, 20, 0.0, 30, 0.0)
        pairs(9, "$EXTMIN", 10, lo[0], 20, lo[1], 30, 0.0)
        pairs(9, "$EXTMAX", 10, hi[0], 20, hi[1], 30, 0.0)
        pairs(0, "ENDSEC")
        pairs(0, "SECTION", 2, "TABLES")
        pairs(0, "TABLE", 2, "LTYPE", 70, 1)
        pairs(0, "LTYPE", 2, "CONTINUOUS", 70, 0, 3, "Solid line", 72, 65, 73, 0, 40, 0.0)
        pairs(0, "ENDTAB")
        pairs(0, "TABLE", 2, "LAYER", 70, len(self.layers) + 1)
        pairs(0, "LAYER", 2, "0", 70, 0, 62, 7, 6, "CONTINUOUS")
        for name, color in sorted(self.layers.items()):
            pairs(0, "LAYER", 2, name, 70, 0, 62, color, 6, "CONTINUOUS")
        pairs(0, "ENDTAB")
        pairs(0, "ENDSEC")
        pairs(0, "SECTION", 2, "ENTITIES")
        out.extend(self._body)
        pairs(0, "ENDSEC")
        pairs(0, "EOF")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes("".join(out).encode("cp1252", errors="replace"))
        return path

    def _pairs(self, *items: object) -> None:
        self._body.extend(_pair(items[i], items[i + 1]) for i in range(0, len(items), 2))

    def _grow(self, x: float, y: float) -> None:
        self._min[0], self._min[1] = min(self._min[0], x), min(self._min[1], y)
        self._max[0], self._max[1] = max(self._max[0], x), max(self._max[1], y)


def _pair(code: object, value: object) -> str:
    if isinstance(value, float):
        value = f"{value:.3f}"
    return f"{int(code):>3}\n{value}\n"


def _name(layer: str) -> str:
    """R12 layer names: upper case letters, digits, `-` and `_` only."""
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in layer.upper())[:31] or "0"


def _text(value: str) -> str:
    return " ".join(str(value).split())[:250]
