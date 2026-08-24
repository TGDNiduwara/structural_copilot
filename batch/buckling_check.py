"""
batch/buckling_check.py
=======================
[PHASE 3] Basic Euler buckling screening for compression members.

This is a companion to get_utilization_ratios (elastic stress check): an
elastically-fine member can still fail buckling, and the elastic check alone
would miss it. This module provides check_euler_buckling() which computes
Pcr = pi^2 * E * I_minor / (K*L)^2 and compares it to the member's solved
axial force.

VERIFIED FACTS (live probes, this Robot build):
  * Compression is NEGATIVE axial force (Probe A) - applies only when
    axial < 0; axial >= 0 returns "member in tension".
  * Radius of gyration r is NOT exposed directly by either the catalog or
    parametric section read-back (Probe B) - only A and I are returned, so
    r = sqrt(I_minor / A) is derived here.

SIMPLIFICATION (same "not full code compliance" discipline as the elastic
utilization tool): this checks MINOR-AXIS (weak-axis) Euler buckling only,
which is conservative when the unbraced length is equal in both directions,
but may be overly conservative for members braced differently about each
axis. It is a basic screening check, NOT a full code-based buckling /
interaction check.

IMPORTANT (T2 finding): bare .Get(n) on Robot collections SILENTLY
auto-creates proxies for nonexistent IDs on this build, so bar_id and
case/combination ids are validated against real enumerations here, never
via a bare .Get().
"""

from __future__ import annotations

import math
from typing import Any

from tools.robot_tool import RobotEnum

#: Constant note attached to every result so the "basic screening" nature is
#: impossible to miss (same discipline as the elastic utilization tool).
EULER_NOTE = (
    "Minor-axis Euler check only, basic screening - not full code-based buckling/interaction check."
)


def _real_bar_ids(bridge) -> list:
    """Real, existing bar numbers (T2: never trust a bare .Get())."""
    bars = bridge.structure.Bars
    coll = bars.GetAll()
    count = int(coll.Count) if coll is not None else 0
    ids = []
    for i in range(1, count + 1):
        try:
            item = coll.Get(i)
            ids.append(int(item.Number))
        except Exception:
            continue
    return ids


def _real_case_ids(bridge) -> list:
    """Real, existing case numbers, including combinations (T2)."""
    out = [num for num, _ in bridge._iter_all_cases()]
    return out


def _bar_length(bridge, bar_id: int) -> float:
    """Euclidean length from the bar's node coordinates (m)."""
    bar = bridge.structure.Bars.Get(bar_id)
    n1, n2 = int(bar.StartNode), int(bar.EndNode)
    n1o, n2o = bridge.structure.Nodes.Get(n1), bridge.structure.Nodes.Get(n2)
    dx = float(n2o.X) - float(n1o.X)
    dy = float(n2o.Y) - float(n1o.Y)
    dz = float(n2o.Z) - float(n1o.Z)
    return math.sqrt(dx * dx + dy * dy + dz * dz)


def _section_a_i(bridge, section_name: str) -> tuple[float, float]:
    """(A m2, I_minor m4) for a section label via the empirical GetValue
    map (0=A, 4/5=I - probed live). Uses the SMALLER of the two principal
    inertias (conservative, weak-axis)."""
    data = bridge.structure.Labels.Get(RobotEnum.I_LT_BAR_SECTION, str(section_name)).Data
    a = float(data.GetValue(0))
    i1, i2 = float(data.GetValue(4)), float(data.GetValue(5))
    return a, min(i1, i2)


def _bar_material_e_pa(bridge, bar_id: int) -> tuple[float | None, str]:
    """(E in Pa, material_name) for a bar. Lookup order matches
    get_utilization_ratios' strength lookup: the bar's own material label,
    then the section's material reference. Returns (None, reason) if no
    material/E can be found."""
    labels = bridge.structure.Labels

    def _e_of_label(mat_name: str) -> float | None:
        try:
            data = labels.Get(RobotEnum.I_LT_MATERIAL, mat_name).Data
            e_pa = float(data.E or 0.0)
            return e_pa if e_pa > 0.0 else None
        except Exception:
            return None

    bar = bridge.structure.Bars.Get(bar_id)
    mat_name = ""
    try:
        if bar.HasLabel(RobotEnum.I_LT_MATERIAL):
            mat_name = str(bar.GetLabelName(RobotEnum.I_LT_MATERIAL))
    except Exception:
        mat_name = ""
    if mat_name:
        e = _e_of_label(mat_name)
        if e is not None:
            return e, mat_name
        return None, f"material '{mat_name}' has no E"
    try:
        sec_name = str(bar.GetLabelName(RobotEnum.I_LT_BAR_SECTION))
        from tools.robot_tool import CastTo

        sdata = CastTo(
            labels.Get(RobotEnum.I_LT_BAR_SECTION, sec_name).Data, "IRobotBarSectionData"
        )
        sec_mat = str(sdata.MaterialName or "")
        if sec_mat:
            e = _e_of_label(sec_mat)
            if e is not None:
                return e, sec_mat
            return None, f"section material '{sec_mat}' has no E"
    except Exception:
        pass
    return None, "no material label or section material found"


def check_euler_buckling(
    bridge,
    bar_id: int,
    case_or_combination_id: int,
    effective_length_factor: float = 1.0,
    axial_force_kn: float | None = None,
) -> dict[str, Any]:
    """Basic Euler buckling check for a single member.

    Applies only to members in COMPRESSION (negative axial force per the
    live-probed convention on this build). Tension members return a clear
    "not applicable" result instead of a misleading number.

    Returns
    -------
    dict with keys:
      applies            - bool; False for tension / invalid cases
      reason             - str explanation when applies=False
      Pcr_kN             - critical buckling load (or None)
      applied_axial_kN   - solved axial force magnitude in the given case
                            (or None)
      slenderness_KL_r   - K*L/r (or None)
      pass_fail          - bool: |N| <= Pcr (or None)
      note               - the standing EULER_NOTE caveat

    Raises ValueError for nonexistent bar_id / case id (T2 real-existence
    check - a bare .Get() would silently auto-create a proxy).
    """
    real_bars = _real_bar_ids(bridge)
    if int(bar_id) not in real_bars:
        raise ValueError(
            f"bar {bar_id} does not exist in the model "
            f"(real bars: {sorted(real_bars)[:10]}{'...' if len(real_bars) > 10 else ''})."
        )
    real_cases = _real_case_ids(bridge)
    if int(case_or_combination_id) not in real_cases:
        raise ValueError(
            f"case/combination {case_or_combination_id} does not exist "
            f"(real cases: {sorted(real_cases)})."
        )

    if axial_force_kn is not None:
        # Runner already exported forces once - reuse to avoid a second,
        # full-model force export per bar.
        axial_kN = float(axial_force_kn)
    else:
        # Solved axial force at the MIDSPAN station (peak moment station); the
        # axial force is roughly constant along a member, so a single reliable
        # station is sufficient for the screening check.
        try:
            df = bridge.export_all_member_forces(case_id=int(case_or_combination_id), divisions=2)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"Could not read member forces for bar {bar_id} in case "
                f"{case_or_combination_id}: {exc}"
            ) from exc
        sub = df[df["Bar_ID"] == int(bar_id)] if df is not None else None
        if sub is None or sub.empty:
            raise RuntimeError(
                f"No force results for bar {bar_id} in case "
                f"{case_or_combination_id} - has the model been solved?"
            )
        axial_kN = float(sub["FX_kN"].iloc[len(sub) // 2])

    if axial_kN >= 0.0:
        return {
            "applies": False,
            "reason": "member in tension",
            "Pcr_kN": None,
            "applied_axial_kN": round(axial_kN, 4),
            "slenderness_KL_r": None,
            "pass_fail": None,
            "note": EULER_NOTE,
        }

    length_m = _bar_length(bridge, int(bar_id))
    if length_m <= 0.0:
        raise RuntimeError(f"bar {bar_id} has zero/unknown length.")

    bar = bridge.structure.Bars.Get(int(bar_id))
    sec_name = ""
    try:
        sec_name = str(bar.GetLabelName(RobotEnum.I_LT_BAR_SECTION))
    except Exception:
        pass
    if not sec_name:
        raise RuntimeError(f"bar {bar_id} has no section assigned.")
    area_m2, i_minor_m4 = _section_a_i(bridge, sec_name)
    e_pa, mat_name = _bar_material_e_pa(bridge, int(bar_id))
    if e_pa is None or e_pa <= 0.0:
        raise RuntimeError(
            f"bar {bar_id}: {mat_name or 'no material E'} - cannot compute Euler Pcr."
        )

    k = float(effective_length_factor)
    kl = k * length_m
    pcr_n = math.pi * math.pi * e_pa * i_minor_m4 / (kl * kl)
    radius = math.sqrt(i_minor_m4 / area_m2) if area_m2 > 0.0 else 0.0
    slenderness = (kl / radius) if radius > 0.0 else float("inf")
    applied_n = abs(axial_kN) * 1000.0
    return {
        "applies": True,
        "reason": "",
        "Pcr_kN": round(pcr_n / 1000.0, 4),
        "applied_axial_kN": round(applied_n / 1000.0, 4),
        "slenderness_KL_r": round(slenderness, 3),
        "pass_fail": bool(applied_n <= pcr_n),
        "note": EULER_NOTE,
        "bar_id": int(bar_id),
        "section": sec_name,
        "material": mat_name,
        "length_m": round(length_m, 3),
        "E_GPa": round(e_pa / 1e9, 2),
    }
