# General-Purpose Structural Modeling via Robot Structural Analysis — Detailed Plan

## Purpose

Move from "can build a portal-frame variant" to "can build any structure a
structural engineer might describe" — including custom materials and custom
cross-section shapes that aren't in Robot's standard catalog.

This document is the spec. Implementation should proceed phase by phase,
each phase probed against the live COM server before writing code (per the
lesson learned from `IRobotSimpleCase` / `IRobotBarSectionData` /
`IRobotBarReleaseData` — Robot's actual interface signatures have repeatedly
differed from reasonable assumptions).

---

## 1. Foundational data model

Everything downstream depends on getting this taxonomy right first. A
structural model is fully described by:

| Category | Examples |
|---|---|
| **Geometry** | Nodes (points), Bars (1D members), Panels (2D shells/slabs/walls), (Solids — rare, skip initially) |
| **Materials** | Steel grade, concrete grade, timber class, or a fully custom material |
| **Cross-sections** | Catalog (HEA200), parametric (rectangle 300×500), or fully custom arbitrary shape |
| **Assignments** | Which material + section applies to which bar/panel |
| **Boundary conditions** | Supports (fixed/pinned/roller/spring), bar-end releases |
| **Loads** | Nodal, bar (point/UDL/trapezoidal), area (on panels), self-weight, thermal, settlement |
| **Load cases & combinations** | Individual cases, and code-based combination rules (1.2D+1.6L, etc.) |
| **Meshing** | Panel mesh density/type (for FE plate/shell analysis) |
| **Analysis type** | Linear static, modal, response spectrum, P-delta, staged construction |
| **Results** | Forces, reactions, displacements, mode shapes, utilization ratios |

The long-term architecture goal (from the earlier discussion) is a **JSON
schema** covering these categories, translated by one layer into Robot COM
calls — rather than one bespoke Python tool per feature. This plan describes
what that schema needs to cover and in what build order.

---

## 2. Step-by-step build sequence (the actual model-creation pipeline)

This is the order operations must happen in, regardless of how simple or
complex the structure is — it mirrors how Robot itself expects a model to
be assembled via COM.

1. **Set up project & units.** Choose analysis type (2D frame / 3D frame /
   shell / mixed), confirm unit system (kN, m, °C) is consistent across every
   subsequent call — unit mismatches are a silent-error risk, not a crash.
2. **Define materials.** Catalog materials first (steel S235/S355, concrete
   C25/30, etc.); custom materials defined explicitly (Section 4 below).
   Materials must exist *before* sections/assignments reference them.
3. **Define cross-sections.** Catalog, parametric, or fully custom shapes
   (Section 5 below). Sections reference a material (or are assigned one at
   the bar-assignment step, depending on the interface).
4. **Create geometry.** Nodes first, then bars (referencing node pairs) and
   panels (referencing node loops/boundaries).
5. **Assign materials & sections to elements.** Every bar/panel needs both,
   or Robot will use whatever default is active — a likely source of silent
   incorrect results if skipped.
6. **Define supports.** Node-level boundary conditions (fixed/pinned/roller/
   spring).
7. **Define bar-end releases,** where connections aren't fully rigid
   (already built, per the earlier `modify_bar_release` work).
8. **Define load cases.** Dead, live, wind, seismic, etc. as separate cases
   before applying any loads.
9. **Apply loads** — nodal, bar, area, self-weight (auto or manual),
   thermal, settlement — to the appropriate case.
10. **Define combinations.** Code-based rules combining cases with load
    factors (this is currently missing from the app — flagged last time).
11. **Mesh panels**, if any exist, before solving.
12. **Solve** (static and/or dynamic/modal/response-spectrum as required).
13. **Retrieve & validate results** — forces, reactions, displacements —
    against expectations (sanity-check against hand calc where possible).
14. **Run code checks** — utilization ratios per member/panel against the
    assigned design code (steel/RC/timber) — this is what turns "a model"
    into "a design."
15. **Iterate** — modify sections/supports/releases/loads, re-solve, compare
    (the `store_result`/`modify_*` machinery already built supports this).
16. **Report** — customizable output (Phase 3 from the earlier plan, not yet
    started).

Steps 1–7 are "build the structure." Steps 8–10 are "define what it must
resist." Steps 11–14 are "find out if it works." Step 15 is where the app's
value compounds over a single Robot session.

---

## 3. Geometry: beyond frames

Current state: nodes + straight bars only, frame-shaped.

To model "any structure," geometry primitives need to expand to:

- **Panels/shells** for slabs, walls, shear walls, foundations — defined by
  a boundary polygon of existing nodes, with a thickness and material.
  These require **meshing** before solving (Robot auto-meshes, but mesh
  density/type is a parameter worth exposing).
- **Openings** in panels (doors, windows, service penetrations) — polygon
  cutouts within a panel boundary.
- **Curved/arc bars** — for arches, curved beams — Robot supports these but
  the node-pair-only bar model won't; needs a bar-with-curvature primitive.
- **Rigid links / diaphragms** — connecting nodes that must move together
  (e.g., a floor diaphragm) without being a physical beam.
- **Coordinate systems** — local vs global, especially important once panels
  and curved members are involved (orientation of local axes affects load
  direction and result interpretation).

Recommended build order: panels/shells first (highest value — most real
buildings have slabs/walls), then openings, then rigid links, then curved
bars (lowest frequency of need for most structural work).

---

## 4. Custom materials — detailed discussion

### 4.1 What Robot needs to define a material

Robot's material object (`IRobotMaterialData`, reached the same way sections
and supports were — via `label.Data` cast to the correct interface) needs
at minimum:

- **Name** (unique label)
- **Type flag** — steel / concrete / timber / aluminum / other. **This
  matters more than it looks**: Robot's code-check modules (steel design,
  RC design) key off this type. A material typed as "other" may not be
  eligible for automated code checking, even if all its numeric properties
  are correct. If a custom material is meant to be checked against a design
  code, it likely needs to be typed as the closest matching category, not
  left as generic/other — this needs to be confirmed by probing, not
  assumed.
- **Elastic modulus (E)**
- **Shear modulus (G)** or Poisson's ratio (Robot may derive one from the
  other, or want the ratio + one modulus — probe before implementing)
- **Density (ρ)** — needed for self-weight auto-calculation
- **Thermal expansion coefficient** — needed if thermal loads are ever used
- **Strength values** — yield strength for steel, characteristic strength
  for concrete, etc. — needed for code checks specifically, not for basic
  analysis

### 4.2 Implementation approach

1. **Probe first** (same pattern as before): confirm the exact property
   names and units `IRobotMaterialData` exposes on the live server —
   don't assume `E`/`G`/`RO`/`NU` naming without checking, since Robot's
   naming has already surprised us multiple times (`UX/UY/UZ/RX/RY/RZ` on
   releases, not `MX/MY/MZ`).
2. **Tool: `define_custom_material(name, type, E, G_or_nu, density,
   thermal_expansion, strength_value)`** — creates the label, casts to the
   correct data interface, sets each property, stores the label.
3. **Validation step (critical for materials specifically):** create a
   custom material with properties that exactly match a known catalog
   material (e.g., manually input S235 steel's real E/G/density and compare
   against Robot's own S235 catalog entry). If small discrepancies appear,
   that tells you which property Robot is deriving vs. storing directly —
   important for getting derived values (like self-weight) right.
4. **Regression test:** apply a custom material to a bar, run self-weight
   load case, confirm the resulting reaction matches `density × cross-
   section area × length × g` by hand calc. This is the single best sanity
   check that a custom material was actually wired correctly end-to-end,
   not just "accepted without error."

### 4.3 Open question to resolve during probing

Whether custom (non-catalog) materials are eligible for Robot's built-in
code-check modules at all, or whether code-checking is restricted to
catalog materials/grades. If restricted, custom materials may only be
usable for analysis (getting forces/deflections), not automated design
checking — worth knowing before promising "full code-check for any
material" as a feature.

---

## 5. Custom shapes/sections — detailed discussion

This is the more involved of the two, because Robot supports **three
distinct tiers** of section definition, each with different capability and
different COM interface requirements.

### Tier 1 — Catalog sections (already working)

`LoadFromDBase(section_name)` against `IRobotBarSectionData`. Full geometric
properties, full code-check support. No further work needed beyond what's
already built, other than validating the section name actually exists in
the currently-loaded database (the earlier "Member not found" warnings
suggest either wrong section-name formatting, or the wrong database
loaded — worth resolving as a quick-win before building custom sections,
since it affects the existing tool too).

### Tier 2 — Parametric standard shapes

Rectangle, circle, box, I-shape-by-dimensions, tapered members — Robot has
a parametric section generator (distinct from the catalog loader) that
builds a shape from dimensions rather than a database lookup. This tier is
worth building next because it covers most non-catalog cases (concrete
rectangular/circular columns, custom-dimensioned steel plates) without
needing full arbitrary geometry.

- **Tool: `define_parametric_section(name, shape_type, dimensions_dict)`**
  — shape_type ∈ {rectangle, circle, box, tapered_I, ...}; dimensions_dict
  keyed by shape (e.g., `{"width": 0.3, "height": 0.5}` for rectangle).
- **Probe first** to find the actual parametric-section creation method
  and its parameter order/units — this is a different code path from
  `LoadFromDBase` and hasn't been touched yet.
- Full geometric properties (A, I, S, Z) are computed by Robot automatically
  from the dimensions — good for code-checking, since the shape is "real"
  as far as Robot's design modules are concerned.

### Tier 3 — Fully arbitrary/custom shapes

For genuinely non-standard cross-sections (built-up sections, unusual
fabricated shapes) Robot supports **two different approaches**, which serve
different purposes:

- **(a) Geometric definition** — building the section as a closed polygon
  (or thin-walled centerline + thickness) of point coordinates. Robot
  computes section properties from the geometry. This preserves code-check
  capability (Robot can still evaluate the actual shape against a design
  code) but is the most complex to implement and validate.
- **(b) Direct property input** — skip geometry entirely and directly input
  A, Iyy, Izz, J, section moduli as numbers. Fast to implement, but **loses
  code-check capability** in most cases (Robot's design modules typically
  need the actual shape, not just derived properties) and is only suitable
  when the user just needs correct analysis forces/deflections, not an
  automated code check on that specific member.

Recommended approach: implement (b) first as a fast, low-risk "advanced"
tool for engineers who already know their section properties (common when
importing properties from another software or a fabricator's spec sheet) —
clearly documented as "analysis-only, no automated code check." Implement
(a) as a later phase once there's real demand, since it's substantially
more complex and needs careful geometric validation.

- **Tool: `define_custom_section_by_properties(name, area, Iyy, Izz, J,
  Wy_elastic, Wz_elastic, ...)`** for tier 3(a).
- **Tool: `define_custom_section_by_geometry(name, points, thickness)`**
  for tier 3(b) — deferred.

### 5.1 Validation strategy specific to sections

For every tier, validate against a known reference before trusting it in a
real model:

- **Tier 2 (parametric):** build a rectangle 300×500mm parametrically,
  compare Robot's computed `A` (should be exactly 0.15 m²) and `I` (bh³/12)
  against hand calc.
- **Tier 3(a) (direct properties):** input known properties for a standard
  catalog section (e.g., manually enter HEA200's real A/I values as a
  "custom" section) and confirm a simple beam deflection matches the result
  you get using the real catalog HEA200 — if they match, the direct-
  property path is wired correctly.
- **Tier 3(b) (geometry, deferred):** validate against a simple known shape
  first (a plain rectangle defined by 4 points) before trusting complex
  fabricated shapes, since this is the path most likely to have unit or
  winding-order (clockwise vs counterclockwise) bugs.

---

## 6. Testing & validation strategy (applies across all of the above)

Given how many "assumed interface ≠ actual interface" surprises this app
has already hit, every new capability in this plan should follow the same
sequence used successfully for releases:

1. **Probe** the real interface/method signature against the live COM
   server before writing the implementation.
2. **Implement** the minimum tool needed.
3. **Read back** what was just written (don't just check the call didn't
   throw — confirm the value actually persisted correctly).
4. **Validate against a known answer** — hand calc, catalog equivalent, or
   textbook case — not just "ran without error."
5. **Regression test** — add it to a growing suite so future changes don't
   silently break it.

---

## 7. Suggested phased implementation order

Building on the phases already completed (element modification, result
store):

| Phase | Scope | Depends on |
|---|---|---|
| 3 | Customizable report sections (already planned) | — |
| 4 | Code-check / utilization ratio access | — |
| 5 | Load combinations (first-class object) | — |
| 6 | Parametric sections (Tier 2) | Section 5 above |
| 7 | Custom materials | Section 4 above |
| 8 | Custom sections by direct properties (Tier 3a) | Phase 6 |
| 9 | Panels/shells + area loads + meshing | New geometry primitive |
| 10 | Algorithmic optimizer loop (weight/cost minimization under code constraints) | Phases 4–8 |
| 11 | Custom sections by geometry (Tier 3b) | Phase 8, real demand |
| 12 | Schema-driven translator (architectural refactor) | Once enough tool coverage exists to know what the schema must express |

Phases 4 and 5 are prioritized early because they're what makes
"optimization" and "comparison" mean anything real (pass/fail against code,
not just lighter/heavier). Custom materials/sections (6–8) come next because
they're the direct subject of this plan and don't depend on the geometry
expansion. Panels/shells (9) is deliberately later — it's valuable but is
the single largest scope item, and the frame-based tooling should be fully
solid first.

---

## 8. Open questions to resolve via probing before committing to this order

- Does Robot's code-check module accept custom (non-catalog) materials?
- What is the actual parametric-section creation API (method name, dims
  order, units)?
- Are load combinations exposed via a distinct interface
  (`IRobotCaseCombination` or similar), and does combination-factor
  application happen automatically on solve, or need explicit triggering?
- What panel/shell meshing controls are exposed via COM vs. only available
  through Robot's GUI?

These four should be probed early — ideally before finalizing Phase 4–9
scope — since the answers materially affect how much work each phase is.
