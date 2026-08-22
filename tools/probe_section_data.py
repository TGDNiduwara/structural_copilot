"""
tools/probe_section_data.py
===========================
[EUROCODE Phase B/C gate — eurocode_scope.md §6]
Live-Robot probe: what cross-section data does RobotOM v27 ACTUALLY expose?

The Eurocode checks need, per section:
  * classification  (EN 1993-1-1 Table 5.2): flange width b, flange
    thickness tf, web height c, web thickness tw  ->  b/tf and c/tw
  * LTB            (§6.3.2.2 closed form): Iz, It (torsion), Iw (warping),
    Wy, h, b
This repo already verified (empirical GetValue map) that label.Data exposes
0=A, 4/5=I, 8/9=extreme-fibre distance, 12=H, 13=B — but NOT whether
flange/web thicknesses or It/Iw appear anywhere. This probe dumps the full
GetValue index range plus the Data object's exposed members for a spread of
catalog sections, so the fallback-table decision in eurocode_scope.md §4
is made on evidence, not assumption.

Also probes material label Data (RE / fy source) and confirms the
member-forces return type actually observed in this build.

Run (needs a live, licensed Robot — attaches to the running instance and
opens a FRESH 3D project, exactly like the repo's own smoke tests):
    python tools/probe_section_data.py
"""

from __future__ import annotations

import sys

sys.path.insert(0, r"c:/Users/dinat/Downloads/structural_multi_app_agent/structural_copilot")

from tools.robot_tool import RobotBridge, RobotEnum

SECTIONS = [
    "IPE 200", "IPE 300", "HEA 200", "HEB 300",
    "L 60x60x6", "UPN 100", "UPE 200",
]
MATERIALS = ["STEEL", "S235", "S275", "S355", "S460"]


def _dump_section(bridge, name: str) -> None:
    print(f"\n--- section '{name}' ---")
    try:
        data = bridge.structure.Labels.Get(
            RobotEnum.I_LT_BAR_SECTION, str(name)).Data
    except Exception as exc:
        print(f"  <could not load section data: {exc}>")
        return
    members = sorted(
        m for m in dir(data)
        if not m.startswith("_") and m not in ("CLSID", "QueryInterface")
        and "sink" not in m.lower())
    print(f"  Data members ({len(members)}): {members}")
    got = []
    for i in range(0, 60):
        try:
            v = data.GetValue(i)
            if v is None:
                continue
            try:
                v = float(v)
                if v == 0.0:
                    continue
                v = round(v, 6)
            except (TypeError, ValueError):
                pass
            got.append((i, v))
        except Exception:
            continue
    print(f"  GetValue indices with non-zero values ({len(got)}):")
    for i, v in got:
        print(f"    [{i:>2}] = {v}")


def _dump_material(bridge, name: str) -> None:
    print(f"\n--- material '{name}' ---")
    try:
        data = bridge.structure.Labels.Get(
            RobotEnum.I_LT_MATERIAL, str(name)).Data
    except Exception as exc:
        print(f"  <material not available: {exc}>")
        return
    members = sorted(
        m for m in dir(data)
        if not m.startswith("_") and m not in ("CLSID", "QueryInterface"))
    print(f"  Data members ({len(members)}): {members}")
    for attr in ("Name", "RE", "RM", "E", "NU", "Type", "IsNonlinear"):
        try:
            print(f"    .{attr} = {getattr(data, attr)}")
        except Exception:
            pass


def _probe_force_return_type(bridge) -> None:
    print("\n--- export_all_member_forces return type (verified) ---")
    try:
        df = bridge.export_all_member_forces(case_id=1, divisions=2)
        print(f"  type: {type(df).__name__}")
        if hasattr(df, "columns"):
            print(f"  columns: {list(df.columns)[:14]}")
        else:
            print(f"  first row: {df[0] if df else '(empty)'}")
    except Exception as exc:
        print(f"  <no solved results to export: {exc}>")


def main() -> int:
    print("=" * 72)
    print("EUROCODE section-data probe (live Robot)")
    print("=" * 72)
    bridge = RobotBridge()
    print("  connecting to Robot (may launch robot.exe, up to 60 s)...")
    bridge.connect(visible=True)
    print("  connected; opening a FRESH 3D project...")
    bridge.new_3d_frame()

    # Ensure the section labels exist in the label database so Data reads
    # are representative of what create_bar actually assigns.
    for name in SECTIONS:
        try:
            bridge._get_or_create_section_label(name)
        except Exception as exc:
            print(f"  <could not load catalog section '{name}': {exc}>")

    for name in SECTIONS:
        _dump_section(bridge, name)
    for name in MATERIALS:
        _dump_material(bridge, name)

    print("\n--- minimal solved model: member-forces return type ---")
    try:
        bridge.clear_structure("3D")
        bridge.create_node(1, 0.0, 0.0, 0.0)
        bridge.create_node(2, 6.0, 0.0, 0.0)
        bridge.create_bar(1, 1, 2, "IPE 300")
        bridge.set_support(1, "pinned")
        bridge.set_support(2, "pinned")
        bridge.create_load_case(1, "DL")
        bridge.apply_bar_load(1, 1, -10.0, "Z")
        bridge.solve(timeout_s=180)
        _probe_force_return_type(bridge)
    except Exception as exc:
        print(f"  <minimal model step failed: {exc}>")

    print("\nPROBE COMPLETE — record these numbers in eurocode_scope.md §6.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
