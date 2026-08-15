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


## Extending

- Add new tool methods to the relevant `tools/*.py` bridge class.
- Register a JSON-schema entry in `agent/tool_registry.TOOL_SCHEMAS`.
- Add a matching `_tool_<name>` handler method to `ToolExecutor`.

No changes to `app.py` or `agent/llm_providers.py` are required to add new
tools — the agent loop is fully schema-driven.
