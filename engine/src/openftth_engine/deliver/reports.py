"""Reports a contractor works from, built only from what the solvers recorded: the cable
schedule (every cable with its ends, size, length and the metres on each kind of route), the
port report (every cabinet, FAT and closure with its ports used and free) and the materials by
build phase."""

from __future__ import annotations

import csv
import io
from typing import Any

from ..interchange import Design
from ..rules import Rulebook
from .boq import Boq

ROUTE_COLUMNS = (
    ("asphalt", "trench_asphalt_m"),
    ("pavement", "trench_pavement_m"),
    ("verge", "trench_verge_m"),
    ("existing_duct", "existing_duct_m"),
    ("aerial", "aerial_m"),
    ("facade", "facade_m"),
)


def _names_by_node(design: Design) -> dict[int, str]:
    """The name of the site at each street node, the most important site winning."""
    out: dict[int, str] = {}
    for kind in ("chamber", "fat", "fdh", "pop"):  # later kinds overwrite earlier ones
        for f in design.by_kind(kind):
            node = f.openftth.get("node")
            name = f.props.get("name") or f.props.get("manhole_id") or f.props.get("site_name")
            if node is not None and name:
                out[int(node)] = str(name)
    return out


def _end(label: Any, names: dict[int, str]) -> str:
    text = str(label or "")
    if text.startswith("N") and text[1:].isdigit():
        return names.get(int(text[1:]), f"street node {text[1:]}")
    return text


def cable_schedule_rows(design: Design) -> list[dict[str, Any]]:
    names = _names_by_node(design)
    cabinets = {f.element_uuid: f.props.get("name", "") for f in design.by_kind("fdh")}
    rows: list[dict[str, Any]] = []
    for kind in ("feeder", "distribution"):
        for c in sorted(design.by_kind(kind), key=lambda f: str(f.props.get("name", ""))):
            routes = {col: 0.0 for _s, col in ROUTE_COLUMNS}
            bores = 0.0
            for surface, metres in (c.openftth.get("edges") or {}).values():
                if str(surface).startswith("crossing:"):
                    bores += float(metres)
                    continue
                col = dict(ROUTE_COLUMNS).get(str(surface))
                if col:
                    routes[col] += float(metres)
            rows.append(
                {
                    "cable": c.props.get("name", ""),
                    "type": kind,
                    "from": _end(c.props.get("from_label"), names),
                    "to": _end(c.props.get("to_label"), names),
                    "cabinet": cabinets.get(c.openftth.get("fdh_id") or "", ""),
                    "fibres": int(c.props.get("fiber_count", 0)),
                    "cables": int(c.props.get("cable_count", 1)),
                    "fibres_used": int(c.openftth.get("fibers_used", 0)),
                    "tubes": int(c.props.get("tube_count", 0)),
                    "length_m": round(float(c.props.get("length_m", 0.0)), 1),
                    "slack_m": round(float(c.props.get("slack_m", 0.0)), 1),
                    "total_length_m": round(float(c.props.get("total_length_m", 0.0)), 1),
                    **{k: round(v, 1) for k, v in routes.items()},
                    "bore_m": round(bores, 1),
                    "phase": c.openftth.get("phase", ""),
                    "lifecycle": c.openftth.get("lifecycle", ""),
                }
            )
    return rows


def port_report_rows(design: Design, rules: Rulebook) -> list[dict[str, Any]]:
    cabinets = {f.element_uuid: f.props.get("name", "") for f in design.by_kind("fdh")}
    rows: list[dict[str, Any]] = []

    def row(kind: str, name: str, capacity: int, used: int, limit: float | None, **extra) -> None:
        fill = round(100.0 * used / capacity, 1) if capacity else None
        rows.append(
            {
                "site": name,
                "type": kind,
                "ports": capacity,
                "used": used,
                "free": max(0, capacity - used),
                "fill_pct": fill,
                "fill_limit_pct": limit if limit is not None else "",
                "over_limit": bool(fill is not None and limit is not None and fill > limit),
                "cabinet": extra.get("cabinet", ""),
                "pon": extra.get("pon", ""),
                "units": extra.get("units", ""),
                "phase": extra.get("phase", ""),
            }
        )

    for f in sorted(design.by_kind("fdh"), key=lambda f: str(f.props.get("name", ""))):
        row(
            "cabinet",
            str(f.props.get("name", "")),
            int(f.props.get("port_capacity") or f.openftth.get("capacity") or 0),
            int(f.props.get("total_connections") or f.openftth.get("units") or 0),
            rules.fdh.max_fill_pct,
            pon=f"{int(f.openftth.get('pons', 0))} PONs",
            units=int(f.openftth.get("units", 0)),
            phase=f.openftth.get("phase", ""),
        )
    for f in sorted(design.by_kind("fat"), key=lambda f: str(f.props.get("name", ""))):
        row(
            "FAT",
            str(f.props.get("name", "")),
            int(f.props.get("port_capacity") or 0),
            int(f.props.get("total_connections") or f.openftth.get("units") or 0),
            None,
            cabinet=cabinets.get(f.openftth.get("fdh_id") or "", ""),
            pon=f.openftth.get("pon_id", ""),
            units=int(f.openftth.get("units", 0)),
            phase=f.openftth.get("phase", ""),
        )
    names = {f.element_uuid: f for f in design}
    for p in sorted(
        design.tables.get("splice_point", []), key=lambda p: str(p.get("element_uuid", ""))
    ):
        el = names.get(p.get("element_uuid", ""))
        if el is None or el.kind != "chamber":
            continue
        row(
            "closure",
            str(el.props.get("manhole_id", "")),
            int(p.get("port_count", 0) or 0),
            int(p.get("used_ports", 0) or 0),
            None,
            phase=el.openftth.get("phase", ""),
        )
    return rows


def materials_by_phase_rows(total: Boq, phases: dict[int, Boq]) -> list[dict[str, Any]]:
    """One row per BOQ item: the quantity in each build phase and in the whole design."""
    keys: list[tuple[str, str, str]] = []
    for b in [total, *phases.values()]:
        for line in b.lines:
            k = (line.category, line.item, line.unit)
            if k not in keys:
                keys.append(k)
    by: dict[int | None, dict[tuple[str, str, str], tuple[float, float]]] = {
        None: {(x.category, x.item, x.unit): (x.qty, x.total) for x in total.lines}
    }
    for k, b in phases.items():
        by[k] = {(x.category, x.item, x.unit): (x.qty, x.total) for x in b.lines}
    rows = []
    for cat, item, unit in keys:
        r: dict[str, Any] = {"category": cat, "item": item, "unit": unit}
        for k in sorted(phases):
            r[f"phase_{k}_qty"] = by[k].get((cat, item, unit), (0.0, 0.0))[0]
        r["design_qty"], r["design_total"] = by[None].get((cat, item, unit), (0.0, 0.0))
        rows.append(r)
    return rows


def to_csv(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return ""
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(rows[0]), lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()
