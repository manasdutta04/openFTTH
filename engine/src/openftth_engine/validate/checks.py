"""Rule checks over a design. Each violation points at a `element_uuid`."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..interchange import Design, Feature
from ..rules import Rulebook


@dataclass
class Violation:
    check: str
    element: str
    message: str
    values: dict[str, Any] = field(default_factory=dict)
    severity: str = "error"  # error | warning


def check_capacity(design: Design, rules: Rulebook) -> list[Violation]:
    out: list[Violation] = []
    for fat in design.by_kind("fat"):
        u = int(fat.openftth.get("units", 0))
        if u > rules.fat.max_homes:
            out.append(
                Violation(
                    "capacity",
                    fat.element_uuid,
                    f"FAT serves {u} units > max_homes {rules.fat.max_homes}",
                    {"units": u},
                )
            )
    for fdh in design.by_kind("fdh"):
        u = int(fdh.openftth.get("units", 0))
        cap = int(fdh.openftth.get("capacity", 0))
        limit = cap * rules.fdh.max_fill_pct / 100.0
        if u > limit:
            out.append(
                Violation(
                    "capacity",
                    fdh.element_uuid,
                    f"FDH {u} units > {limit:g} ({rules.fdh.max_fill_pct:g}% of {cap})",
                    {"units": u, "capacity": cap},
                )
            )
    pon_units: dict[str, int] = {}
    for fat in design.by_kind("fat"):
        pon = fat.openftth.get("pon_id")
        if pon:
            pon_units[pon] = pon_units.get(pon, 0) + int(fat.openftth.get("units", 0))
    for pon, u in sorted(pon_units.items()):
        if u > rules.pon.split_ratio:
            out.append(
                Violation(
                    "capacity",
                    pon,
                    f"PON {pon} has {u} units > split 1:{rules.pon.split_ratio}",
                    {"units": u},
                )
            )
    return out


def check_distances(design: Design, rules: Rulebook) -> list[Violation]:
    out: list[Violation] = []
    for drop in design.by_kind("drop"):
        length = float(drop.props.get("length_m", 0.0))
        if length > rules.distances.max_drop_m:
            out.append(
                Violation(
                    "distance",
                    drop.element_uuid,
                    f"drop {length:.0f} m > max_drop_m {rules.distances.max_drop_m:g}",
                    {"length_m": length},
                )
            )
    for home in design.by_kind("home"):
        d = float(home.openftth.get("distribution_path_m", 0.0))
        if d > rules.distances.max_dist_m:
            out.append(
                Violation(
                    "distance",
                    home.element_uuid,
                    f"FDH→FAT path {d:.0f} m > max_dist_m {rules.distances.max_dist_m:g}",
                    {"distribution_path_m": d},
                )
            )
    return out


def path_loss_db(rules: Rulebook, path_m: float, splices: int) -> dict[str, float]:
    km = path_m / 1000.0
    fiber = km * rules.loss_db.fiber_per_km[rules.pon.worst_case_nm]
    connectors = rules.connectors_on_path() * rules.loss_db.connector
    splice = splices * rules.loss_db.splice
    splitter = rules.splitter_loss()
    total = fiber + connectors + splice + splitter + rules.loss_db.margin
    return {
        "fiber": fiber,
        "connectors": connectors,
        "splices": splice,
        "splitter": splitter,
        "margin": rules.loss_db.margin,
        "total": total,
    }


def check_power_budget(design: Design, rules: Rulebook) -> tuple[list[Violation], dict[str, Any]]:
    """Per-home ODN loss at the worst-case wavelength vs. the class budget, plus the
    differential loss within each PON."""
    out: list[Violation] = []
    per_pon: dict[str, list[float]] = {}
    worst = 0.0
    best = float("inf")
    n = 0
    for home in design.by_kind("home"):
        path_m = float(home.openftth.get("path_to_pop_m", 0.0))
        splices = int(home.openftth.get("splices_on_path", 0))
        loss = path_loss_db(rules, path_m, splices)
        home.openftth["odn_loss_db"] = round(loss["total"], 2)
        n += 1
        worst = max(worst, loss["total"])
        best = min(best, loss["total"])
        pon = home.openftth.get("pon_id")
        if pon:
            per_pon.setdefault(pon, []).append(loss["total"])
        if loss["total"] > rules.pon.budget_db:
            out.append(
                Violation(
                    "power_budget",
                    home.element_uuid,
                    f"ODN loss {loss['total']:.1f} dB > budget {rules.pon.budget_db:g} dB",
                    loss,
                )
            )
    for pon, losses in sorted(per_pon.items()):
        diff = max(losses) - min(losses)
        if diff > rules.pon.max_differential_db:
            out.append(
                Violation(
                    "power_budget",
                    pon,
                    f"PON {pon} differential loss {diff:.1f} dB > {rules.pon.max_differential_db:g}",
                    {"differential_db": diff},
                )
            )
    stats = {
        "homes": n,
        "worst_db": round(worst, 2),
        "best_db": round(best, 2) if n else None,
        "budget_db": rules.pon.budget_db,
        "min_loss_db": rules.pon.min_loss_db,
    }
    if n and best < rules.pon.min_loss_db:
        stats["note"] = (
            f"Some paths are below the class minimum loss ({rules.pon.min_loss_db:g} dB); an attenuator may be needed at the ONT."
        )
    return out, stats


def run_all_checks(design: Design, rules: Rulebook) -> tuple[list[Violation], dict[str, Any]]:
    v = check_capacity(design, rules) + check_distances(design, rules)
    pb, stats = check_power_budget(design, rules)
    return v + pb, stats


def violation_summary(violations: list[Violation]) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in violations:
        out[v.check] = out.get(v.check, 0) + 1
    return out


def element_label(f: Feature) -> str:
    return f.props.get("name") or f.openftth.get("kind", f.element_type)
