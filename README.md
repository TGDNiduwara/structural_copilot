# Structural Multi-App Agent

A unified Streamlit AI copilot that orchestrates **Autodesk Robot Structural
Analysis**, **Excel**, **Word**, **PowerPoint**, and **Matplotlib diagrams**
through a single LLM tool-calling agent. Supports **OpenAI**, **OpenRouter**,
and **Google AI Studio (Gemini)** as interchangeable LLM backends.

## Architecture

```
structural_copilot/
├── app.py                     # Streamlit UI + agent orchestration loop
├── requirements.txt
├── agent/
│   ├── llm_providers.py       # Unified OpenAI/OpenRouter/Gemini chat+tool-call client
│   └── tool_registry.py       # Tool JSON schemas + ToolExecutor dispatcher
├── tools/
│   ├── robot_tool.py          # RobotBridge — thread-safe COM bridge to Robot SA
│   ├── excel_tool.py          # ExcelReporter — formatted .xlsx generation
│   ├── diagram_tool.py        # DiagramGenerator — SFD/BMD matplotlib rendering
│   ├── word_tool.py           # WordReporter — formal .docx calculation reports
│   └── pptx_tool.py           # PowerPointReporter — .pptx presentation decks
└── generated/                 # Output artifacts land here (xlsx/docx/pptx/png)
```

### Data flow

```
User prompt
   │
   ▼
LLM (OpenAI / OpenRouter / Gemini) ── selects tool calls ──▶ ToolExecutor
   ▲                                                              │
   │  tool result / structured error (for self-correction)        │
   └──────────────────────────────────────────────────────────────┘
                                                                    │
                                          RobotBridge (COM) ◀───────┤
                                          ExcelReporter     ◀───────┤
                                          DiagramGenerator  ◀───────┤
                                          WordReporter      ◀───────┘
```

Every tool call result — success or failure — is fed back into the message
history as a `tool` role message. On failure, `ToolExecutor.dispatch` raises
`ToolExecutionError`, which `app.py`'s `_execute_with_reflection` converts
into a structured JSON error payload (with a corrective hint) instead of
crashing the app. The next LLM turn sees that error and can retry with
corrected arguments or a different call order — this is the autonomous
error-reflection loop.

## Requirements

- **Windows** with **Autodesk Robot Structural Analysis Professional**
  installed and licensed (COM server `Robot.Application` registered) — only
  required for `tools/robot_tool.py`. The Excel/Word/diagram tools are
  cross-platform and work on any OS.
- Python 3.10+
- `pip install -r requirements.txt`

## Running

```bash
streamlit run app.py
```

In the sidebar:
1. Choose your LLM provider (OpenAI / OpenRouter / Google AI Studio / Z.AI /
   DeepSeek), or pick **Custom (OpenAI-compatible)** to point the agent at
   *any* OpenAI-style endpoint (Ollama, LM Studio, vLLM, Together, Groq,
   Azure OpenAI, a corporate proxy…) by entering its base URL + model name.
2. Enter the model name (defaults are pre-filled) and your API key.
3. Optionally toggle whether the Robot application window is visible during
   automation.

## Example prompt

> "Build a 2-bay 6m frame, run 25 kN/m dead load, export data to
> 'Frame_Results.xlsx', and generate a formal design report
> 'Frame_Report.docx' with moment diagrams."

The agent will typically:
1. `new_2d_frame`
2. `create_node` × N, `create_bar` × N (assigning steel sections)
3. `set_support` at base nodes
4. `create_load_case` → `apply_bar_load` (25 kN/m dead load)
5. `solve`
6. `export_member_forces`, `export_reactions`, `export_bill_of_materials`
7. `export_to_excel` → `Frame_Results.xlsx`
8. `generate_diagrams` → SFD/BMD PNGs
9. `generate_word_report` → `Frame_Report.docx` (with embedded diagrams and
   result tables)
10. `generate_powerpoint_report` → `Frame_Presentation.pptx` (title,
    assumptions, standards, summary, result tables, diagram slides)

All generated files appear as sidebar download buttons.

## Verification without an LLM

```bash
python smoke_test.py --offline   # office artifacts from synthetic data only
python smoke_test.py             # full Robot COM round trip + artifacts
```

## Troubleshooting (Robot COM)

- **`'IRobotApplication' object has no attribute ...` / gen_py cache errors** —
  the win32com cache is stale. Delete the folder printed by
  `python -c "import win32com; print(win32com.__gen_path__)"` and restart.
- **"Robot launch circuit-breaker tripped"** — Robot failed to become ready
  twice within 90 s. It is almost certainly waiting on a splash / license /
  "select project type" dialog; open Robot manually, dismiss it, and retry.
- Liveness probes use `robot_app.Project` — `IRobotApplication` has no
  `.Name` property, and only COM transport errors count as "connection
  lost". (Previously every healthy connection was misdiagnosed as lost,
  spawning endless `robot.exe` processes.)
- **Sections / supports not applied** — the label-type enum values were
  corrected (section = 3, node support = 0; verified live). Sections are
  loaded from the Robot catalogs via `LoadFromDBase2(name, catalog)`, e.g.
  `IPE 300` from `Europe`. If a name isn't found, the tool raises a clear
  error listing tried catalogs instead of silently using a default profile.

## Known issues (live-verified 2026-08-22)

### `project: "2D"` specs produce structurally invalid frames on this build

**Symptom (verified live via `build_structure_from_spec` on a 3-bar portal
frame with columns):** the solved model is not the intended structure —

- the columns carry **exactly zero force** (axial = shear = moment = 0) at
  every section/support combination probed;
- the beam's reactions do **not** equilibrate the applied load (e.g. sum of
  vertical "reactions" −32 kN vs a 60 kN UDL, and +256 kN vs a 60 kN UDL in
  another corner) and its end moments have no physical source (−94 kNm,
  −350 kNm, +610 kNm depending on the beam section);
- forces change wildly with the beam's *own* section size — behaviour a
  correctly-connected frame does not have.

**Root cause:** not yet probed inside Robot (needs a dedicated Robot-side
investigation). The finding is behavioural: `new_2d_frame()` +
`build_structure_from_spec` on this Robot build does not produce a coherent
multi-member frame from the X-Z spec.

**Workaround (validated):** model planar frames with `project: "3D"` (the
mode every `RobotBridge` template uses). The same portal modelled as 3D
reproduces exact portal-frame statics at both probe corners — column axial
= wL/2 each (= 30 kN for w=10 kN/m, L=6 m), beam end moment M_end and
midspan M_mid satisfying M_end + M_mid = wL²/8 (45 kNm), utilizations sane
(0.17–0.43 range for the tested corners).

**Affected / unaffected specs:**
- AFFECTED (live-solve `"2D"` specs with columns — historical PASS/FAIL
  results are suspect, not just untested):
  - `batch/test_runner.py` `SPEC` (portal: bars 1,3 columns + bar 2 beam).
    FIXED to `"3D"` in the same commit as this note.
- UNEFFECTED (`"2D"` single-beam / stability-only models — the P1 baseline
  6 m pinned beam still reproduces 45/90 kN·m midspan moments exactly):
  - `batch/test_headless_driver.py` `BASELINE_SPEC`
  - `batch/test_dialog_watcher.py` `BASE`
  - `batch/test_stability_timeout.py` `BASE`
- OFFLINE-ONLY (`"2D"` but never sent to Robot; no solve, results not
  affected, kept for spec-shape parity):
  - `batch/test_design_space.py` `_geometry()`
  - `batch/test_surrogate_search.py` `_geometry()`

**Regression guard:** `batch/test_portal_statics.py` (live test) asserts
column axial force ≈ wL/2 and the beam moment balance
M_end + M_mid = wL²/8 for a 3D portal — so a silent regression to the
incoherent pattern is caught the next time it is run.

## Thread safety notes (Windows COM)

`RobotBridge` wraps every public method in a `@com_thread_safe` decorator
that calls `pythoncom.CoInitialize()` / `CoUninitialize()` around COM access
on the calling thread, guarded by a re-entrant lock. This makes it safe to
call from Streamlit's per-session worker threads and from a tool-calling
executor thread without triggering `CoInitialize has not been called` or
cross-apartment marshalling errors.

## Rich results (WP6)

- `export_member_forces` now returns all six components
  (FX/FY/FZ/MX/MY/MZ) at `divisions` stations per bar.
- `export_node_displacements(case_id)` → UX/UY/UZ (m) and RX/RY/RZ (rad)
  per node, with coordinates. **Verified live:** Robot returns translation
  components scaled ×1e-3 vs metres, so they are multiplied by 1000;
  rotations are already in rad.
- `export_bar_stresses(case_id, divisions)` → MPa at each station: axial
  (FXSX), combined extreme (Smax/Smin), bending from MY and MZ, shear
  Y/Z, torsion. **Verified live:** raw values are in kPa (÷1000 = MPa).
- `export_results_to_excel(file_name, sheets=[...])` builds a workbook with
  exactly the requested sheets: `member_forces`, `reactions`,
  `displacements`, `stresses`, `boq`, `modal`. Missing data raises a clear
  error telling the agent to export it first.

## Shells / materials / volumes (WP4)

- `set_material` uses native material labels (type 8) — verified `'STEEL'`
  loads E = 210 GPa, NU = 0.3 from the database.
- **Panels are an honest approximation.** RobotOM v27 exposes no
  panel/plate object server, and `FiniteElems.Create` cannot marshal its
  node parameter (arrays/selections/strings all rejected — verified live),
  so `create_panel` builds an equivalent **bar grillage** (a dense grid of
  beams in the panel plane). `set_panel_thickness` re-sections the grillage
  to the nearest IPE depth; `apply_panel_pressure` distributes pressure as
  equivalent nodal loads (total force = p·A is conserved).
- **Solid volumes are native.** `create_solid` / `create_solid_box` use
  `Objects.CreateSolid` with a semicolon-separated face string (verified
  live, `IsVolume=True`). CAUTION: solids mesh with Robot's default fine
  mesh, so `solve` can take several minutes — use sparingly.
- `create_structure_from_spec` accepts `materials` and `panels` keys.

## Photo / PDF import + custom-script results fix

- **Attachments (sidebar "📎")**: upload sketches/photos (PNG/JPG) and PDFs
  before a message. Images are sent to the LLM as vision content when the
  selected model supports it (OpenAI-style `image_url` parts and Gemini
  `inline_data` are both wired; non-vision endpoints get an honest
  "describe the sketch" fallback instead of a crash). PDFs are parsed with
  `pypdf` and their text (≤6k chars) is injected into the user message;
  scanned/image-only PDFs are flagged. Attachments are one-shot per turn.
- **Custom-script results**: `run_custom_script` now wraps the live bridge
  so export methods return **lists of dicts (records)**, not DataFrames —
  `for r in rows: r['MY_kNm']` works directly. This eliminates the
  "persistent error pattern" caused by iterating a DataFrame's column
  names. `type`/`repr` were also added to the sandbox builtins.
  The stuck-pattern message now names the last failing tool(s).

## Code check & combinations (P4/P5) — verified live

**Utilization ratios (P4).** RobotOM v27 exposes **no code-check server at
all** (verified: nothing in the type library, and `dir()` sweeps of
`Project`/`Results`/`CalcEngine` show only the RC-only `DimServer`).
`get_utilization_ratios(case_id, bar_ids?)` therefore computes an
**analytical elastic check**: Robot's own solved bar stresses (WP6,
MPa-corrected) ÷ the material design strength `RE` (verified: catalog
`STEEL` carries RE = 235 MPa; the EURO section default resolves to
248.2 MPa = 36 ksi — the returned `fy_MPa` column is the source of truth).
Per bar it returns the governing `Utilization`, `Governing_Check`
(`combined_normal` = axial + biaxial bending at the extreme fiber,
`axial`, `shear_y/z`, `torsion`), and `Status` PASS/FAIL (>1.0). Custom
materials are checkable **only** when `set_material(..., fy_mpa=...)` was
given; otherwise the bar returns an explicit `NOT_CHECKABLE` row with the
reason — never a silent number. `store_result` snapshots now include the
utilization summary (`util 1.51 (combined_normal) [FAIL]`), so
`list_stored_results` comparisons mean "does it pass", not just "how much
does it weigh".

**Load combinations (P5).** `define_combination(name, case_factors,
combination_type)` creates real Robot combinations via
`Cases.CreateCombination(num, name, I_CBT_*, nature, I_CAT_COMB=0)`. Two
traps verified live: the **analize param must be 0** (`I_CAT_COMB` —
passing `STATIC_LINEAR(1)` silently creates a case that solves to zero),
and `CreateCombination` returns `None` (fetch via `Cases.Get` +
`CastTo('IRobotCaseCombination')`). Also, `CastTo` QI-succeeds even on
simple cases, so combination detection uses `IRobotCase.Type == 1`.
Factors go through `CaseFactors.New(case, factor)` and read back via
`Get(i).CaseNumber/.Factor` (1-based). **`solve()` evaluates combinations
automatically** — verified: 1.2D+1.6L returned exactly 1.2·M_dead +
1.6·M_live (−198.00000000000006), idempotently. `solve_combination` is a
documented convenience; `get_governing_combination(bar_id, 'MY')` ranks
all cases/combinations by max |component|.

**Parametric sections (probed for Phase 6).** `IRobotBarSectionData`:
`Type = I_BST_NS_RECT(5)` → `CreateNonstd(0.0)` → `SetValue(0=B, 1=H)` →
`CalcNonstdGeometry()` → `Store`. **Unit-agnostic**: feed metres →
A = 0.15 m², I = bh³/12 exact for a 300×500; feed millimetres →
mm-unit values back. Empirical `GetValue` indices: 0=A, 4=I_major,
5=I_minor, 8/9=extreme-fibre distance, 12=H, 13=B.

**Meshing (probed for Phase 9).** Exposed via COM:
`Project.Preferences.MeshParams` → `MeshType` / `SurfaceParams`
(`Coons`, `Delaunay`, `FiniteElems`, `Generation`, `Method`) /
`VolumeParams`, plus `MeshParamsFloors/Walls` and `MeshAutoAdjust`.

## Modal analysis (WP7) — verified limitation

- Modal cases (I_CAT_DYNAMIC_MODAL = 11), `ModesCount`, and the result
  servers (`Results.Advanced.Eigenvalues` / `Eigenvectors`) all work.
- **However, the modal solver cannot be driven programmatically in this
  Robot SA 2027 build:** `CalcEngine.Calculate()` never returns while a
  modal case exists (verified in multiple live probes — it hangs after the
  GUI shows the modes and the results DB stays empty), and it leaves the
  engine unusable for later static solves.
- `solve_modal` therefore never calls `Calculate()`: it sets up the modal
  case, checks for pre-existing results, removes the case again (so static
  analysis keeps working) and reports the limitation honestly, telling the
  user to run modal analysis in the Robot GUI. After that,
  `export_modal_frequencies` / `export_modal_mode_shapes` read the stored
  results, and `export_results_to_excel(sheets=['modal'])` includes them.


## Batch optimization engine (Phases 1–7)

A standalone, headless optimizer in `batch/` — no Streamlit, no LLM per
candidate. Reuses the verified RobotBridge primitives but runs on its own
Robot instance (`HeadlessSession`, always `new_instance=True`, never touches
the interactive app's Robot).

- **`batch/headless_driver.py`** — `HeadlessSession`: connect (`visible=False`
  verified), build_from_spec, `validate_stability()` (pre-solve mechanism
  detection), `solve_all`, DialogWatcher (auto-dismisses the benign
  "Calculation Messages" dialog; force-kills on unknown dialogs), solve
  timeout, deterministic `close()` (zero orphaned robot.exe verified),
  dead-session `reconnect()`. Reused-session timing ≈ 5–11 s/candidate
  (3× faster than relaunching).
- **`batch/storage.py`** — SQLite (sqlite3, no new dep): runs / candidates /
  results / checkpoints / run_cancellations. Checkpoint after every candidate
  (a crash loses at most one); `get_resume_point` makes runs resumable.
- **`batch/buckling_check.py`** — `check_euler_buckling()`: compression-only
  (negative axial verified), `r = sqrt(I_minor/A)` derived (no direct r in
  RobotOM), `Pcr = π²EI/(KL)²`, real-existence validation (T2), with the
  standing "minor-axis Euler screening only" caveat.
- **`batch/design_space.py`** — DesignSpace spec + full grid-search candidate
  generation (cap 50 000). Validates group names / bar-id overlaps / section
  presence / analysis types.
- **`batch/runner.py`** — `run_batch(design_space, run_id=None,
  max_consecutive_failures=5)`: one reused session; per-candidate
  build → validate_stability → solve → weight + utilization + buckling →
  record → checkpoint; failure isolation (one bad candidate never aborts the
  run); abort after N consecutive failures; **cooperative cancellation**
  (`storage.is_cancel_requested` checked between candidates — current
  candidate finishes + checkpoints first); resume from checkpoint.
- **`batch/pareto.py`** — `compute_pareto_frontier()` (hard constraint gate:
  `pass_fail == PASS` excluded candidates never enter the frontier, no matter
  how light; then standard Pareto dominance over weight + strength_margin)
  and `pareto_summary()` (markdown ranked by weight with the "elastic stress
  + basic Euler buckling, not full code compliance" caveat).
- **`batch/surrogate_search.py`** — [SURROGATE PHASE A] evaluation-efficient
  alternative to the full grid: a pure-numpy Gaussian process trained on
  EVERY compatible past run in `runs.db` (`Storage.get_all_results_all_runs`)
  proposes which candidate gets the next Robot call (EHVI over the live
  Pareto frontier, or UCB). Robot COM stays the only evaluator — every
  proposal is really built/solved/checked through `runner._evaluate_candidate`
  with identical checkpointing, failure isolation and cancellation. Stops on
  a Robot-call budget (default 300), N non-improving proposals (patience),
  space exhaustion or cancellation; auto-falls back to plain grid search
  (status `grid_fallback`, zero calls spent) when the grid is small enough
  that exhausting it is cheaper. Nothing is ever certified by the surrogate —
  `pareto.py`'s hard gate is unchanged. Offline tests:
  `batch/test_surrogate_search.py` (fake-session, brute-force validated).
- **`batch/export_candidate.py`** — materialize an optimized design as a
  real `.rtd` project so it can be opened in Robot: `export_candidate()`
  builds + solves one design_vars (exactly like `runner._evaluate_candidate`)
  in a `visible=True` session and saves via `RobotBridge.save_project()`;
  `export_best_from_run(run_id, path, frontier_index=0)` exports the
  lightest passing frontier candidate from a completed run. Warns (never
  silently saves) when a spec still uses `project="2D"` (README known
  issue). Offline tests: `batch/test_export_candidate.py`.

**LLM-facing tools (Phase 7, in `agent/tool_registry.py`):**
`start_optimization_run(spec)` (validate + estimate only — NEVER starts),
`confirm_and_start_optimization_run(run_config_id)` (background thread,
returns run_id immediately), `check_optimization_status(run_id)`,
`get_optimization_results(run_id)` (Pareto markdown once completed; refuses
partial results), `cancel_optimization_run(run_id)` (clean stop between
candidates). Same staged-confirm + background-thread shape for large design
spaces: `start_surrogate_search_run(spec, budget, patience, acquisition)`
(validate + estimate only; recommends the grid tool instead when the grid
is small enough that exhaustive search is cheaper) and
`confirm_and_start_surrogate_search_run(run_config_id)` — surrogate runs
poll through the same `check_optimization_status` / `get_optimization_results`
tools. `export_best_design(run_id, file_name)` materializes the lightest
passing candidate of a completed run as a `.rtd` project in `generated/`.
The batch tools import into `batch/` only — `batch/` never
imports `agent/tool_registry.py`, preserving the isolation from Phase 0.

**Build-and-optimize round-trip chat tools:** `export_structure_spec()` reads
the LIVE model back into the same `geometry` JSON shape
`create_structure_from_spec`/`start_optimization_run` accept (the reverse of
building from a spec — use it as `spec.geometry` when optimizing an
already-built model); `list_available_sections(family=None)` returns the
catalog section names (`tools/section_sizing.py`) with no Robot solve;
`apply_self_weight(case_id, density=7850)` applies every bar's self-weight
as one call (unit mass × length × g, lumped 50/50 to each bar's end nodes —
the classic truss lumping; **verified live: sum(FZ) reactions equals the
reported total exactly**, whereas the earlier per-bar uniform-load write
under-applied ~15.7% on a 3D 138-bar assembly, so nodal lumping is the only
self-weight source now); `apply_bar_load(bar_id, case_id, value_kn_m, direction)`
has the SAME protection: if the current model contains **coincident-but-distinct
nodes** (an arch springing node sharing a deck-end node's coordinate — what
`create_arch_truss`/a compose twin-arch produce), Robot's solver silently
under-transfers bar-uniform records to reactions (live-verified 6.9–20%
shortfall across single-plane arch / twin-arch / twin-arch deck UDL), so the
tool transparently substitutes the statically equivalent nodal loads (q·L/2
per end node) and reports `method='nodal_lumped'` + a `warning`; models
without coincident nodes keep the true uniform record (`method='bar_uniform_record'`,
verified exact: flat trusses/frames/multi-plane braced frames/elevated arches
all 0.00% error live). The trigger is **coincident-node geometry only** —
multi-plane connectivity/bracing/valence are exonerated. `force_record=True`
forces the raw uniform record on affected models with an explicit risk
warning (for true member-level UDL beam design, accepting the equilibrium
shortfall). `set_support` gained `"spring"`
(elastic-linear via `IRobotNodeSupportData.ElasticLinear` + K*/H* stiffness,
additive to the fixed/pinned/roller_* types); `preview_structure_geometry()`
renders a wireframe PNG of the in-memory geometry (matplotlib, no COM).
`check_model_stability()` runs the same mechanism rank check
`batch/runner.py` uses (now on `RobotBridge.validate_stability`, which
`HeadlessSession` delegates to — call it before `solve` on manually-built
models); `generate_code_combinations(combination_set="ULS_SLS_basic")`
builds the EN 1990 set (1.35G+1.5Q, 1.05 multi-variable, SLS 1.0) via the
existing `define_combination`; `compare_topologies(variants, load_spec)`
sizes truss/arch/braced-frame/grid variants under one load spec through the
existing optimizer and ranks them by lightest passing design
(`batch/topology_compare.py`, one run per variant).
Offline tests: `batch/test_chat_build_tools.py` + `batch/test_topology_compare.py`.

**Compose arbitrary shapes (compose_structure, in `agent/tool_registry.py`):**
`compose_structure` builds ANY shape (twin arches, twin trusses, cable-stayed
decks, double-deck frames…) from verified geometry primitives
(`tools/geometry_primitives.py`) instead of hand-written node/bar JSON. State
persists ACROSS tool calls in the session: call it once per step
(`action="step"`, `step={...}`), then `action="finish"` returns the assembled
geometry which you pass to `create_structure_from_spec`. Ops: `chord`
(straight|arc), `web` (pratt|warren between two chains), `bracing`
(cross|transverse BETWEEN two parallel planes — the twin-arch case), `copy`
(mirror a chain into a second plane via `y_shift`), `support` (pinned/fixed/
roller_x/roller_z/spring on a chain's ends or explicit nodes). **Reliability
rule:** for assemblies with more than ~5-6 steps call `action="step"` ONCE PER
STEP — do not pack a giant `steps` array into one call (hand-typed JSON has a
reliability ceiling). Every op validates IMMEDIATELY at the step boundary
(unknown chain names, mismatched panel counts, invalid `y_shift`, duplicate
names all raise actionable errors, never deferred to `finish`); `finish` then
runs the same `spec_integrity_issues` pre-flight as `build_structure_from_spec`
and refuses to return a broken spec. Auto-numbering means node/bar ids are
never computed by hand and copies can never collide with existing chains.
[2026-08-23 AUDIT] compose finish also MERGES coincident-but-distinct nodes
(identical coordinates) into one node up front - Robot's solver silently
merges them during Calculate() anyway, which was the root cause of the
bar-uniform load shortfall and of lossy export_structure_spec round-trips
after a solve. Compose-built models therefore never contain coincident
nodes, so bar_uniform loads are always exact on them and the round-trip is
lossless. apply_nodal_load now fails loudly if the target node does not
exist in the live model (a silent no-op otherwise).
The named templates (`truss_spec` / `arch_truss_spec` / `cylindrical_tank_spec`)
are themselves re-implemented as recipes over these primitives, guarded
byte-identical by `tools/test_geometry_primitives.py::test_legacy_byte_identity`.
Offline tests: same file — `test_compose_chord_generators`,
`test_compose_web_and_bracing_primitives`, `test_compose_copy_no_id_collision`,
`test_compose_bracing_lengths_sane`, `test_compose_per_op_validation`,
`test_compose_full_assembly_finish`.

**Session-safety & diagnostics (post-split-session hardening):** `tools/robot_seat.py`
is a cross-process Robot "seat" registry (`runtime/robot_seat.json`) that records
who owns the lone Robot seat — owner pid/kind, robot pid(s), connect path. Any
`RobotBridge.connect()` refuses to attach/spawn over a LIVE foreign owner (the
interactive app fails fast; a batch `new_instance=True` waits up to 60 s for a
just-finished chain stage to release, covering the stage-handoff race). `close()`
releases only if it owns the seat; stale seats (dead owner / dead robot) are
reclaimed automatically. The new `robot_session_status()` tool surfaces "Robot
session pid X, connected via Y, seat owner Z, live robot.exe list" in one call —
call it FIRST when anything smells like a split session (stale bar ids, RPC
drops, phantom dialogs). `build_structure_from_spec` now (a) rejects malformed
specs up front via `spec_integrity_issues()` (duplicate node/bar ids, dangling
bar→node refs) and (b) hard-errors if the live bar count does not exactly match
the requested spec, instead of failing later with `Bar N not found`; the
`'Calculation Messages'`/save-changes dialogs are dismissed deterministically by
both paths (headless `DEFAULT_DIALOG_PATTERNS` now includes the save prompt with
`No`); and section names are pre-validated (`L 120` missing its `x…x…` legs,
`IPE  chord` placeholder doubles, punctuation) before any catalog poke.


## Eurocode member checks (EN 1993) — v1 scope and explicit caveats

The repo adds Eurocode-graded member checks on top of the existing elastic
utilization screen (see `eurocode_scope.md` for the locked decisions D1–D8):

- **Bracing data model** (`tools/bracing_registry.py`): Robot has no bracing
  concept, so unbraced lengths are an engineer-input layer
  (`set_bracing` / `get_bracing`). **Default-and-warn**: any check running
  without an explicit `Lcr_*` uses the FULL bar length and tags the result
  `lcr_*_source: defaulted` with a warning — a default is a conservative
  assumption, NOT a verified bracing condition.
- **Classification** (`tools/section_classification.py`): EN 1993-1-1
  Table 5.2, Class 1–3 fully supported; **Class 4 -> NOT_CHECKABLE**
  (no EN 1993-1-5 effective width in v1). Dimensions are read LIVE from
  Robot (`GetValue` 12/13/14/15/16 = h/b/tw/tf/r — probe-verified).
- **LTB** (`tools/ltb_check.py`): **EN 1993-1-1 §6.3.2.2 (general method)
  only — §6.3.2.3 NOT implemented.** Doubly-symmetric rolled I-sections
  only (ShapeType-verified). It/Iw are not exposed by Robot so they are
  computed from the live geometry (It within ~10%, Iw <1% of published
  values). C1 comes from the exported moment shape (ENV 1993-1-1 Annex F —
  withdrawn-annex material used as standard practice, stated, never
  hidden); load assumed at the shear center. Beam-column interaction per
  §6.3.3 eqs. (6.61)/(6.62) with Annex B factors. Non-I / Class 4 /
  unavailable-dimension sections -> NOT_CHECKABLE (never a guessed value).
- **Connections** (`tools/connection_check.py`): **simple shear only**
  (fin plate / double angle / end plate), EN 1993-1-8 §3 bolts
  (Table 3.4), §3.10.2 block shear, §4.5.3 fillet welds. **No moment
  connections, no base plates in v1.** The governing failure mode is always
  named (bolt shear / bearing / block shear / weld). Block-shear geometry
  is a documented v1 single-line-end-bolt model, flagged for validation
  against the SCI "Green Book" numbers (D8).
- **Partial factors** are EN recommended values (γM0=γM1=1.0, γM2=1.25),
  configurable constants in `tools/eurocode_params.py` (National-Annex
  override point). Grades S235/S275/S355/S460 with EN 10025-2
  thickness-dependent fy/fu; Robot's material RE is the source of truth but
  is capped by the EN table at the actual flange thickness.
- **Integration** (`check_eurocode_members`): per-bar worst-governing
  across elastic / Euler buckling / LTB / connection with the governing
  check named. NOT_CHECKABLE means "not certified", never a silent pass.
- **Validation oracles (D8)**: the Designers' Guide to EN 1993-1-1 LTB
  worked example and the SCI "Green Book" simple-joint numbers are the
  agreed targets; the tests currently carry independent hand calcs (swap
  points clearly marked) until the published numbers are pasted in.

KNOWN BUILD DEFECT (separately tracked — see `eurocode_scope.md` §6):
a stale attached Robot session and PINNED supports both return zero solver
results on this build; live Eurocode tests therefore use a fresh instance
and fixed/roller supports (the simply-supported closed-form Mcr is
conservative for the stiffer fixed end).

## Extending

- Add new tool methods to the relevant `tools/*.py` bridge class.
- Register a JSON-schema entry in `agent/tool_registry.TOOL_SCHEMAS`.
- Add a matching `_tool_<name>` handler method to `ToolExecutor`.

No changes to `app.py` or `agent/llm_providers.py` are required to add new
tools — the agent loop is fully schema-driven.
