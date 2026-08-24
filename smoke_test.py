"""
smoke_test.py
=============
No-LLM end-to-end verification for the Structural Multi-App Agent.

Modes
-----
python smoke_test.py --offline
    Generates Excel / Word / PowerPoint / diagram artifacts from synthetic
    DataFrames. No Robot, no LLM, no network needed.

python smoke_test.py
    Full round trip through Autodesk Robot Structural Analysis via COM:
    connect -> new_2d_frame -> 6 m simply-supported beam (IPE300) ->
    supports -> 10 kN/m dead load -> solve -> export forces/reactions/BOQ ->
    office artifacts. Directly exercises the fixed COM liveness probe
    (IRobotApplication has no `.Name`; `.Project` is used instead).
"""

from __future__ import annotations

import os
import sys

import pandas as pd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from tools.diagram_tool import DiagramGenerator
from tools.excel_tool import ExcelReporter
from tools.pptx_tool import PowerPointReporter
from tools.word_tool import WordReporter

GENERATED = os.path.join(BASE_DIR, "generated")


# ---------------------------------------------------------------------- #
# Synthetic data (closed-form 6 m simply-supported beam, w = 10 kN/m)
# ---------------------------------------------------------------------- #


def _synthetic_member_forces() -> pd.DataFrame:
    rows = []
    for i in range(9):
        x = i * 6.0 / 8.0
        v = 10.0 * (3.0 - x)  # V(x) = w(L/2 - x)
        m = 5.0 * x * (6.0 - x)  # M(x) = w*x*(L-x)/2
        rows.append(
            {
                "Bar_ID": 1,
                "Position_m": round(x, 3),
                "FX_kN": 0.0,
                "FZ_kN": round(v, 3),
                "MY_kNm": round(m, 3),
            }
        )
    return pd.DataFrame(rows)


def _synthetic_reactions() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"Node_ID": 1, "Support_Type": "Pinned", "FX_kN": 0.0, "FZ_kN": 30.0, "MY_kNm": 0.0},
            {"Node_ID": 2, "Support_Type": "Pinned", "FX_kN": 0.0, "FZ_kN": 30.0, "MY_kNm": 0.0},
        ]
    )


def _synthetic_boq() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Section": "IPE300",
                "Count": 1,
                "Total_Length_m": 6.0,
                "Unit_Mass_kg_m": 42.2,
                "Total_Weight_kg": 253.2,
            },
        ]
    )


# ---------------------------------------------------------------------- #
# Office artifact generation (no Robot / no LLM required)
# ---------------------------------------------------------------------- #


def run_office_artifacts(member_df, reactions_df, boq_df, tag: str) -> None:
    os.makedirs(GENERATED, exist_ok=True)

    diagrams = DiagramGenerator()
    sfd = os.path.join(GENERATED, f"{tag}_SFD.png")
    bmd = os.path.join(GENERATED, f"{tag}_BMD.png")
    diagrams.plot_sfd(member_df, sfd)
    diagrams.plot_bmd(member_df, bmd)

    xlsx = os.path.join(GENERATED, f"{tag}_Results.xlsx")
    ExcelReporter().create_structural_workbook(
        file_path=xlsx,
        project_name="Smoke Test Project",
        member_forces_df=member_df,
        reactions_df=reactions_df,
        boq_df=boq_df,
    )

    docx = os.path.join(GENERATED, f"{tag}_Report.docx")
    WordReporter().generate_calculation_report(
        file_path=docx,
        project_title="Smoke Test Project",
        engineer_name="smoke_test.py",
        summary_text="Verification run: 6 m simply-supported beam, 10 kN/m UDL.",
        member_df=member_df,
        reactions_df=reactions_df,
        diagram_paths=[sfd, bmd],
    )

    pptx = os.path.join(GENERATED, f"{tag}_Presentation.pptx")
    PowerPointReporter().generate_presentation(
        file_path=pptx,
        project_title="Smoke Test Project",
        engineer_name="smoke_test.py",
        summary_text="Verification run: 6 m simply-supported beam, 10 kN/m UDL.",
        member_df=member_df,
        reactions_df=reactions_df,
        diagram_paths=[sfd, bmd],
    )

    for p in (sfd, bmd, xlsx, docx, pptx):
        size = os.path.getsize(p)
        assert size > 0, f"{p} is empty"
        print(f"  OK  {os.path.basename(p)}  ({size:,} bytes)")


# ---------------------------------------------------------------------- #
# Full Robot COM round trip
# ---------------------------------------------------------------------- #


def run_robot_roundtrip():
    from tools.robot_tool import RobotBridge

    robot = RobotBridge()
    print("  connecting to Robot (may launch robot.exe, up to 60 s)...")
    robot.connect(visible=True)

    robot.new_2d_frame()
    print("  [OK] new_2d_frame  (liveness probe passed — this is the fixed path)")

    robot.create_node(1, 0.0, 0.0, 0.0)
    robot.create_node(2, 6.0, 0.0, 0.0)
    print("  [OK] nodes 1-2 created")

    robot.create_bar(1, 1, 2, "IPE300")
    print("  [OK] bar 1 created (IPE300)")

    robot.set_support(1, "pinned")
    robot.set_support(2, "pinned")
    print("  [OK] pinned supports applied")

    robot.create_load_case(1, "Dead Load")
    robot.apply_bar_load(1, 1, -10.0, "Z")
    print("  [OK] load case + 10 kN/m UDL applied")

    robot.solve(timeout_s=180)
    print("  [OK] solve completed")

    member_df = robot.export_all_member_forces(case_id=1, divisions=8)
    reactions_df = robot.export_reactions(case_id=1)
    boq_df = robot.export_bill_of_materials()

    print(
        f"  [OK] exports: {len(member_df)} force rows, "
        f"{len(reactions_df)} reaction rows, {len(boq_df)} BOQ rows"
    )
    if not member_df.empty:
        print(member_df.to_string(index=False))
    if not reactions_df.empty:
        print(reactions_df.to_string(index=False))

    # Deliberately leave Robot open so the model can be inspected manually.
    return member_df, reactions_df, boq_df


def test_milestone_a() -> None:
    """Milestone A: templates, model-spec builder, concentrated loads."""
    from tools.robot_tool import RobotBridge

    robot = RobotBridge()
    robot.connect(visible=True)

    s = robot.create_truss(span=12.0, height=2.0, panels=6)
    assert s["status"] == "ok" and s["nodes"] == 14 and s["bars"] == 31, s
    print("  [OK] truss template -> %d nodes, %d bars" % (s["nodes"], s["bars"]))

    robot.create_load_case(1, "DL", nature=1)
    robot.create_load_case(2, "LL", nature=1)
    robot.apply_bar_load(1, 1, -5.0, "Z")
    robot.apply_bar_concentrated(2, 2, fz_kn=-10.0, ratio=0.5)
    robot.solve(timeout_s=180)
    mf = robot.export_all_member_forces(case_id=1, divisions=4)
    re_ = robot.export_reactions(case_id=1)
    print("  [OK] truss solved -> forces rows=%d, reactions=%d" % (len(mf), len(re_)))

    g = robot.create_rectangular_grid_frame(levels=2, bays_x=2, bays_y=2)
    assert g["status"] == "ok" and g["nodes"] == 27 and g["bars"] == 42, g
    print("  [OK] 2x2x2 grid frame -> %d nodes, %d bars" % (g["nodes"], g["bars"]))

    robot.clear_structure("3D")
    spec = {
        "project": "3D",
        "nodes": [
            {"id": 1, "x": 0, "y": 0, "z": 0},
            {"id": 2, "x": 6, "y": 0, "z": 0},
            {"id": 3, "x": 0, "y": 0, "z": 6},
            {"id": 4, "x": 6, "y": 0, "z": 6},
        ],
        "bars": [
            {"id": 1, "n1": 1, "n2": 3, "section": "HEB 200"},
            {"id": 2, "n1": 2, "n2": 4, "section": "HEB 200"},
            {"id": 3, "n1": 3, "n2": 4, "section": "IPE 360"},
            {"id": 4, "n1": 1, "n2": 4, "section": "IPE 200"},
        ],
        "supports": [{"node": 1, "type": "pinned"}, {"node": 2, "type": "pinned"}],
        "cases": [{"id": 1, "name": "DL", "nature": "permanent"}],
        "loads": [{"kind": "bar_uniform", "bar": 3, "case": 1, "direction": "Z", "value": -12.0}],
    }
    su = robot.build_structure_from_spec(spec)
    assert su["status"] == "ok" and su["nodes"] == 4 and su["bars"] == 4, su
    print(
        "  [OK] spec builder -> nodes=%d bars=%d cases=%d" % (su["nodes"], su["bars"], su["cases"])
    )
    print("  [OK] Milestone A")


def main() -> int:
    offline = "--offline" in sys.argv
    try:
        if offline:
            print("[offline] generating office artifacts from synthetic data...")
            run_office_artifacts(
                _synthetic_member_forces(),
                _synthetic_reactions(),
                _synthetic_boq(),
                "smoke_offline",
            )
            print("[offline] ALL CHECKS PASSED")
        else:
            print("[robot] full COM round trip + office artifacts...")
            member_df, reactions_df, boq_df = run_robot_roundtrip()
            test_milestone_a()
            run_office_artifacts(member_df, reactions_df, boq_df, "smoke_robot")
            print("ALL CHECKS PASSED (Robot left open for inspection)")
        return 0
    except Exception as exc:
        print(f"FAILED: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
