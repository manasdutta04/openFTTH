# openFTTH Network — plan for the operator's whole network workflow

Status: proposed 2026-10-19. Extends [PLAN.md](PLAN.md); every decision there still holds.

## 1. Goal

Operators and their contractors run their fibre networks in a telecom GIS: the network of
record. People draw designs into it command by command, redraw the contractor's as-built PDF
plans object by object, trace fibres, and print block plans and reports. openFTTH will cover
that whole workflow, with the crew doing the work and the engineer steering from the chat.

Three properties make it better than the tools it replaces:

1. **The crew does the work.** Every step is an engine tool the crew calls. People review,
   decide and sign off; they do not redraw.
2. **Nothing is unexplained.** Every object carries where it came from (solver, rulebook,
   document, survey, engineer) and why. Every change is a version with a semantic diff.
3. **It shows the network properly.** Cartographic plans by operator symbology, a 3D view of
   what is underground and on façades, and the source document beside its digitised result.

## 2. Principles carried over and extended

- **The LLM never writes a number into the network.** New for documents: numbers read from a
  PDF come from extraction tools (vector geometry, text with positions, OCR, symbol matching).
  Each extracted value keeps a pointer to its crop in the source page and a confidence. A
  vision model may classify a symbol or read a label, but its reading is evidence, checked by
  deterministic parsers and rules, never a value typed into the design.
- **Decision sources** gain `document` (read from a plan) and `survey` (field measurement)
  beside `solver`, `rulebook`, `assumption` and `user`.
- **Risky steps need approval.** Committing a work order to the network of record, deleting
  in-service objects and exporting to the operator's system join the approval list.
- **Operator rules are data.** Naming patterns, id schemes, object types, symbology and
  validation rules live in an *operator pack* (YAML, schema-checked), like the rulebooks.
- **Licences and confidentiality.** MIT code only. Operator documents and work instructions
  are never committed; test material is synthetic or explicitly released for use.

## 3. What the incumbent tools do, and what openFTTH builds

| Capability | Incumbent telecom GIS | openFTTH | Crew member |
|---|---|---|---|
| Network of record | Inventory of every duct, cable, closure, port, splice, fibre with operator ids | Network store: the design bundle extended to a full inventory model with lifecycle states | Surveyor, Recorder |
| Work orders | Design changes drawn inside a work order, posted when approved | A work order collects every change since the network of record; posting moves the record to the new version, with diff, checks and approval | Planner |
| Guided drawing commands | Operator menus: place distribution point, façade cable, cable going underground, splice pullback cables | The same steps as engine tools that take ids and rule references; the crew calls them, the engineer can trigger them from the map | Planner |
| Business rules and QA | Connectivity and data-quality checks while drawing | Rule engine from the operator pack, run on every commit, explained per object | Checker |
| Connectivity and tracing | Port-to-port, fibre-to-fibre trace from exchange to home | Connectivity graph with trace, loss budget per path, impact analysis ("which homes lose service if this closure is cut") | Checker |
| As-built intake | People redraw the contractor's PDF block plans | Document intake: the crew reads the PDF, builds the objects, compares with the design, and asks only about what it is unsure of | Recorder (new) |
| Plans and reports | Block plans, splice reports, material lists | Plan sheets with title block, legend, scale and grid; splice and port reports; BOQ and material reconciliation against as-built | Drafter |
| Integration | Imports and exports to other operator systems | Adapters: GeoPackage, GML/CSV by operator schema mapping, and an interchange format for the operator's own GIS | Drafter |

## 4. The network model

The current bundle holds a design. The network model adds what an operator keeps for decades.

- **Point objects:** exchange/POP, OLT and ODF, cabinet (FDH), closure, manhole and handhole,
  virtual manhole (where a façade cable goes underground), pole, façade distribution point,
  building distribution frame, ONU/ONT, measurement point.
- **Linear objects:** trench, duct, microduct bundle and its tubes (for example 7×12 + 2×30),
  cable (underground, façade, aerial, indoor), aerial span, façade run.
- **Connectivity:** ports, fibres and tubes with colour codes, splices, splitters, patch links.
  Every fibre path from OLT port to home is traceable.
- **Addresses:** one record per address with unit counts by category, as the operator labels
  them, linked to buildings and to the distribution point that serves them.
- **Lifecycle:** planned → in construction → as-built → in service → out of service →
  removed, with dates and the work order that changed it.
- **Provenance on every object:** source, confidence, the document crop or decision behind it.

Storage stays local first: canonical GeoJSON per layer plus tables for connectivity, versioned
in the project repository. A project grows into a *network* repository shared by many work
orders; Phase N6 adds a server mode for teams.

## 5. Workflows

Each workflow is a chain of engine tools. The crew runs it; the engineer sees progress in the
chat, answers questions there, and approves at the marked steps.

### A. Intake of what exists
Import the address register, existing inventory (operator export, GeoPackage, shapefiles),
cadastre parcels and street surfaces. The Surveyor reports gaps and conflicts between sources.

### B. Design
Today's greenfield chain, extended with façade and aerial options:
- façade runs along building fronts with façade distribution points, chosen where the rulebook
  allows (no listed buildings, minimum façade length, owner consent flags);
- underground transitions through a virtual manhole at the end of a façade;
- pole routes where poles exist;
- reuse of existing ducts before new trench.
The solver prices every option; the Checker explains each choice against the rules.

### C. Work orders
N1 keeps one work order open at a time on the project's single line of versions; the network
of record is a pointer (`refs/network/record`) to the last posted version. Parallel work orders
as real branches arrive with the team server in N6.
Every change happens in a work order. The Planner opens it, the solvers fill it, the Checker
validates it, and posting it to the network of record needs the engineer's approval.

### D. As-built from documents (the step people do by hand today)
New crew member **Recorder** owns it.

1. **Read the PDF.** Vector PDFs: extract paths, fills and text with positions. Scans: render,
   then OCR and symbol detection. Keep page, crop and confidence for every item.
2. **Georeference.** Match the title block, street names, parcel outlines and house numbers to
   cadastre and OpenStreetMap; fit the page to the map with control points; report the residual
   error.
3. **Recognise objects.** Classify symbols by the operator pack's legend (distribution point,
   manhole, closure, cable styles by colour and dash). Parse labels with the pack's id
   patterns: distribution point ids, address unit counts, duct and cable specifications.
4. **Build the network.** Snap objects to each other, infer cable runs and connectivity, and
   link addresses to their distribution points.
5. **Compare with the design.** Produce the as-designed vs as-built diff: moved, added, missing.
6. **Validate.** Run every operator rule.
7. **Review queue.** Objects below the confidence threshold or with rule errors go to the chat
   as cards: the crop, what the Recorder read, its best guess, one-click accept or correct.
   Everything else is committed automatically.
8. **Post.** With approval, the work order becomes the network of record.

### E. Trace and analysis
Trace a fibre or a port, list the homes behind a closure, power budget per path, spare
capacity per cabinet and duct, impact of an outage or a road works zone.

### F. Plans and reports
Plan sheets per block or per area: operator symbology, label placement without collisions,
north arrow, scale bar, legend, title block filled from the work order; PDF and DXF. Splice
and port reports, material lists, BOQ, and reconciliation of as-built materials against the
design.

### G. Exchange with the operator's systems
Export and import through files or interfaces the operator supports, mapped by the operator
pack. **No screen automation of third-party applications**: it is fragile and usually
forbidden by licence and security rules. Where no interface exists, openFTTH produces a
complete, validated package and the last load stays a documented manual step.

## 6. The app

- **Map:** cartographic rendering by operator symbology, scale-dependent labels, façade runs
  drawn on the building edge, dimensions and distances on demand.
- **Document view:** the source PDF beside the map, linked selection, the digitised result
  overlaid on the page with a confidence heat map.
- **Review queue:** the Recorder's open questions as cards, keyboard-driven.
- **Work orders:** list, diff, approve and post.
- **Trace view:** a schematic of the fibre path with ports, splices and loss.
- **3D:** underground ducts and chambers, façade runs on buildings, poles (Phase 3 renderer).
- **Plan sheets:** layout preview and print.

The engineer stays in the loop through the chat: they ask, correct, approve and sign off. The
crew never posts to the network of record, deletes in-service objects or exports without an
approval card.

## 7. Phases

Each phase ends at a gate that is measured, not felt.

- **N1 — Network model and work orders.** Inventory model, lifecycle, provenance, operator
  pack v1, work orders as branches, posting with approval, connectivity tables and trace.
  *Gate:* the Manchester design posted as a work order; a fibre traced from OLT port to home in
  the app; the generic operator pack validates it.
- **N2 — Façade, aerial and existing infrastructure.** Façade runs and distribution points,
  virtual manholes, poles, duct and microduct reuse, address register import.
  *Gate:* a terraced street designed with façade runs where allowed and underground elsewhere,
  every choice explained.
- **N3 — Document intake (Recorder).** Vector PDF extraction, georeferencing, symbol and label
  recognition, diff against design, review queue.
  *Gate:* on a labelled set of at least 20 block plans released for use, ≥ 95 % of objects
  recognised, ≥ 99 % of committed objects correct, under 10 % of objects sent to review.
- **N4 — Plans and reports.** Plan sheets with symbology and title blocks, PDF and DXF, splice
  and port reports, material reconciliation.
- **N5 — Exchange.** Import and export adapters per operator pack; round-trip tests.
- **N6 — Teams and scale.** Server mode for many users and work orders, permissions, audit,
  areas of 100k+ homes.

N1 and N2 build on what exists. N3 is the hardest and runs in parallel from the start of N2 as
a research track, because extraction quality decides how much of the manual work disappears.

## 8. How we know it is better

Measured per block plan on the labelled set: minutes of human time, objects corrected after
posting, rule violations found by the operator's own checks, and the share of plans posted with
no human edit. The target is that a person reviews a block in minutes instead of redrawing it in
hours.

## 9. Risks

- **Operator interfaces.** Posting into the operator's own system needs an import format or
  API from the operator. Without it, the final load stays manual.
- **Extraction accuracy.** Scans, hand annotations and inconsistent symbols will defeat some
  plans. The review queue and confidence scores keep errors out of the network of record.
- **Confidential material.** Real plans and work instructions belong to their owners. Only
  released or synthetic documents enter the repository or the test set.
- **Scope.** This is several years of incumbent product. The phases ship usable value one at a
  time; each gate decides whether the next one starts.

## 10. Needed from outside

1. A set of block plans and their expected as-built objects, released for use.
2. The operator's object types, id patterns, symbology legend and validation rules, in a form
   that can become an operator pack.
3. Whether the operator's GIS accepts an import file or interface for work orders.
