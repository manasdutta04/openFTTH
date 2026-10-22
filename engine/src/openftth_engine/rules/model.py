"""The design rulebook: every number a solver or validator may use.

A rulebook is a YAML file validated by this model. `rulebooks/schema.json` is generated
from it (`openftth-engine schema`). Every preset value carries a provenance marker in the
YAML (`# source:` or `# assumed`), see docs/RULEBOOK.md.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

PonClass = Literal["B+", "C+"]
SurfaceClass = Literal["asphalt", "pavement", "verge", "aerial", "existing_duct"]
Placement = Literal["pole", "wall", "chamber", "cabinet"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Pon(StrictModel):
    pon_class: PonClass = Field("B+", alias="class", description="ITU-T G.984.2 ODN class")
    budget_db: float = Field(28.0, description="Max ODN loss for the class (B+ = 28 dB)")
    min_loss_db: float = Field(13.0, description="Min ODN loss for the class (B+ = 13 dB)")
    max_differential_db: float = Field(
        15.0, description="Max difference in loss between ONTs on one PON"
    )
    split_ratio: int = Field(32, description="Total split per PON (1:N)")
    cascade: list[int] | None = Field(
        None,
        description="Cascaded split stages, e.g. [4, 8]. None means a single centralised "
        "splitter in the FDH.",
    )
    worst_case_nm: Literal[1310, 1490, 1550] = Field(
        1310, description="Wavelength used for the worst-case budget check"
    )
    architecture: Literal["centralised", "cascaded", "p2p"] | None = Field(
        None,
        description="centralised: one splitter per PON in the FDH; cascaded: `cascade` stages, the "
        "first in the FDH and the second in each FAT; p2p: no splitters, one fibre per unit from "
        "the POP. Left out, it follows `cascade`.",
    )
    p2p_budget_db: float = Field(
        14.0,
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
    def _cascade_matches_ratio(self) -> Pon:
        if self.cascade:
            prod = 1
            for s in self.cascade:
                prod *= s
            if prod != self.split_ratio:
                raise ValueError(f"cascade {self.cascade} does not multiply to {self.split_ratio}")
        return self


class Fibers(StrictModel):
    drop: int = Field(2, description="Fibres per drop cable")
    distribution: list[int] = Field(
        [12, 24, 48, 96, 144, 288], description="Distribution cable ladder"
    )
    feeder: list[int] = Field([96, 144, 288], description="Feeder cable ladder")
    spare_pct: float = Field(20.0, description="Spare fibres as % of demand, rounded up to ladder")
    take_rate: float = Field(1.0, description="Share of homes passed that get a drop (0–1)")


class Homes(StrictModel):
    count_units: bool = Field(
        True, description="Count dwelling units (busnummers / flats), not addresses"
    )
    default_units_apartment: int = Field(
        6, description="Units assumed for an apartment building with no unit data"
    )
    m2_per_unit: float = Field(
        90.0, description="Floor area per unit used to estimate units from footprint × levels"
    )


class Distances(StrictModel):
    max_drop_m: float = Field(150.0, description="Max drop length home → FAT along the street")
    max_dist_m: float = Field(2000.0, description="Max distribution cable length FDH → FAT")
    max_reach_km: float = Field(20.0, description="Max logical reach OLT → ONT")
    max_pull_m: float = Field(250.0, description="Max pull between chambers")


class LossDb(StrictModel):
    connector: float = 0.3
    splice: float = 0.1
    margin: float = Field(2.0, description="Design margin added to every path")
    splitter: dict[int, float] = Field(
        default_factory=lambda: {2: 3.7, 4: 7.2, 8: 10.5, 16: 13.8, 32: 17.1, 64: 20.5}
    )
    fiber_per_km: dict[int, float] = Field(
        default_factory=lambda: {1310: 0.35, 1490: 0.25, 1550: 0.22}
    )


class ConnectorsPerPath(StrictModel):
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
    max_fill_pct: float = Field(80.0, description="Max initial fill of an FDH")
    placements: list[Placement] = Field(["cabinet"])


class Fat(StrictModel):
    max_homes: int = Field(32, description="Max homes (units) served by one FAT")
    min_homes: int = Field(4, description="Below this an FAT is discouraged (cost only)")
    placements: list[Placement] = Field(["pole", "wall", "chamber"])


class Chambers(StrictModel):
    spacing_m: float = 250.0
    types: dict[str, str] = Field(default_factory=lambda: {"small": "600x450", "large": "1200x600"})


class Closures(StrictModel):
    sizes: list[int] = Field([48, 96, 288])


class Slack(StrictModel):
    per_closure_m: float = 10.0
    per_fat_m: float = 5.0


class Ducts(StrictModel):
    prefer_existing: bool = True
    max_fill_pct: float = 40.0
    min_bend_radius_mm: float = 300.0
    microduct_bundles: list[int] = Field([7, 12, 24])


class SurfaceCost(StrictModel):
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
    existing_duct: SurfaceCost = SurfaceCost(trench_per_m=5.0)

    def cost(self, surface: SurfaceClass) -> float:
        sc: SurfaceCost = getattr(self, surface)
        return sc.trench_per_m + sc.reinstatement_per_m


class BarrierCrossing(StrictModel):
    fixed: float = Field(
        description="Permit, launch and reception pits, mobilisation: once per crossing"
    )
    per_m: float = Field(description="Directional drilling under the barrier, per metre")


class Crossings(StrictModel):
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


class UnitCosts(StrictModel):
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


class Labour(StrictModel):
    splice: float = 18.0
    otdr_test: float = 12.0
    fat_install: float = 180.0
    fdh_install: float = 1200.0


class Rulebook(StrictModel):
    version: Literal[1] = 1
    name: str
    description: str = ""
    country: str | None = None
    currency: str = "EUR"
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

    # ---- derived helpers used by solvers/validators -------------------------------------

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
