"""Bill of quantities with the categories a contractor can quote against:
civil, cables, passives, labour, permits."""

from __future__ import annotations

import math
from collections import Counter

from pydantic import BaseModel, Field

from ..interchange import Design
from ..rules import Rulebook


class BoqLine(BaseModel):
    category: str
    item: str
    qty: float
    unit: str
    unit_cost: float
    total: float
    note: str | None = None


class Boq(BaseModel):
    currency: str
    lines: list[BoqLine]
    totals: dict[str, float]
    grand_total: float
    units_served: int
    cost_per_unit: float | None
    assumptions: list[str] = Field(default_factory=list)


def build_boq(design: Design, rules: Rulebook) -> Boq:
    lines: list[BoqLine] = []
    notes: list[str] = []

    def add(
        cat: str, item: str, qty: float, unit: str, unit_cost: float, note: str | None = None
    ) -> None:
        if qty <= 0:
            return
        lines.append(
            BoqLine(
                category=cat,
                item=item,
                qty=round(qty, 2),
                unit=unit,
                unit_cost=unit_cost,
                total=round(qty * unit_cost, 2),
                note=note,
            )
        )

    # ---- civil: trench by surface (shared between feeder and distribution), chambers ----
    trench: dict[str, float] = {}
    seen_edges: set[str] = set()
    for cable in design.by_type("cable.underground"):
        if cable.openftth.get("kind") == "drop":
            continue
        for edge, (surface, metres) in cable.openftth.get("edges", {}).items():
            if edge in seen_edges:
                continue
            seen_edges.add(edge)
            trench[surface] = trench.get(surface, 0.0) + metres
    for surface in sorted(trench):
        sc = getattr(rules.surfaces, surface)
        add("civil", f"trench in {surface}", trench[surface], "m", sc.trench_per_m)
        if sc.reinstatement_per_m:
            add("civil", f"reinstatement {surface}", trench[surface], "m", sc.reinstatement_per_m)
    drop_offset = sum(float(d.openftth.get("offset_m", 0.0)) for d in design.by_kind("drop"))
    add(
        "civil",
        "drop trench (home to street)",
        drop_offset,
        "m",
        rules.surfaces.cost("verge"),
        "assumed: verge/garden trench for the off-street part of each drop",
    )
    chambers = Counter(c.props.get("manhole_type", "small") for c in design.by_kind("chamber"))
    add("civil", "chamber small", chambers.get("small", 0), "pc", rules.units.chamber_small)
    add("civil", "chamber large", chambers.get("large", 0), "pc", rules.units.chamber_large)

    # ---- cables -------------------------------------------------------------------------
    by_size: dict[tuple[str, int], float] = {}
    drop_m = 0.0
    for cable in design.by_type("cable.underground"):
        kind = cable.openftth.get("kind")
        length = float(cable.props.get("total_length_m") or cable.props.get("length_m") or 0.0)
        if kind == "drop":
            drop_m += length
            continue
        fibers = int(cable.props.get("fiber_count", 0))
        count = int(cable.props.get("cable_count", 1))
        by_size[(kind, fibers)] = by_size.get((kind, fibers), 0.0) + length * count
    for (kind, fibers), metres in sorted(by_size.items()):
        add(
            "cables",
            f"{kind} cable {fibers}F",
            metres,
            "m",
            rules.units.cable_base_per_m + fibers * rules.units.cable_per_fiber_m,
            "incl. slack",
        )
    add("cables", f"drop cable {rules.fibers.drop}F", drop_m, "m", rules.units.drop_per_m)

    # ---- passives -----------------------------------------------------------------------
    fats = design.by_kind("fat")
    fdhs = design.by_kind("fdh")
    add("passives", "FAT", len(fats), "pc", rules.units.fat)
    for cap in sorted({int(f.openftth.get("capacity", 0)) for f in fdhs}):
        n = sum(1 for f in fdhs if int(f.openftth.get("capacity", 0)) == cap)
        add(
            "passives",
            f"FDH {cap}",
            n,
            "pc",
            getattr(rules.units, f"fdh_{cap}", rules.units.fdh_576),
        )
    pons = {h.openftth.get("pon_id") for h in design.by_kind("home") if h.openftth.get("pon_id")}
    stages = rules.cascade_stages()
    if len(stages) == 1:
        add(
            "passives",
            f"splitter 1:{stages[0]}",
            len(pons),
            "pc",
            getattr(rules.units, f"splitter_1_{stages[0]}", rules.units.splitter_1_32),
        )
    else:
        add(
            "passives",
            f"splitter 1:{stages[0]} (FDH)",
            len(pons),
            "pc",
            getattr(rules.units, f"splitter_1_{stages[0]}", rules.units.splitter_1_4),
        )
        add(
            "passives",
            f"splitter 1:{stages[1]} (FAT)",
            len(fats),
            "pc",
            getattr(rules.units, f"splitter_1_{stages[1]}", rules.units.splitter_1_8),
        )
    closures = Counter(
        int(c.openftth.get("size", 48))
        for c in design.by_kind("chamber")
        if c.openftth.get("closure")
    )
    for size in sorted(closures):
        add(
            "passives",
            f"closure {size}F",
            closures[size],
            "pc",
            getattr(rules.units, f"closure_{size}", rules.units.closure_288),
        )
    add("passives", "ODF ports", len(pons), "port", rules.units.odf_per_port)

    # ---- labour -------------------------------------------------------------------------
    units = sum(int(h.openftth.get("units", 1)) for h in design.by_kind("home"))
    splices = units * 2 + len(pons)  # drop@FAT + distribution@FDH per unit, feeder@FDH per PON
    add(
        "labour",
        "fusion splice",
        splices,
        "pc",
        rules.labour.splice,
        "2 per unit (FAT, FDH) + 1 per PON at the FDH",
    )
    add("labour", "OTDR test", units, "pc", rules.labour.otdr_test)
    add("labour", "FAT install", len(fats), "pc", rules.labour.fat_install)
    add("labour", "FDH install", len(fdhs), "pc", rules.labour.fdh_install)

    # ---- permits ------------------------------------------------------------------------
    crossings = sum(
        int(c.openftth.get("road_crossings", 0)) for c in design.by_type("cable.underground")
    )
    add(
        "permits",
        "road crossing (directional drill)",
        crossings,
        "pc",
        rules.crossings.road_drill_per_m * 8.0,
        "assumed 8 m per crossing",
    )
    notes.append(
        "Phase 0: crossings are counted only where a cable follows a primary/secondary road edge; detailed crossing detection comes with the duct layer."
    )

    totals: dict[str, float] = {}
    for line in lines:
        totals[line.category] = round(totals.get(line.category, 0.0) + line.total, 2)
    grand = round(sum(totals.values()), 2)
    cpu = round(grand / units, 2) if units else None
    return Boq(
        currency=rules.currency,
        lines=lines,
        totals=totals,
        grand_total=grand,
        units_served=units,
        cost_per_unit=cpu,
        assumptions=notes,
    )


def boq_to_csv(boq: Boq) -> str:
    rows = ["category,item,qty,unit,unit_cost,total,note"]
    for line in boq.lines:
        note = (line.note or "").replace('"', "'")
        rows.append(
            f'{line.category},"{line.item}",{line.qty},{line.unit},{line.unit_cost},{line.total},"{note}"'
        )
    rows.append(f',"TOTAL",,,{"" if math.isnan(boq.grand_total) else ""},{boq.grand_total},')
    return "\n".join(rows) + "\n"
