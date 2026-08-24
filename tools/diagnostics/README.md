# tools/diagnostics — OpenSeesPy cross-check (Phase A, developer-facing)

## What this is

An **independent-solver diff tool** for discrepancy investigations (e.g. the
bar_uniform / coincident-node under-transfer class of bug). It takes the SAME
geometry spec JSON that `create_structure_from_spec` /
`build_structure_from_spec` accept (`project`, `nodes`, `bars`, `supports`,
`cases`, `loads`), builds an equivalent **OpenSeesPy** elastic-frame model,
solves it, and prints reactions (global axes) + member forces (local bar
actions) so they can be compared against Robot or closed form.

## Scope / integration

- **NOT wired into the chat tool registry, app.py, or the batch optimizer.**
  `openseespy` is a dev-only dependency (`requirements-dev.txt`), never a
  runtime or Docker dependency.
- It is a diagnostic for the engineer/developer, not an agent tool.

## Usage

    pip install -r requirements-dev.txt          # includes openseespy

    # 1) Prove the solver against closed-form statics (the trust gate):
    python tools/diagnostics/opensees_crosscheck.py --self-test

    # 2) Cross-check a geometry spec (offline, no Robot needed):
    python tools/diagnostics/opensees_crosscheck.py --spec my_spec.json --divisions 4

## Trust gate (run before trusting any output)

`--self-test` verifies OpenSeesPy against three closed-form cases at 0.1%
(or better):

1. Pinned-base portal, beam UDL: column axial = wL/2 each, M_end + M_mid =
   wL²/8.
2. Simply-supported CHS beam, UDL: sum reactions = wL, midspan MY = wL²/8.
3. Cantilever, tip load: base shear = P, base moment = P·L.

All pass at 0.000% on the current solver.

## Modeling notes / conventions

- 3D elastic frame (`-ndm 3 -ndf 6`), `elasticBeamColumn`, `Linear`
  geomTransf. E = 210 GPa, G = E/2(1+ν).
- **Section properties are resolved OFFLINE from nominal dims** (idealized
  rolled-I from the `I_DIMS` table, exact CHS closed form, approximate L /
  RHS / SHS / fallback I). Sections flagged `approx` may be a few % off the
  published values — equilibrium / reactions are insensitive to this, member
  force *distribution* is not. Use live Robot props (future `--robot` mode)
  when absolute member-force values matter.
- **Reactions are in GLOBAL axes** (unambiguous). **Member forces are LOCAL
  bar-end actions** (N, Vy, Vz, Mx, My, Mz); Robot exports its own local
  convention, so compare component-wise and note possible sign flips between
  the two solvers.
- A **torsional anchor** (MX fixed) is applied at the first pinned/roller
  support: a collinear 3D beam chain with translation-only supports is free
  to twist about its own axis (a zero-energy mode). The anchor kills that
  mode only — in-plane statics are unaffected.
- `bar_uniform` loads are applied per sub-element via `eleLoad -beamUniform`;
  each spec bar is subdivided into `--divisions` sub-elements so member-force
  stations align with Robot`s `export_all_member_forces(divisions)`.
- One load case is solved per model (separate `ops.model` per case).

## Section catalog

Add sections to `I_DIMS` (rolled I) in `opensees_crosscheck.py`, or use the
`CHS DxT` / `RHS bXhXt` / `SHS bXbXt` / `L aXaXt` forms.