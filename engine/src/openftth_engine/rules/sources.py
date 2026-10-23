"""Where every rulebook value comes from.

Each rulebook path (dotted; `*` in a pattern matches anything) has a source:

- `standard`: set or bounded by a published standard (ITU-T, IEEE, TIA, IEC);
- `practice`: common industry practice or typical datasheet values, inside the standard's limits;
- `model`: a modelling choice of the engine (how it counts, what it routes along);
- `assumed`: a placeholder the user must replace with their own (prices, local rules).

`docs/RULEBOOK.md` carries the generated table (`openftth-engine rules sources`); a test makes
sure every value of every shipped preset has an entry here.
"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Any, Literal

Kind = Literal["standard", "practice", "model", "assumed"]


@dataclass(frozen=True)
class Source:
    kind: Kind
    ref: str
    note: str = ""


G984 = "ITU-T G.984.2 (GPON PMD)"
G9807 = "ITU-T G.9807.1 (XGS-PON)"
G652 = "ITU-T G.652 (single-mode fibre), table for G.652.D"

#: path pattern → source; the first matching pattern wins, so specific ones come first.
SOURCES: list[tuple[str, Source]] = [
    ("version", Source("model", "openFTTH rulebook format")),
    ("name", Source("model", "openFTTH rulebook format")),
    ("description", Source("model", "openFTTH rulebook format")),
    ("country", Source("model", "ISO 3166-1 alpha-2 code")),
    ("currency", Source("model", "ISO 4217 code", "every price in the rulebook is in it")),
    ("review.*", Source("model", "openFTTH rulebook format", "who reviewed the values, and when")),
    # ---- PON ----------------------------------------------------------------------------
    ("pon.technology", Source("standard", f"{G984}; {G9807}; ITU-T G.984.5 (coexistence)")),
    (
        "pon.class",
        Source("standard", f"{G984} classes B+, C+; {G9807} classes N1, N2, E1, E2"),
    ),
    (
        "pon.budget_db",
        Source("standard", f"{G984}; {G9807}", "the class's maximum ODN loss; may be set lower"),
    ),
    ("pon.min_loss_db", Source("standard", f"{G984}; {G9807}", "the class's minimum ODN loss")),
    (
        "pon.max_differential_db",
        Source("standard", f"{G984}; {G9807}", "15 dB maximum differential optical path loss"),
    ),
    (
        "pon.split_ratio",
        Source("practice", "ITU-T G.984.1 (up to 1:64 per PON)", "1:32 is the common choice"),
    ),
    ("pon.cascade", Source("practice", "operator architecture", "stages multiply to the split")),
    ("pon.architecture", Source("model", "openFTTH architectures: centralised, cascaded, p2p")),
    (
        "pon.worst_case_nm",
        Source(
            "standard",
            f"{G984} upstream 1260–1360 nm; {G9807} upstream 1260–1280 nm",
            "the wavelength with the highest fibre loss on the path",
        ),
    ),
    (
        "pon.p2p_budget_db",
        Source("assumed", "bidirectional 1G optics datasheets", "check the optics you buy"),
    ),
    # ---- fibres and homes ---------------------------------------------------------------
    ("fibers.drop", Source("practice", "operator drop cable types (1, 2 or 4 fibres)")),
    (
        "fibers.distribution",
        Source("practice", "TIA-598-C (12-fibre tubes)", "cable sizes the supplier stocks"),
    ),
    ("fibers.feeder", Source("practice", "TIA-598-C (12-fibre tubes)", "cable sizes in stock")),
    ("fibers.spare_pct", Source("assumed", "operator planning rule")),
    ("fibers.take_rate", Source("assumed", "the business case", "share of homes that connect")),
    ("homes.count_units", Source("model", "openFTTH counts dwelling units, not addresses")),
    ("homes.*", Source("assumed", "local building stock", "used only when the data has no units")),
    # ---- distances ----------------------------------------------------------------------
    (
        "distances.max_reach_km",
        Source("standard", "ITU-T G.984.1 (20 km physical reach; 60 km logical)"),
    ),
    ("distances.*", Source("assumed", "operator planning rule")),
    # ---- losses -------------------------------------------------------------------------
    (
        "loss_db.connector",
        Source(
            "practice",
            "IEC 61755-1 performance grades",
            "a design value per mated pair; grade C connectors stay at or below 0.5 dB",
        ),
    ),
    ("loss_db.splice", Source("practice", "ITU-T L.12 (fusion splices)", "about 0.1 dB")),
    ("loss_db.margin", Source("practice", "operator planning rule", "repairs and ageing")),
    (
        "loss_db.splitter.*",
        Source(
            "practice",
            "PLC splitter datasheet maxima; ITU-T G.671",
            "never below the ideal 10·log10(N) dB",
        ),
    ),
    (
        "loss_db.fiber_per_km.*",
        Source(
            "practice",
            f"{G652}",
            "typical cabled values, below the maxima of 0.4 dB/km at 1310 nm and 0.3 dB/km at "
            "1550 nm",
        ),
    ),
    (
        "loss_db.coexistence",
        Source("practice", "ITU-T G.984.5 (WDM1r coexistence element)", "about 1 dB"),
    ),
    ("connectors_per_path.*", Source("model", "openFTTH path model", "connectors on one path")),
    # ---- equipment ----------------------------------------------------------------------
    ("pops.max_units", Source("assumed", "OLT chassis size and operator rule")),
    ("fdh.capacities", Source("practice", "cabinet sizes on the market (splitter ports)")),
    ("fdh.max_fill_pct", Source("assumed", "operator planning rule", "room for growth")),
    ("fdh.placements", Source("model", "openFTTH placement kinds")),
    ("fat.*", Source("assumed", "operator FAT products and rules")),
    ("chambers.*", Source("assumed", "local chamber products")),
    ("closures.sizes", Source("practice", "splice closure sizes on the market")),
    ("slack.*", Source("practice", "operator installation rule", "coiled at closures and FATs")),
    ("ducts.prefer_existing", Source("model", "openFTTH routing: reuse before digging")),
    (
        "ducts.max_fill_pct",
        Source("practice", "NEC Chapter 9 Table 1 (40 % for three or more cables)"),
    ),
    (
        "ducts.min_bend_radius_mm",
        Source("practice", "cable datasheets (about 20 × cable diameter under load)"),
    ),
    ("ducts.microduct_bundles", Source("practice", "microduct bundle sizes on the market")),
    # ---- routes and prices --------------------------------------------------------------
    ("surfaces.*.*", Source("assumed", "local civil works prices")),
    ("aerial.enabled", Source("model", "openFTTH route options")),
    ("aerial.max_span_m", Source("assumed", "pole owner's rule")),
    ("aerial.*", Source("assumed", "pole owner's fees and local prices")),
    ("facade.enabled", Source("model", "openFTTH route options", "needs the owners' consent")),
    ("facade.highways", Source("model", "OpenStreetMap highway=* classes")),
    ("facade.*", Source("assumed", "local practice and prices")),
    ("crossings.max_length_m", Source("assumed", "directional drilling contractor")),
    ("crossings.*", Source("assumed", "local permit and drilling prices")),
    ("crossings.*.*", Source("assumed", "local permit and drilling prices")),
    (
        "routing.exclude_highways",
        Source("model", "OpenStreetMap highway=* classes", "ways no cable may follow"),
    ),
    (
        "highway_to_surface.*",
        Source("model", "OpenStreetMap highway=* classes", "where the trench goes"),
    ),
    ("units.*", Source("assumed", "supplier price list")),
    ("labour.*", Source("assumed", "contractor rates")),
    ("inside.floor_height_m", Source("practice", "typical storey height")),
    ("inside.*", Source("assumed", "local building practice and prices")),
    ("phasing.*", Source("model", "openFTTH build phasing")),
]


def source_for(path: str) -> Source | None:
    for pattern, src in SOURCES:
        if fnmatchcase(path, pattern):
            return src
    return None


def leaf_paths(data: Any, prefix: str = "") -> list[str]:
    """Every value's dotted path in a dumped rulebook; lists are one value, dicts are walked."""
    if isinstance(data, dict):
        out: list[str] = []
        for k, v in data.items():
            out += leaf_paths(v, f"{prefix}.{k}" if prefix else str(k))
        return out
    return [prefix]


def unsourced(rules: Any) -> list[str]:
    """The paths of a rulebook with no entry in SOURCES (should be none)."""
    return [p for p in leaf_paths(rules.model_dump(by_alias=True)) if source_for(p) is None]


BEGIN = "<!-- sources:begin (generated by `openftth-engine rules sources --out`) -->"
END = "<!-- sources:end -->"


def write_sources_table(path: Any) -> None:
    """Replace the generated table between the markers in RULEBOOK.md."""
    from pathlib import Path

    p = Path(path)
    text = p.read_text(encoding="utf-8")
    head, rest = text.split(BEGIN, 1)
    _old, tail = rest.split(END, 1)
    p.write_text(f"{head}{BEGIN}\n{sources_markdown()}{END}{tail}", encoding="utf-8", newline="\n")


def sources_markdown() -> str:
    """The table for docs/RULEBOOK.md."""
    lines = ["| Values | Kind | Source | Note |", "|---|---|---|---|"]
    for pattern, src in SOURCES:
        lines.append(f"| `{pattern}` | {src.kind} | {src.ref} | {src.note} |")
    return "\n".join(lines) + "\n"
