# Eurocode End-to-End Scope — EN 1993 Checks for the Structural Copilot

Status: **LOCKED** (do not drift from this document while implementing).

This document is the single source of truth for the Eurocode implementation
phases. Every module, tool, and test added for this work must be traceable to
a numbered decision below. Deviations require updating this document first.

---

## 1. Scope (what "end-to-end" means here)

Four governing checks per bar, composable with the existing pipeline:

| Check | Standard | Module | Status convention |
|---|---|---|---|
| Elastic stress (existing) | first-yield / fy | `get_utilization_ratios` (robot_tool) | PASS / FAIL / NOT_CHECKABLE |
| Member buckling (existing) | minor-axis Euler screening | `batch/buckling_check.py` | PASS / FAIL / N/A (tension) |
| Cross-section classification | EN 1993-1-1 §5.5 / §5.6 | `tools/section_classification.py` | Class 1–4, Class 4 → NOT_CHECKABLE |
| Lateral-torsional buckling | EN 1993-1-1 §6.3.2.2 (+ §6.3.3 interaction) | `tools/ltb_check.py` | PASS / FAIL / NOT_CHECKABLE |
| Simple shear connections | EN 1993-1-8 §3 / §4 | `tools/connection_check.py` | PASS / FAIL / NOT_CHECKABLE |

The overall per-bar verdict (Phase E) is the WORST of the applicable checks,
with the governing check named — same enumeration style as
`Governing_Check` today.

## 2. Locked decisions (Phase 0)

### D1 — Partial safety factors (configurable constants, NOT literals)
- γM0 = **1.0** (cross-section resistance)
- γM1 = **1.0** (member buckling resistance)
- γM2 = **1.25** (connections: bolts / welds / bearing)
These are the **EN recommended values**. They live in
`tools/eurocode_params.py` as module constants with a documented
National-Annex override point. The formulas never inline a γ as a magic
number.

### D2 — Steel grades (EN 10025-2)
Supported grades: **S235, S275, S355, S460**. `fy`/`fu` are
**thickness-dependent** (nominal values per EN 10025-2 Table 7 / Table 3.1):
t ≤ 16 / 16–40 / 40–63 / 63–80 / 80–100 / 100–150 / 150–200 / 200–250.
Built as a data table in `tools/eurocode_params.py` (`GRADE_TABLE`),
keyed by grade with a per-thickness band list. Source-of-truth rule:
use Robot's material `RE` where it is authoritative (this repo verified
catalog `STEEL` → 235 MPa), but **cap it by the EN 10025-2 nominal value
for the grade at the section's actual flange thickness** — a material
declared S355 with RE=355 must not be credited 355 MPa on a flange thicker
than 16 mm.

### D3 — Buckling curves (EN 1993-1-1 Table 6.1/6.2/6.3/6.4)
- Imperfection factors α per Table 6.1: a0=0.13, a=0.21, b=0.34, c=0.49,
  d=0.76; αLT per Tables 6.3/6.4 + 6.5 (rolled I: h/b ≤ 2 → curve a,
  αLT=0.21; h/b > 2 → curve b, αLT=0.34) — **data, not derived**.
  Table 6.2 (rolled I, S235–S460, buckling about y–y / z–z) is also data:
  `h/b ≤ 1.2, tf ≤ 100` → a / b; `1.2 < h/b ≤ 3.0, tf ≤ 100` → b / c;
  `h/b > 3.0, tf ≤ 100` → c / d; `tf > 100` → d / d. There is no
  geometric formula that reproduces these tables — they are lookup data.

### D4 — LTB method
Implement **§6.3.2.2 (general case)** only. §6.3.2.3 (rolled I-sections,
less conservative) is explicitly out of scope for v1. `Mcr` uses the
closed-form for **doubly-symmetric I-sections** with `C1` from the
**actual exported moment diagram shape**, per the ENV 1993-1-1 Annex F
tables — this is withdrawn-annex material used as standard practice; the
fact is stated in `ltb_check.py` and in the tool description, never
hidden. Interaction for beam-columns per **§6.3.3 eqs. (6.61)/(6.62)
with Annex B interaction factors kyy/kzy** (simpler, common practice).

### D5 — Connection tier
**Simple shear connections only**: fin plate / double-angle / end plate in
shear, EN 1993-1-8 §3 (bolts) and §4 (welds). **No moment connections,
no base plates** in v1. Governing failure mode is always named (bolt
shear / bearing / block shear / weld).

### D6 — Bracing data model
Robot has no bracing concept. Unbraced lengths are an **engineer-specified
input layer** (`tools/bracing_registry.py`, session-scoped on the bridge):
`Lcr_y`, `Lcr_z`, `Lcr_LT`, plus optional `brace_points` (fractions 0–1)
that shorten `Lcr_LT` only. **Default-and-warn**: any check running
without an explicit value uses the full bar length and MUST tag the result
`lcr_*_source: defaulted` with a warning. A default is a conservative
assumption, NOT a verified bracing condition. Validation: `Lcr < 0`
rejected; `Lcr > 2.5 × bar length` flagged as a suspicious K-factor
(warning surfaced in results, not silently accepted).

### D7 — NOT_CHECKABLE discipline (unchanged from the repo standard)
Any section/check outside v1 scope returns `Status=NOT_CHECKABLE` with a
stated reason — Class 4 sections, parametric/custom sections without
published section data, non-doubly-symmetric sections for LTB, moment
connections. **Never** silently apply an in-scope formula to an
out-of-scope section (that overstates capacity).

### D8 — Validation oracles (LOCKED by the user)
- **LTB (Phase C):** the **Designers' Guide to EN 1993-1-1** LTB worked
  example — simply-supported rolled I-beam under UDL. The published
  `Mcr`, `χLT`, `Mb,Rd` numbers are hardcoded into `test_ltb_check.py`
  as known-answer assertions.
- **Connections (Phase D):** the **SCI "Green Book"** simple-joint worked
  example (fin plate / end plate in shear). Published bolt/bearing/block
  shear numbers hardcoded into `test_connection_check.py`.
Exact citations + numbers get recorded in each test's docstring when the
tests are written.

## 3. Data model (Phase A)

`tools/bracing_registry.py` — pure Python, no COM:

```
BracingRegistry
  set_bracing(bar_id, lcr_y, lcr_z, lcr_lt, brace_points, bar_length=None)
  get(bar_id) -> entry dict
  lcr_lt_for(bar_id, length_m) -> (value, source, warnings)
  lcr_y_for / lcr_z_for (same contract)
  resolve(bar_id, length_m) -> full summary (values + sources + warnings)
  all_bars() / remove(bar_id) / clear() / __len__
```

- `brace_points` are fractions in [0, 1]; `Lcr_LT` = longest sub-span
  between braces (end points 0.0 / 1.0 implied) × bar length.
- Sources: `"explicit"`, `"brace_points"` (derived from explicit bracing),
  `"defaulted"` (full length, conservative assumption — warning emitted).
- Side-table lives on `RobotBridge.bracing` (session-scoped; the batch
  runner reaches it via `session.bridge.bracing`).

## 4. Classification scope (Phase B)

- `classify_section(name, h, b, tf, tw, fy, stress_state)` per Table 5.2
  parts 1 & 2 → Class 1–4 (data-driven element checks).
- Section detail data is **read LIVE from Robot** — the probe (§6) proved
  `label.Data.GetValue(12)=h, (13)=b, (14)=tw, (15)=tf, (16)=r` are exposed
  for catalog sections. Flange outstand `c = (b − tw)/2 − r` and web height
  `c = h − 2·tf − 2·r` (rolled, root radius included) are computed from
  those — no fallback table needed. Parametric/custom sections whose
  GetValue dimensions are unavailable → NOT_CHECKABLE.
- **Class 4 → NOT_CHECKABLE** with the reason stated (EN 1993-1-5
  effective width is out of v1 scope). Class 1–3 fully implemented.
- Validated: IPE 300 in pure bending = Class 1; a very slender built-up
  plate = Class 4 (both hand-checked in tests).

## 5. LTB scope (Phase C)

`check_lateral_torsional_buckling(case_id, bar_ids=None)`:
- **Doubly-symmetric rolled I-sections only** (detected via the verified
  `ShapeType` map: IPE=20, IPN=25, HEA=10, HEB=12, HEM=14) for v1.
- Properties from LIVE geometry: `Iz = GetValue(5)`, `h = (12)`,
  `b = (13)`, `tf = (15)`, `tw = (14)`, `r = (16)`, `Iy = (4)`,
  `Wy = 2·Iy/h`. `It` and `Iw` are NOT exposed by Robot (probed 0–150 +
  ElasticParams — nothing), so they are COMPUTED from the live geometry
  via standard closed forms:
    - `Iw = Iz·(h − tf)² / 4`   (doubly-symmetric I, flange-centroid model)
    - `It = (2·b·tf³ + (h − 2·tf)·tw³)/3 + 2·0.105·(r + tw/2)⁴`
      (thin-wall + fillet-corner correction; underestimates the published
      value by ~10% for rolled sections — conservative for Mcr, and
      validated against published It/Iw within tolerance in the tests).
  Non-I shapes, or sections whose live dimensions are unavailable →
  NOT_CHECKABLE (no guessed values, ever).
- `Mcr` closed form with `C1` from exported moment shape (ENV Annex F);
  λ̄LT = √(Wy·fy/Mcr); χLT from the LTB curve (§6.3.2.2 general method,
  λLT,0 = 0.4, β = 0.75); `Mb,Rd = χLT·Wy·fy/γM1`; §6.3.3 Annex B
  interaction for beam-columns.
- Forces consumed exactly like `batch/buckling_check.py` consumes
  `export_all_member_forces` (verified DataFrame, columns
  Bar_ID, Position_m, FX…MZ).

## 6. Live-probe results (probed on this build — the source of truth)

`tools/probe_section_data.py` was run against live Robot (2027). Findings:

1. **Section GetValue map (catalog sections, units m / m² / m³ / m⁴):**
   `[0]=A, [4]=Iy (major), [5]=Iz (minor), [6]/[7]=b/2, [8]/[9]=h/2,
   [12]=h, [13]=b, [14]=tw, [15]=tf, [16]=r (root radius), [19]=Wpl,y,
   [20]=Wpl,z, [36]=nominal depth (mm)`. Verified against published
   values: IPE 200 (A=0.002848, tw=0.0056, tf=0.0085, r=0.012),
   IPE 300 (A=0.005381, tw=0.0071, tf=0.0107, r=0.015),
   HEA 200 (A=0.005383, tw=0.0065, tf=0.010, r=0.018),
   HEB 300 (A=0.014908, tw=0.011, tf=0.019, r=0.027).
2. **It / Iw are NOT exposed anywhere** (GetValue 0–150 scanned,
   `ElasticParams` = {L1, L2, N1, N2, MaterialModel} — unrelated).
   → computed from live dimensions per §5.
3. **ShapeType codes** (shape detection is data, not name-parsing):
   IPE=20, IPN=25, HEA=10, HEB=12, HEM=14 (doubly-symmetric I);
   UPE=37, UPN=38 (channels); L=1 (angle).
4. **Materials:** `STEEL` Data exposes `.RE` (248.2 MPa = 36 ksi in this
   build), `.E`, `.NU`, `.RO`. S-grade names (S235/S275/S355/S460) are NOT
   pre-existing labels — they must be created via set_material or declared
   by the engineer. EN 10025-2 `GRADE_TABLE` governs declared grades; RE
   caps the result.
5. **Member forces:** `export_all_member_forces(case_id, divisions)`
   returns a **pandas DataFrame** (columns Bar_ID, Position_m, FX_kN,
   FY_kN, FZ_kN, MX_kNm, MY_kNm, MZ_kNm) — consumed exactly like
   `batch/buckling_check.py` does.

6. **KNOWN BUILD DEFECTS (live-verified during Phase C; separately tracked
   zero-results bug — do NOT fix here):**
   a. A STALE attached Robot session returns zero results; a FRESH
      instance (`connect(new_instance=True)`) solves correctly (verified:
      cantilever FZ=+50 / MY=-250).
   b. PINNED supports return zero results even on a fresh instance
      (pinned-pinned and pinned+roller beams -> all-zero reactions and
      forces), while fixed-fixed and fixed+roller_z solve correctly.
      Strong candidate for the root cause of the tracked bug for
      pinned-base models. Live tests requiring results MUST use fixed /
      roller supports until this is resolved.
   c. Bar-uniform loads (`apply_bar_load`) return zero results even on a
      fresh instance (the `I_LRT_BAR_UNIFORM` enum is commented as both 4
      and 5 in robot_tool.py — a concrete lead). Nodal loads work.
   The Phase C live validation therefore uses a fixed+roller_z
   propped-cantilever driven by nodal loads; the simply-supported
   closed-form Mcr is conservative for the stiffer fixed end.

The "assumed interface ≠ actual" trap is closed: classification and LTB
are built on these verified facts, not on assumed section data.

## 7. Build order (do not parallelize)

A (bracing) → B (classification) → C (LTB) → **validate C against D8**
→ D (connections) → **validate D against D8** → E (integration:
utilization wrapper, runner constraint grammar, result_store/pareto
snapshots) → F (tests + README + plan doc caveats).

## 8. Explicit caveats (Phase F doc text — same discipline as existing)
- EN 1993-1-1 §6.3.2.2 (general method) implemented; §6.3.2.3 not.
- Class 1–3 sections supported; Class 4 → NOT_CHECKABLE (no EN 1993-1-5
  effective width in v1).
- LTB limited to doubly-symmetric rolled I-sections with section data from
  the local table + probe cross-check; parametric/custom NOT_CHECKABLE.
- Simple shear connections only (EN 1993-1-8 §3/§4); no moment connections
  or base plates.
- Bracing lengths are engineer-specified; defaulting to full bar length is
  conservative and explicitly warned.
- Partial factors are EN recommended values (γM0=γM1=1.0, γM2=1.25),
  configurable per National Annex.

