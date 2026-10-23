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
    units_connected: int = 0
    cost_per_connection: float | None = None
    assumptions: list[str] = Field(default_factory=list)


def _transitions(design: Design) -> dict[str, int]:
    """Points where the network goes from poles or walls into the ground: a node shared by an
    aerial (or facade) edge and an edge in a trench, duct or bore."""
    kinds: dict[str, set[str]] = {}
    for cable in design.by_type("cable.underground"):
        if cable.openftth.get("kind") == "drop":
            continue
        for edge, (surface, _m) in cable.openftth.get("edges", {}).items():
            group = surface if surface in ("aerial", "facade") else "ground"
            for node in str(edge).split("-"):
                kinds.setdefault(node, set()).add(group)
    out = {"aerial": 0, "facade": 0}
    for groups in kinds.values():
        if "ground" in groups or len(groups) > 1:
            for k in ("aerial", "facade"):
                if k in groups and (groups - {k}):
                    out[k] += 1
    return out


def build_boq(design: Design, rules: Rulebook, dug: set[str] | None = None) -> Boq:
    """The bill of quantities. The network (trench, cables, cabinets, FATs) is built for every
    home passed; the connection of a home (its drop, the flat cabling, its splice at the FAT and
    its test) only for the share `fibers.take_rate` expected to take a service.

    `dug`: street edges an earlier build phase already trenched; they are not trenched again,
    and this design's edges are added to it."""
    lines: list[BoqLine] = []
    notes: list[str] = []
    take = min(1.0, max(0.0, float(rules.fibers.take_rate)))
    at_take = f"× take rate {take:.0%}" if take < 1 else None

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
    bores: dict[str, list[float]] = {}  # barrier → length of each crossing under it
    seen_edges: set[str] = set(dug) if dug is not None else set()
    for cable in design.by_type("cable.underground"):
        if cable.openftth.get("kind") == "drop":
            continue
        for edge, (surface, metres) in cable.openftth.get("edges", {}).items():
            if edge in seen_edges:
                continue
            seen_edges.add(edge)
            if str(surface).startswith("crossing:"):
                bores.setdefault(str(surface).split(":", 1)[1], []).append(metres)
            else:
                trench[surface] = trench.get(surface, 0.0) + metres
    if dug is not None:
        dug |= seen_edges
    transitions = _transitions(design)
    for barrier in sorted(bores):
        price = getattr(rules.crossings, barrier)
        label = {"water": "river or canal", "unmapped": "unmapped obstacle"}.get(barrier, barrier)
        add(
            "civil",
            f"crossing under {label}: permit and pits",
            len(bores[barrier]),
            "pc",
            price.fixed,
            "directional drill, taken where it is cheaper than going round",
        )
        add("civil", f"crossing under {label}: drilling", sum(bores[barrier]), "m", price.per_m)
    overhead = {"aerial": "aerial span on existing poles", "facade": "facade cabling"}
    for surface in sorted(trench):
        sc = getattr(rules.surfaces, surface)
        item = overhead.get(surface, f"trench in {surface}")
        add("civil", item, trench[surface], "m", sc.trench_per_m)
        if sc.reinstatement_per_m:
            add("civil", f"reinstatement {surface}", trench[surface], "m", sc.reinstatement_per_m)
    if trench.get("aerial"):
        add(
            "civil",
            "pole attachment",
            math.ceil(trench["aerial"] / rules.aerial.max_span_m) + transitions["aerial"],
            "pc",
            rules.aerial.pole_attach,
            f"one per {rules.aerial.max_span_m:g} m span, plus one per run end",
        )
    for kind, label in (("aerial", "pole"), ("facade", "wall")):
        add(
            "civil",
            f"{label} to ground transition",
            transitions[kind],
            "pc",
            getattr(rules, kind).transition,
            "where the route goes from overhead into the ground",
        )
    drop_offset = sum(float(d.openftth.get("offset_m", 0.0)) for d in design.by_kind("drop"))
    add(
        "civil",
        "drop trench (home to street)",
        drop_offset * take,
        "m",
        rules.surfaces.cost("verge"),
        "assumed: verge/garden trench for the off-street part of each drop"
        + (f"; {at_take}" if at_take else ""),
    )
    chambers = Counter(
        c.props.get("manhole_type", "small")
        for c in design.by_kind("chamber")
        if not c.openftth.get("mount")  # a closure on a pole or wall: nothing dug
    )
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
    add(
        "cables",
        f"drop cable {rules.fibers.drop}F",
        drop_m * take,
        "m",
        rules.units.drop_per_m,
        at_take,
    )

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
    # PONs belong to FATs: a building's dedicated FATs can hang off two cabinets, so counting
    # the homes' single pon_id would miss one.
    pons = {f.openftth.get("pon_id") for f in design.by_kind("fat") if f.openftth.get("pon_id")}
    stages = rules.cascade_stages()
    arch = rules.pon.arch()
    if arch == "p2p":
        pass  # no splitters: each unit's fibre is patched through to its own POP port
    elif len(stages) == 1:
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
            sum(int(f.openftth.get("splitters", 1)) for f in fats),
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
    units = sum(int(h.openftth.get("units", 1)) for h in design.by_kind("home"))
    if arch == "p2p":
        add("passives", "ODF ports", units, "port", rules.units.odf_per_port, "one per unit")
    else:
        add("passives", "ODF ports", len(pons), "port", rules.units.odf_per_port)

    # ---- inside plant: risers, floor boxes and flat cabling in buildings with several units ---
    ins = rules.inside
    mdus = [h for h in design.by_kind("home") if float(h.openftth.get("riser_m", 0) or 0) > 0]
    if mdus:
        riser_m = sum(float(h.openftth["riser_m"]) for h in mdus)
        floors = sum(int(h.openftth.get("floors", 0)) for h in mdus)
        boxes = sum(
            max(
                int(h.openftth.get("floors", 0)),
                -(-int(h.openftth.get("units", 1)) // max(1, ins.units_per_floor_box)),
            )
            for h in mdus
        )
        flats = sum(int(h.openftth.get("units", 1)) for h in mdus)
        add(
            "inside plant", "riser cable", riser_m, "m", ins.riser_per_m, f"{len(mdus)} building(s)"
        )
        add("inside plant", "floor box", boxes, "pc", ins.floor_box, "one per floor at least")
        add(
            "inside plant",
            "flat cabling and outlet",
            flats * take,
            "pc",
            ins.indoor_per_unit,
            at_take,
        )
        add("labour", "riser install", floors, "floor", ins.riser_install_per_floor)

    # ---- labour -------------------------------------------------------------------------
    # Splices made while building the network, and the one per home made when it connects.
    if arch == "p2p":
        splices = units * 2  # distribution@FDH and feeder@FDH, per unit
        why = "2 per unit: FDH (distribution) and FDH (feeder)"
    elif arch == "cascaded":
        fat_splitters = sum(int(f.openftth.get("splitters", 1)) for f in fats)
        splices = 2 * fat_splitters + len(pons)
        why = (
            "2 per FAT splitter (its input at the FAT, its fibre at the FDH) + 1 per PON at the FDH"
        )
    else:
        splices = units + len(pons)  # distribution@FDH per unit, feeder@FDH per PON
        why = "1 per unit at the FDH + 1 per PON"
    add("labour", "fusion splice (network)", splices, "pc", rules.labour.splice, why)
    add(
        "labour",
        "fusion splice (connection)",
        units * take,
        "pc",
        rules.labour.splice,
        "1 per connected unit, at the FAT" + (f"; {at_take}" if at_take else ""),
    )
    add("labour", "OTDR test", units * take, "pc", rules.labour.otdr_test, at_take)
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
    connected = int(round(units * take))
    if take < 1:
        notes.append(
            f"Take rate {take:.0%}: the network passes all {units} units; drops, flat cabling, "
            f"connection splices and tests are counted for {connected} connected units."
        )
    return Boq(
        currency=rules.currency,
        lines=lines,
        totals=totals,
        grand_total=grand,
        units_served=units,
        cost_per_unit=cpu,
        units_connected=connected,
        cost_per_connection=round(grand / connected, 2) if connected else None,
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
