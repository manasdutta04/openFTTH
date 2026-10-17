"""Splice schedule: the per-closure rows a splicer works from, built from the bundle's
`fiber_connection` and `splice_point` tables."""

from __future__ import annotations

import csv
import io
from collections import Counter
from typing import Any

from ..interchange import Design
from ..solve.fibers import color_name


def splice_schedule_rows(design: Design) -> list[dict[str, Any]]:
    points = {p["uuid"]: p for p in design.tables.get("splice_point", [])}
    names = {f.element_uuid: _label(f) for f in design}
    rows: list[dict[str, Any]] = []
    for c in design.tables.get("fiber_connection", []):
        p = points.get(c.get("splice_point_uuid", ""), {})
        closure = names.get(p.get("element_uuid", ""), p.get("element_uuid", "")[:8])
        rows.append(
            {
                "closure": closure,
                "closure_uuid": p.get("element_uuid", ""),
                "type": c["connection_type"],
                "from": _side(names, c, "source"),
                "to": _side(names, c, "dest"),
                "loss_db": c.get("loss_db"),
                "tray": c.get("tray_ref") or "",
            }
        )
    rows.sort(key=lambda r: (r["closure"], r["type"], r["from"], r["to"]))
    return rows


def _label(f: Any) -> str:
    for key in ("name", "manhole_id", "site_name"):
        v = f.props.get(key)
        if v:
            return str(v)
    return f"{f.kind or f.element_type} {f.element_uuid[:8]}"


def _side(names: dict[str, str], c: dict[str, Any], side: str) -> str:
    cable = c.get(f"{side}_cable_uuid")
    if cable:
        tube, fib = c.get(f"{side}_tube"), c.get(f"{side}_fiber")
        return f"{names.get(cable, cable[:8])} T{tube}:{fib} ({color_name(int(tube))}/{color_name(int(fib))})"
    el = c.get(f"{side}_element_uuid") or ""
    port = c.get(f"{side}_port")
    side_txt = c.get(f"{side}_side") or ""
    return f"{names.get(el, el[:8])} port {port} {side_txt}".strip()


def splice_schedule_csv(rows: list[dict[str, Any]]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(
        buf,
        fieldnames=["closure", "type", "from", "to", "loss_db", "tray", "closure_uuid"],
        lineterminator="\n",
    )
    w.writeheader()
    for r in rows:
        w.writerow({k: r.get(k, "") for k in w.fieldnames})
    return buf.getvalue()


def splice_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_type = Counter(r["type"] for r in rows)
    by_closure = Counter(r["closure"] for r in rows)
    return {
        "connections": len(rows),
        "by_type": dict(by_type),
        "closures": len(by_closure),
        "largest_closure": max(by_closure.items(), key=lambda kv: kv[1]) if by_closure else None,
    }
