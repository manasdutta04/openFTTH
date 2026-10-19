# Field tests — 2026-10-18

The engine was run the way an operator trials a planning tool: real neighbourhoods of very
different character, straight from OpenStreetMap, no hand-tuning, default `generic-gpon`
rulebook, 20 s CP-SAT limit per cell. Two areas then went through the agent crew end to end.

Areas live in `fixtures/field/` (polygons committed; extracts re-fetched by
`fixtures/field/scripts/fetch.py` into the git-ignored `raw/`). `python tools/field_run.py`
reproduces the table.

## Areas

| Area | Character | Why |
|---|---|---|
| berlin-pberg | Pre-war perimeter blocks, 5-storey apartment buildings | MDU-heavy demand, indoor FATs, big cables |
| amsterdam-jordaan | Canal city, narrow streets, water on every side | crossings, disconnected street fragments |
| manchester-chorlton | British terraces with back alleys | many small plots, alley routing |
| phoenix-grid | US grid suburb, large lots | long drops, low density |
| lillehammer-edge | Norwegian town edge, detached houses, slopes | sparse demand, long distribution |
| bangalore-indiranagar | Indian neighbourhood | thin building data, fragmented street graph |
| lochristi-village | Flemish village, ribbon development | ribbon roads, few intersections |
| gent, gent-small | the original fixture | regression baseline |

## Results

Before = engine as committed that morning; after = with the fixes below. Cost is the BOQ per
unit in EUR under the generic rulebook (new trench for every metre, no address register), so
treat it as a relative figure.

| Area | Homes / units | FATs | FDHs | Time | Before | After |
|---|---|---|---|---|---|---|
| berlin-pberg | 474 / 8,469 | 343 | 20 | 80 s | 0 violations, 358/unit | unchanged |
| amsterdam-jordaan | 1,858 / 8,853 | 304 | 20 | 143 s | 7 × FDH→FAT over 2 km, 418/unit; 180 parallel sections | 0 violations, 413/unit; 60 parallel sections |
| manchester-chorlton | 432 / 562 | 40 | 2 | 52 s | **crash** (POP on a dropped fragment) | 0 violations, 1,719/unit |
| phoenix-grid | 267 / 267 | 30 | 1 | 44 s | 0 violations, 2,643/unit | unchanged |
| lillehammer-edge | 492 / 1,508 | 113 | 5 | 74 s | 12 × FDH→FAT over 2 km, 1,036/unit | 0 violations, 1,001/unit |
| bangalore-indiranagar | 1,951 / 2,932 | 157 | 8 | 153 s | **crash** (POP on a dropped fragment) | 0 violations, 1,197/unit |
| lochristi-village | 927 / 1,237 | 99 | 5 | 81 s | 0 violations, 1,693/unit | unchanged |
| gent | 1,334 / 1,636 | 94 | 5 | 100 s | 33 × FDH→FAT over 2 km, 1,441/unit | 0 violations, 1,484/unit |
| gent-small | 524 / 597 | 36 | 2 | 65 s | 3 drops over 150 m, 1,283/unit | same 3 (homes far from any street; reported by the twin) |

Every run is deterministic and every tool call is a version; the worst ODN loss stayed between
21.6 and 23.9 dB of the 28 dB class B+ budget in all nine areas.

## Faults found

Ranked by what they would cost an operator. Fixed items carry a regression test.

1. **Crash: POP placed on a street fragment the router had discarded** (Manchester, Bangalore;
   any area whose OSM extract has disconnected service roads near the centroid). The twin keeps
   only the largest connected street component but its nearest-node index still knew every
   node. *Fixed:* the index covers routable nodes only. Test: `test_nearest_node_ignores_street_fragments_outside_the_main_graph`.
2. **Invalid designs delivered: FDH→FAT paths over `max_dist_m`** (Gent 33, Lillehammer 12,
   Amsterdam 7). Cabinet clustering prices distance to the nearest candidate and never rechecks
   the street path. *Fixed:* after clustering, every FAT's street distance to its cabinet is
   measured; FATs out of reach move to the nearest cabinet with room, or are reported as
   stranded in the tool result. Gent and Lillehammer now pass with one more cabinet each.
   Tests: `test_repair_reach_*`.
3. **Cables could follow anything tagged `highway`**: motorways, trunk roads, staircases,
   planned roads. Berlin's distribution ran along the S-Bahn ring road; a footbridge of steps
   counted as a trench. *Fixed:* new rulebook section `routing.exclude_highways` with sane
   defaults; excluded ways never enter the route graph and the twin records which classes it
   left out. Test: `test_excluded_highway_classes_never_enter_the_route_graph`.
4. **Parallel cables everywhere in dense areas** (Berlin 168 of 502 distribution sections,
   Amsterdam 180 of 398). The generic distribution ladder stopped at 96 F while a cabinet feeds
   460 units through one trunk section. *Changed:* the generic ladder now goes to 144 and 288 F,
   as real dense-urban designs do. Belgium and Norway keep their own ladders.
5. **Tiny cabinets** (Lochristi: FDHs serving 16 and 51 units). The clustering reach for
   cabinets is 60 % of `max_dist_m` to leave room for the tree detour, which strands small
   clusters at the edge. Not fixed: needs a merge pass that allows a longer path when the real
   tree stays under the limit. Noted for the solver backlog.
6. **Tiny FATs** (Berlin 21, Lillehammer 31 FATs under 4 units). The `fat.min_homes` penalty is
   soft and loses to the drop-length budget on scattered buildings. Not fixed; an operator would
   serve those from a neighbour FAT with a longer drop or skip them.
7. **Homes farther than `max_drop_m` from any street vertex** (gent-small: 3). These are
   buildings set deep in plots; the twin reports them at build time and the check flags them.
   Correct behaviour, but the crew should say so in its summary rather than only in the log.
8. **Unit counts are estimates** (see the next section): Berlin's 8,469 units for 474 buildings
   decides the whole cable plan and rests on `building:levels` and 90 m² per flat.
9. **Field-test tooling**: the batch runner could not print the engine's box-drawing output on a
   Windows console and lost a crash report. Fixed in `tools/field_run.py`.
10. **Rebuilding the twin undid the design** (found 2026-10-19 while moving a cabinet by hand
    on the Manchester project). The Surveyor had run `import_osm` and `build_twin` once more
    after the solve; the rebuilt homes came back without their FAT, PON, drop and path
    lengths, so the next `route_drops` would have produced nothing. *Fixed:* a rebuilt home
    keeps what the solvers put on it, and `route_drops` recovers lost links from the drops on
    record. Test: `test_exclude_building_removes_its_homes_and_survives_a_twin_rebuild`.
11. **One distribution section always "undersized"** (every area; the Planner re-ran
    `size_cables` and `assign_fibers` after the warning, to no effect). The route tree summed
    demand per terminal node with a dict comprehension, so two FATs on one node, an apartment
    building's dedicated indoor FATs, kept only the last one's units. *Fixed:* demands are
    summed per node in both route trees; the Manchester warning is gone.

## The crew on real areas

Both runs start from a purged project with one message: "Design this area with the current
rulebook: import the data, build the twin, place POP, FDHs and FATs, route everything,
validate and give me the BOQ." Engine: Claude CLI. Transcripts in `runs/field/crew-*.json`.

### gent-small, before the crew fixes

475 s, $0.56, 26 tool calls, no tool errors. What went right:

- The Planner refused to start solving on an empty twin, read the rulebook back to the user,
  and handed the import to the Surveyor. The Surveyor imported, built the twin, and reported
  every assumption from the tool results (synthetic homes, no address register).
- The Planner ran the ten solver tools in the prescribed order and quoted each summary.
- The Checker validated independently and reported the three drop violations with element ids.
- Nobody wrote a number that a tool had not returned.

What went wrong, and what changed because of it:

1. **The chain stopped before the BOQ.** Three homes sit farther from any street than
   `max_drop_m`; no FAT placement can fix that, but the check reported them as errors, so the
   Planner (correctly, per its rules) stopped and asked the user. An operator treats these as
   exceptions to accept at sign-off. *Fixed:* such drops are now warnings with an explanation;
   errors still block, warnings travel to the Drafter and the engineer. The BOQ tab shows them
   as "exceptions to accept". Tests in `test_checks.py`.
2. **Two hand-offs wasted on version ids.** `check_rules` commits its own version, so the
   Planner suspected the Checker had validated a different design and the two crew members spent two
   turns (about $0.20) agreeing that nothing was wrong. *Fixed:* the check's result now names the
   design version it looked at, and both crew members' instructions state the rule.
3. **The hand-off limit (6) was reached** before the Drafter ever spoke; a full design needs
   five hops without any discussion. *Changed:* default limit 10.
4. The Surveyor spent four calls reading project files with Glob/Read/Grep before importing.
   Harmless, but a sign the crew member did not trust the empty `design_summary`; left as is.

### manchester-chorlton, attempts two and three

The first Manchester attempt never imported anything: the Surveyor looked for the area and
map files in the project folder, did not find them (they live wherever the user keeps them),
asked the user, and the room then bounced "we are waiting for you" between three crew members for
ten hops, $0.43. Two fixes: every crew member's prompt now carries the project's inputs, and the room
stops a chain when a crew member repeats itself.

The second attempt exposed the real blocker behind both earlier runs. The Planner, now
knowing the paths, called `import_osm` itself. That tool is the Surveyor's, so the CLI asked
for permission, the proxy opened an approval card, and the card waited for an engineer who
was not there. After five minutes Node's default request timeout cut the connection and the
crew member reported "harness unreachable: fetch failed", twice, 659 s for nothing. The same thing
had silently cost the Surveyor's Glob calls earlier. Fixes:

- Only the four risky engine tools open a card. Read-only CLI tools are allowed at once; an
  engine tool that belongs to a teammate is refused with "hand it to @Surveyor"; anything
  else is refused. No crew member can wake the engineer for a step that is not theirs.
- The HTTP server no longer times out a held request, and the proxy waits as long as the
  configured approval timeout (15 min).
- The project prompt says that only the Surveyor imports.

Third attempt, with all fixes: **147 s, $0.20, 26 tool calls, no errors, deliverables
written.** Planner handed the import to the Surveyor with the right paths; Surveyor imported
and built the twin (432 homes, 562 units, 7 apartment buildings) and reported the synthetic
homes assumption; Planner ran the ten solvers and summarised each; Checker found no errors and
no warnings (worst ODN loss 22.12 dB of 28); Drafter produced the BOQ (849,072 EUR, 1,511 per
unit), splice schedule and GeoPackage and said explicitly that it had not approved anything.
Every figure in the transcript appears in a tool result. One small waste remains: the Planner
re-ran `size_cables` and `assign_fibers` once more after reading its own warning.

### What the crew can and cannot do unattended today

Can: take an area and a map extract from nothing to a validated, priced, exported design with
every decision recorded, and stop at the right moments (a violation only the engineer can
accept, a risky tool). Cannot: judge whether the inputs are good enough (see the next section),
or sign off. Both are the engineer's job by design.

## What an operator would still need before letting the crew run unattended

Everything below is a gap between "the crew designs a plausible network" and "nobody checks the
design by hand". None of it is a solver bug; it is input the engine does not have yet.

1. **Addresses, not footprints.** Every area here counts homes from OpenStreetMap building
   polygons, with flats estimated from floor area. Berlin came out at 8,469 units for 474
   buildings; Amsterdam at 8,853 for 1,858. Both are guesses. The address register (one unit per
   dwelling number) is the first input a real designer loads, and it changes FAT counts, cable
   sizes and the cost per unit directly.
2. **Existing infrastructure.** No ducts, poles, chambers or the operator's own routes are known,
   so every metre is a new trench. In practice half the route reuses something, and the cost
   model (civil works are 60–85 % of every BOQ above) is wrong by that much.
3. **Surface truth.** Trench cost comes from the OSM highway class through `highway_to_surface`.
   A real plan uses the cadastre's pavement/verge polygons and the municipality's no-dig list.
4. **Crossings and obstacles.** Railways, rivers and main roads are crossed wherever a bridge or
   road exists in OSM; there is no drilling cost, no permit cost and no "you cannot open this
   road until 2028" constraint. Amsterdam's canals and Berlin's railway are routed as if free.
5. **Aerial option.** Everything is underground. Lillehammer, Phoenix and the village would be
   pole routes in reality, at a third of the civil cost.
6. **MDU inside plant.** A building with 60 flats gets an indoor FAT and one drop; the riser,
   floor boxes and in-building cabling are not designed or priced.
7. **Phasing and take rate.** One shot for the whole polygon at 100 % take rate.

The crew can be left alone with the solver once those inputs exist as tools with the same
contract as today's: ids and rule references in, numbers and recorded reasons out.
