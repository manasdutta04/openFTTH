"""The design rulebook: every number a solver or validator may use.

A rulebook is a YAML file validated by this model. `rulebooks/schema.json` is generated
from it (`openftth-engine schema`). Every preset value carries a provenance marker in the
YAML (`# source:` or `# assumed`), see docs/RULEBOOK.md.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

PonClass = Literal["B+", "C+", "N1", "N2", "E1", "E2"]
PonTechnology = Literal["gpon", "xgspon", "gpon+xgspon"]

#: Nominal ODN loss range of each class (min, max dB): ITU-T G.984.2 (GPON B+, C+) and
#: G.9807.1 (XGS-PON N1, N2, E1, E2).
CLASS_LOSS: dict[str, tuple[float, float]] = {
    "B+": (13.0, 28.0),
    "C+": (17.0, 32.0),
    "N1": (14.0, 29.0),
    "N2": (16.0, 31.0),
    "E1": (18.0, 33.0),
    "E2": (20.0, 35.0),
}
GPON_CLASSES = ("B+", "C+")
XGS_CLASSES = ("N1", "N2", "E1", "E2")
SurfaceClass = Literal["asphalt", "pavement", "verge", "aerial", "facade", "existing_duct"]
Placement = Literal["pole", "wall", "chamber", "cabinet"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PriceModel(StrictModel):
    """A section of prices and counts: nothing in it may be negative."""

    @model_validator(mode="after")
    def _not_negative(self):
        for name, value in self:
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value < 0:
                raise ValueError(f"{name} is {value:g}; a price or count cannot be negative")
        return self


def _ascending(name: str, values: list[int]) -> None:
    if not values:
        raise ValueError(f"{name} needs at least one size")
    if any(v <= 0 for v in values) or values != sorted(set(values)):
        raise ValueError(f"{name} must be positive sizes, smallest first, each once: {values}")


SPLIT_RATIOS = (2, 4, 8, 16, 32, 64, 128)


class Pon(StrictModel):
    technology: PonTechnology = Field(
        "gpon",
        description="gpon (G.984), xgspon (G.9807.1, 10G symmetric) or gpon+xgspon: both on one "
        "ODN through a coexistence element, whose loss (loss_db.coexistence) every path pays",
    )
    pon_class: PonClass = Field(
        "B+",
        alias="class",
        description="ODN loss class: B+ or C+ for GPON, N1, N2, E1 or E2 for XGS-PON (with both "
        "on one ODN, the tighter of the two classes in use)",
    )
    budget_db: float = Field(28.0, description="Max ODN loss for the class (B+ = 28 dB)")
    min_loss_db: float = Field(13.0, description="Min ODN loss for the class (B+ = 13 dB)")
    max_differential_db: float = Field(
        15.0, gt=0, le=20, description="Max difference in loss between ONTs on one PON"
    )
    split_ratio: int = Field(32, description="Total split per PON (1:N)")
    cascade: list[int] | None = Field(
        None,
        description="Cascaded split stages, e.g. [4, 8]. None means a single centralised "
        "splitter in the FDH.",
    )
    worst_case_nm: Literal[1270, 1310, 1490, 1550, 1577] = Field(
        1310,
        description="Wavelength used for the worst-case budget check: 1310 nm (GPON upstream), "
        "1270 nm (XGS-PON upstream, the worse of the two when XGS-PON is on the ODN)",
    )
    architecture: Literal["centralised", "cascaded", "p2p"] | None = Field(
        None,
        description="centralised: one splitter per PON in the FDH; cascaded: `cascade` stages, the "
        "first in the FDH and the second in each FAT; p2p: no splitters, one fibre per unit from "
        "the POP. Left out, it follows `cascade`.",
    )
    p2p_budget_db: float = Field(
        14.0,
        gt=0,
        le=40,
        description="Loss budget of point-to-point optics (1000BASE-BX20 class); used instead of "
        "budget_db when the architecture is p2p",
    )

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    def arch(self) -> str:
        """The architecture in force: the one named, or what `cascade` implies."""
        return self.architecture or ("cascaded" if self.cascade else "centralised")

    @model_validator(mode="after")
    def _architecture_matches_cascade(self) -> Pon:
        if self.architecture == "cascaded" and (not self.cascade or len(self.cascade) != 2):
            raise ValueError("a cascaded architecture needs two `cascade` stages, e.g. [4, 8]")
        if self.architecture == "centralised" and self.cascade:
            raise ValueError("a centralised architecture has no `cascade`; set it to null")
        return self

    @model_validator(mode="after")
    def _class_fits_technology(self) -> Pon:
        if self.technology == "gpon" and self.pon_class not in GPON_CLASSES:
            raise ValueError(f"class {self.pon_class} is an XGS-PON class; GPON uses B+ or C+")
        if self.technology == "xgspon" and self.pon_class not in XGS_CLASSES:
            raise ValueError(
                f"class {self.pon_class} is a GPON class; XGS-PON uses N1, N2, E1 or E2"
            )
        if self.technology != "gpon" and self.worst_case_nm in (1490, 1550):
            raise ValueError(
                "with XGS-PON on the ODN the worst case is its 1270 nm upstream (or 1310 nm); "
                "set worst_case_nm to 1270"
            )
        return self

    @model_validator(mode="after")
    def _ratios_are_powers_of_two(self) -> Pon:
        for r in [self.split_ratio, *(self.cascade or [])]:
            if r not in SPLIT_RATIOS:
                raise ValueError(f"split 1:{r} does not exist; splitters are 1:2 … 1:128")
        return self

    @model_validator(mode="after")
    def _cascade_matches_ratio(self) -> Pon:
        if self.cascade:
            prod = 1
            for s in self.cascade:
                prod *= s
            if prod != self.split_ratio:
                raise ValueError(f"cascade {self.cascade} does not multiply to {self.split_ratio}")
        return self


class Fibers(StrictModel):
    drop: int = Field(2, ge=1, le=12, description="Fibres per drop cable")
    distribution: list[int] = Field(
        [12, 24, 48, 96, 144, 288], description="Distribution cable ladder"
    )
    feeder: list[int] = Field([96, 144, 288], description="Feeder cable ladder")
    spare_pct: float = Field(
        20.0, ge=0, le=200, description="Spare fibres as % of demand, rounded up to ladder"
    )
    take_rate: float = Field(
        1.0, gt=0, le=1, description="Share of homes passed that get a drop (0–1)"
    )

    @model_validator(mode="after")
    def _ladders(self) -> Fibers:
        _ascending("fibers.distribution", self.distribution)
        _ascending("fibers.feeder", self.feeder)
        return self


class Homes(StrictModel):
    count_units: bool = Field(
        True, description="Count dwelling units (busnummers / flats), not addresses"
    )
    default_units_apartment: int = Field(
        6, ge=1, le=500, description="Units assumed for an apartment building with no unit data"
    )
    m2_per_unit: float = Field(
        90.0,
        ge=15,
        le=1000,
        description="Floor area per unit used to estimate units from footprint × levels",
    )


class Distances(StrictModel):
    max_drop_m: float = Field(
        150.0, gt=0, le=2000, description="Max drop length home → FAT along the street"
    )
    max_dist_m: float = Field(
        2000.0, gt=0, le=20000, description="Max distribution cable length FDH → FAT"
    )
    max_reach_km: float = Field(
        20.0, gt=0, le=60, description="Max reach OLT → ONT (G.984.1: 60 km logical at most)"
    )
    max_pull_m: float = Field(250.0, gt=0, le=5000, description="Max pull between chambers")


class LossDb(StrictModel):
    connector: float = Field(0.3, ge=0, le=1.0, description="Loss per mated connector pair")
    splice: float = Field(0.1, ge=0, le=0.5, description="Loss per fusion splice")
    margin: float = Field(2.0, ge=0, le=10, description="Design margin added to every path")
    splitter: dict[int, float] = Field(
        default_factory=lambda: {2: 3.7, 4: 7.2, 8: 10.5, 16: 13.8, 32: 17.1, 64: 20.5}
    )
    fiber_per_km: dict[int, float] = Field(
        default_factory=lambda: {1270: 0.37, 1310: 0.35, 1490: 0.25, 1550: 0.22, 1577: 0.23}
    )
    coexistence: float = Field(
        1.0,
        ge=0,
        le=3,
        description="Insertion loss of the coexistence element (WDM1r) when GPON and XGS-PON "
        "share an ODN",
    )

    @model_validator(mode="after")
    def _physical(self) -> LossDb:
        import math

        last = 0.0
        for n in sorted(self.splitter):
            loss = self.splitter[n]
            ideal = 10 * math.log10(n)
            if n not in SPLIT_RATIOS:
                raise ValueError(f"loss_db.splitter has a 1:{n} splitter, which does not exist")
            if not ideal <= loss <= ideal + 4:
                raise ValueError(
                    f"loss_db.splitter 1:{n} is {loss:g} dB; a real one loses between "
                    f"{ideal:.1f} dB (the ideal split) and {ideal + 4:.1f} dB"
                )
            if loss <= last:
                raise ValueError("loss_db.splitter must lose more with every larger split")
            last = loss
        for nm, a in self.fiber_per_km.items():
            if not 1260 <= nm <= 1625:
                raise ValueError(f"loss_db.fiber_per_km has {nm} nm, outside the 1260–1625 nm band")
            if not 0.15 <= a <= 0.6:
                raise ValueError(
                    f"loss_db.fiber_per_km at {nm} nm is {a:g} dB/km; single-mode fibre is "
                    "0.15–0.6 dB/km (ITU-T G.652)"
                )
        return self


class ConnectorsPerPath(PriceModel):
    odf: int = 2
    fdh_in: int = 1
    fdh_out: int = 1
    fat: int = 1
    ont: int = 1


class Pops(StrictModel):
    max_units: int = Field(
        4096,
        ge=1,
        description="Units one POP (OLT site) serves; an area with more gets several POPs, each with its own catchment",
    )


class Fdh(StrictModel):
    capacities: list[int] = Field([288, 576], description="FDH sizes in splitter output ports")
    max_fill_pct: float = Field(80.0, gt=0, le=100, description="Max initial fill of an FDH")
    placements: list[Placement] = Field(["cabinet"])

    @model_validator(mode="after")
    def _sizes(self) -> Fdh:
        _ascending("fdh.capacities", self.capacities)
        return self


class Fat(StrictModel):
    max_homes: int = Field(32, ge=1, le=256, description="Max homes (units) served by one FAT")
    min_homes: int = Field(4, ge=0, description="Below this an FAT is discouraged (cost only)")
    placements: list[Placement] = Field(["pole", "wall", "chamber"])

    @model_validator(mode="after")
    def _order(self) -> Fat:
        if self.min_homes > self.max_homes:
            raise ValueError(f"fat.min_homes {self.min_homes} is above max_homes {self.max_homes}")
        return self


class Chambers(StrictModel):
    spacing_m: float = Field(250.0, gt=0, le=5000)
    types: dict[str, str] = Field(default_factory=lambda: {"small": "600x450", "large": "1200x600"})


class Closures(StrictModel):
    sizes: list[int] = Field([48, 96, 288])

    @model_validator(mode="after")
    def _sizes(self) -> Closures:
        _ascending("closures.sizes", self.sizes)
        return self


class Slack(PriceModel):
    per_closure_m: float = 10.0
    per_fat_m: float = 5.0


class Ducts(StrictModel):
    prefer_existing: bool = True
    max_fill_pct: float = Field(40.0, gt=0, le=100)
    min_bend_radius_mm: float = Field(300.0, gt=0)
    microduct_bundles: list[int] = Field([7, 12, 24])


class SurfaceCost(PriceModel):
    trench_per_m: float
    reinstatement_per_m: float = 0.0


class Routing(StrictModel):
    exclude_highways: list[str] = Field(
        [
            "motorway",
            "motorway_link",
            "trunk",
            "trunk_link",
            "steps",
            "escalator",
            "elevator",
            "corridor",
            "platform",
            "raceway",
            "bus_guideway",
            "proposed",
            "construction",
            "abandoned",
        ],
        description="OSM highway classes no cable may follow: you cannot trench a motorway or a staircase",
    )


class Surfaces(StrictModel):
    asphalt: SurfaceCost = SurfaceCost(trench_per_m=90.0, reinstatement_per_m=35.0)
    pavement: SurfaceCost = SurfaceCost(trench_per_m=60.0, reinstatement_per_m=20.0)
    verge: SurfaceCost = SurfaceCost(trench_per_m=35.0, reinstatement_per_m=5.0)
    aerial: SurfaceCost = SurfaceCost(trench_per_m=20.0)
    facade: SurfaceCost = Field(
        SurfaceCost(trench_per_m=30.0),
        description="Cable clipped along building fronts, with its wall fixings, per metre",
    )
    existing_duct: SurfaceCost = SurfaceCost(trench_per_m=5.0)

    def cost(self, surface: SurfaceClass) -> float:
        sc: SurfaceCost = getattr(self, surface)
        return sc.trench_per_m + sc.reinstatement_per_m


class Aerial(PriceModel):
    """Cable strung on existing poles (the imported pole layer)."""

    enabled: bool = Field(True, description="Offer aerial spans where the data has poles")
    max_span_m: float = Field(50.0, gt=0, description="Longest span between two poles")
    pole_attach: float = Field(
        45.0, description="Attaching to one pole: clamp, bracket and the owner's fee"
    )
    transition: float = Field(
        250.0, description="Taking the cable down a pole into the ground: guard and pit"
    )


class Facade(PriceModel):
    """Cable along building fronts: where a street is lined with buildings the cable runs on
    their walls instead of in a trench."""

    enabled: bool = Field(
        False, description="Offer facade routes (needs the owners' consent; off unless allowed)"
    )
    max_offset_m: float = Field(
        6.0, gt=0, description="A wall counts as lining the street within this distance"
    )
    min_cover: float = Field(
        0.8, gt=0, le=1, description="Share of a stretch of street that must be lined by walls"
    )
    highways: list[str] = Field(
        ["residential", "living_street", "unclassified", "service", "tertiary", "pedestrian"],
        description="Street classes where facade cabling is allowed",
    )
    transition: float = Field(
        300.0, description="Taking the cable down a wall into the ground: guard and pit"
    )


class BarrierCrossing(PriceModel):
    fixed: float = Field(
        description="Permit, launch and reception pits, mobilisation: once per crossing"
    )
    per_m: float = Field(description="Directional drilling under the barrier, per metre")


class Crossings(PriceModel):
    road_drill_per_m: float = 180.0
    max_length_m: float = Field(
        80.0,
        description="Longest bore the engine offers under a motorway, railway or river; longer gaps are left to the detour",
    )
    motorway: BarrierCrossing = BarrierCrossing(fixed=12000.0, per_m=350.0)
    railway: BarrierCrossing = BarrierCrossing(fixed=18000.0, per_m=450.0)
    water: BarrierCrossing = BarrierCrossing(fixed=9000.0, per_m=300.0)
    unmapped: BarrierCrossing = Field(
        BarrierCrossing(fixed=12000.0, per_m=400.0),
        description="An obstacle the map does not show (streets close together that do not join); priced cautiously",
    )

    def cost(self, barrier: str, length_m: float) -> float:
        c: BarrierCrossing = getattr(self, barrier)
        return c.fixed + c.per_m * length_m


class UnitCosts(PriceModel):
    fat: float = 350.0
    fdh_288: float = 6500.0
    fdh_576: float = 9500.0
    splitter_1_32: float = 120.0
    splitter_1_4: float = 40.0
    splitter_1_8: float = 60.0
    chamber_small: float = 900.0
    chamber_large: float = 1800.0
    closure_48: float = 250.0
    closure_96: float = 350.0
    closure_288: float = 600.0
    odf_per_port: float = 25.0
    cable_per_fiber_m: float = Field(
        0.012, description="Cable cost per fibre-metre (linear approximation of the ladder)"
    )
    cable_base_per_m: float = Field(0.9, description="Cable sheath cost per metre")
    drop_per_m: float = 1.2


class Labour(PriceModel):
    splice: float = 18.0
    otdr_test: float = 12.0
    fat_install: float = 180.0
    fdh_install: float = 1200.0


class InsidePlant(PriceModel):
    """Fibre inside a building with several units: a riser from the building entry up through
    the floors, a floor box on each floor, and a cable from the box to each flat."""

    floor_height_m: float = Field(
        3.0, ge=2, le=8, description="Storey height used for the riser length"
    )
    units_per_floor: int = Field(
        4,
        description="Flats per floor, to estimate the floors of a building whose levels are unknown",
    )
    units_per_floor_box: int = Field(8, ge=1, description="Flats one floor box serves")
    riser_per_m: float = Field(2.5, description="Riser cable and its fixing, per metre")
    floor_box: float = Field(70.0, description="One floor box (splice/patch point on a floor)")
    indoor_per_unit: float = Field(
        55.0, description="Cable from the floor box to one flat with its wall outlet"
    )
    riser_install_per_floor: float = Field(
        60.0, description="Labour to install the riser, per floor"
    )

    def floors(self, units: int, levels: int | None) -> int:
        """A building's floors: its mapped levels, else estimated from its flats (at least 2)."""
        if levels and levels > 0:
            return int(levels)
        return max(2, -(-int(units) // max(1, self.units_per_floor)))


class Phasing(StrictModel):
    """Building the network in phases: cabinet areas grouped into `phases` of about equal units."""

    phases: int = Field(
        1, ge=1, le=20, description="Number of build phases (1: build it all at once)"
    )
    order: Literal["density", "distance"] = Field(
        "density",
        description="Which cabinet areas come first: density (most units per metre of cable, the "
        "best business case) or distance (nearest the POP first)",
    )


class Review(StrictModel):
    """Who checked the values. Shipped presets are drafts: confirm every value with your
    operator before a design is built. openFTTH certifies nothing."""

    status: Literal["draft", "reviewed"] = "draft"
    reviewed_by: str | None = Field(None, description="Name and role of the reviewing engineer")
    date: str | None = Field(None, description="Date of the review (YYYY-MM-DD)")
    note: str = "Draft: confirm every value with your operator before construction."

    @model_validator(mode="after")
    def _who_and_when(self) -> Review:
        if self.status == "reviewed" and not (self.reviewed_by and self.date):
            raise ValueError(
                "a reviewed rulebook names who reviewed it (reviewed_by) and when (date)"
            )
        return self


class Rulebook(StrictModel):
    version: Literal[1] = 1
    name: str
    description: str = ""
    country: str | None = Field(None, pattern=r"^[A-Z]{2}$")
    currency: str = Field("EUR", pattern=r"^[A-Z]{3}$")
    review: Review = Review()
    pon: Pon = Pon()
    fibers: Fibers = Fibers()
    homes: Homes = Homes()
    distances: Distances = Distances()
    loss_db: LossDb = LossDb()
    connectors_per_path: ConnectorsPerPath = ConnectorsPerPath()
    pops: Pops = Pops()
    fdh: Fdh = Fdh()
    fat: Fat = Fat()
    chambers: Chambers = Chambers()
    closures: Closures = Closures()
    slack: Slack = Slack()
    ducts: Ducts = Ducts()
    surfaces: Surfaces = Surfaces()
    aerial: Aerial = Aerial()
    facade: Facade = Facade()
    crossings: Crossings = Crossings()
    routing: Routing = Routing()
    highway_to_surface: dict[str, SurfaceClass] = Field(
        default_factory=lambda: {
            "motorway": "asphalt",
            "trunk": "asphalt",
            "primary": "asphalt",
            "secondary": "asphalt",
            "tertiary": "pavement",
            "residential": "pavement",
            "living_street": "pavement",
            "unclassified": "verge",
            "service": "verge",
            "footway": "pavement",
            "cycleway": "verge",
            "path": "verge",
            "pedestrian": "pavement",
            "track": "verge",
        },
        description="Where the trench goes for each OSM highway class (the footpath beside "
        "a residential road, the verge beside a track, …)",
    )
    units: UnitCosts = UnitCosts()
    labour: Labour = Labour()
    inside: InsidePlant = InsidePlant()
    phasing: Phasing = Phasing()

    @model_validator(mode="after")
    def _consistent(self) -> Rulebook:
        if self.pon.arch() != "p2p":
            missing = [s for s in self.cascade_stages() if s not in self.loss_db.splitter]
            if missing:
                raise ValueError(
                    f"loss_db.splitter has no loss for 1:{missing[0]}, a split the design uses"
                )
            if self.fat.max_homes > self.pon.split_ratio:
                raise ValueError(
                    f"fat.max_homes {self.fat.max_homes} is more than one PON "
                    f"(1:{self.pon.split_ratio}) can serve"
                )
        return self

    @model_validator(mode="after")
    def _budget_fits_class(self) -> Rulebook:
        nm = self.pon.worst_case_nm
        if nm not in self.loss_db.fiber_per_km:
            raise ValueError(f"loss_db.fiber_per_km has no attenuation for {nm} nm (worst_case_nm)")
        lo, hi = CLASS_LOSS[self.pon.pon_class]
        if self.pon.budget_db > hi:
            raise ValueError(
                f"budget_db {self.pon.budget_db:g} dB is above class {self.pon.pon_class} "
                f"({hi:g} dB); the optics cannot reach it"
            )
        if not lo <= self.pon.min_loss_db < self.pon.budget_db:
            raise ValueError(
                f"min_loss_db {self.pon.min_loss_db:g} dB must be at least the class minimum "
                f"({lo:g} dB) and below budget_db ({self.pon.budget_db:g} dB)"
            )
        return self

    # ---- derived helpers used by solvers/validators -------------------------------------

    def coexistence_loss(self) -> float:
        """Loss of the coexistence element when GPON and XGS-PON share the ODN (else none)."""
        return self.loss_db.coexistence if self.pon.technology == "gpon+xgspon" else 0.0

    def splitter_loss(self) -> float:
        """Total splitter loss on one path, from the cascade or the single split ratio (none for
        point-to-point)."""
        if self.pon.arch() == "p2p":
            return 0.0
        stages = self.cascade_stages()
        return sum(self.loss_db.splitter[s] for s in stages)

    def budget_db(self) -> float:
        """The loss budget a path is checked against: the PON class, or the p2p optics."""
        return self.pon.p2p_budget_db if self.pon.arch() == "p2p" else self.pon.budget_db

    def fat_splitters(self, units: int) -> int:
        """Second-stage splitters a FAT needs in a cascaded design (none otherwise)."""
        if self.pon.arch() != "cascaded" or units <= 0:
            return 0
        return -(-units // self.cascade_stages()[1])

    def cascade_stages(self) -> list[int]:
        return list(self.pon.cascade) if self.pon.cascade else [self.pon.split_ratio]

    def connectors_on_path(self) -> int:
        c = self.connectors_per_path
        return c.odf + c.fdh_in + c.fdh_out + c.fat + c.ont

    def fibers_needed(self, demand: int) -> int:
        """Demand plus the spare percentage, rounded up."""
        return demand + max(0, int(-(-demand * self.fibers.spare_pct // 100)))

    def ladder_size(self, ladder: list[int], demand: int) -> int:
        """Smallest ladder size covering demand plus spare; the largest size if none does."""
        need = self.fibers_needed(demand)
        for size in sorted(ladder):
            if size >= need:
                return size
        return max(ladder)

    def ladder_cables(self, ladder: list[int], demand: int) -> tuple[int, int]:
        """(cable count, fibres per cable) so that count × fibres ≥ demand + spare, using
        parallel cables of equal size when one cable of the largest ladder size is not enough."""
        need = self.fibers_needed(demand)
        biggest = max(ladder)
        count = max(1, -(-need // biggest))
        per_cable = -(-need // count)
        return count, self.ladder_size(
            ladder, per_cable - max(0, int(-(-per_cable * self.fibers.spare_pct // 100)))
        )
