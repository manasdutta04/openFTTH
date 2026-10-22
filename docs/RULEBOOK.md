# Rulebooks

A rulebook is a YAML file holding every number a solver or validator may use. The agents can
read it, explain it and change single values on the user's request; they can never bypass it.

- Schema: [`rulebooks/schema.json`](../rulebooks/schema.json), generated from
  `engine/src/openftth_engine/rules/model.py` with `openftth-engine schema --out rulebooks/schema.json`.
  `tests/test_rulebook.py` fails when the file is out of date.
- Presets: `generic-gpon` (neutral), `belgium` (Flanders draft), `norway` (draft, cascaded split).
- Validate: `uv run openftth-engine rules validate rulebooks/belgium.yaml`.

## Provenance

Every value in a preset carries a comment: `# source: …` for a public specification or
datasheet, `# assumed` otherwise. A design partner reviews the assumed values before a preset is
called anything but "draft".

## Sections

| Section | Used by | Notes |
|---|---|---|
| `pon` | validate (power budget), assign_pon | ITU-T G.984.2 class (B+ 13–28 dB, C+ 17–32 dB), split ratio, optional cascade `[4, 8]`, differential loss, worst-case wavelength (1310 nm) |
| `fibers` | size cables | ladders for distribution/feeder, drop fibres, spare %, take rate |
| `homes` | ingest | count dwelling units (not addresses); apartment estimation when no unit data |
| `distances` | cluster_homes, place_fdh, validate | max drop (street distance), max FDH→FAT, logical reach, pulling distance |
| `loss_db`, `connectors_per_path` | validate | connector/splice/splitter losses, fibre attenuation per wavelength, design margin, connectors counted on a path (ODF, FDH in/out, FAT, ONT) |
| `fdh`, `fat`, `chambers`, `closures`, `slack`, `ducts` | solve | capacities, fill limits, placements, sizes |
| `routing` | twin | OSM highway classes no cable may follow (motorways, staircases, planned roads, …); those ways never enter the route graph |
| `surfaces`, `highway_to_surface` | twin, boq | trench + reinstatement cost per metre by surface; which surface each OSM highway class trenches in |
| `crossings` | twin, solve, boq | the price of a directional drill under a `motorway`, `railway`, `water` or `unmapped` obstacle (`fixed` for the permit and pits, `per_m` for the drilling) and `max_length_m`, the longest bore offered |
| `pops` | solve | `max_units`, the units one POP (OLT site) serves; an area with more gets several POPs, each with its own catchment |
| `units`, `labour` | boq | unit prices |

## Crossings (twin, solve)

A trench cannot follow or cut across a motorway, a railway or a river. Where streets face each
other across one within `crossings.max_length_m`, the twin offers a bore as an edge priced
`fixed + per_m × length`; routing takes it only when it is cheaper than going round, and the
routing step records both prices. Motorways come from the street data, railways and rivers from
the extract. Streets that come that close but do not join within 600 m have something between
them the map does not show (an extract without water, for instance): those get an `unmapped`
crossing, recorded as an assumption to check on site. A building in the way rules a bore out,
and gaps under 8 m are joined as ordinary street: a slip in the map. Crossings used appear as
`route` elements of kind `crossing` and as permit-and-pits and drilling lines in the BOQ.

## Power budget formula (validate)

```
ODN loss = km × fiber_per_km[worst_case_nm]
         + connectors_on_path × loss_db.connector
         + splices_on_path × loss_db.splice
         + Σ splitter losses over the cascade
         + loss_db.margin
must be ≤ pon.budget_db, and within one PON max − min ≤ pon.max_differential_db
```

Splice count is derived from the path (FAT, FDH ×2, plus one per branch closure passed), not a
constant.

## Several POPs (solve)

`set_pop` places one POP while the area holds at most `pops.max_units` units, as before. Beyond
that, or when asked for a `count` or given `sites`, it places several: a weighted k-means over
the homes picks the sites (snapped to the street), and a min-cost flow gives every home to one
POP, keeping each within its capacity and the homes as close to their POP as the capacities
allow. A FAT belongs to the POP most of its units belong to; cabinets are placed within each
catchment, so none is fed from two POPs; each POP routes its own feeder tree and numbers its
own fibres. The elements carry `pop_id`, and a trace from any home ends at its own POP.

## Architectures (solve)

`pon.architecture` chooses how homes share fibre; `generate_scenarios` with
`names=["centralised", "cascaded", "point-to-point"]` prices all three on the same area.

- **centralised**: one 1:`split_ratio` splitter per PON in the FDH; whole FATs are packed into
  PONs; the distribution cable carries one fibre per unit.
- **cascaded** (`cascade: [s1, s2]`): a 1:s1 splitter per PON in the FDH and 1:s2 splitters in
  each FAT (as many as its units need). The distribution cable carries one fibre per FAT
  splitter, so it is much thinner; each path passes both splitters, and the trace shows the
  loss of each stage.
- **p2p**: no splitters. Every unit has its own fibre from the POP, patched through its FDH, so
  the feeder carries one fibre per unit and the POP needs a port per unit. Paths are checked
  against `p2p_budget_db` instead of the PON class budget.
