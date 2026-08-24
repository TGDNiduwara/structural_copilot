"""
tools/connection_check.py
=========================
[EUROCODE Phase D — eurocode_scope.md D5/D7/D8]
Simple shear connection checks per EN 1993-1-8 §3 (bolts) and §4 (welds).

SCOPE (eurocode_scope.md D5, verbatim): SIMPLE SHEAR CONNECTIONS only —
fin plate / double angle / end plate in shear. NO moment connections, NO
base plates in v1. The governing failure mode is always named (bolt
shear / bearing / block shear / weld).

Checks implemented:
  * bolt shear            Fv,Rd = alpha_v * fub * A / gamma_M2   (Table 3.4)
  * bearing (plate + web) Fb,Rd = k1 * alpha_b * fu * d * t / gamma_M2
  * block shear           Veff,1,Rd = fu*Ant/gamma_M2 + fy*Anv/(sqrt3*gamma_M0)
                          §3.10.2 — v1 geometry model documented below
  * fillet weld           directional method §4.5.3 (pure-shear branch):
                          Fw,Rd = (fu/(sqrt3*beta_w*gamma_M2)) * a * L

BLOCK SHEAR v1 GEOMETRY MODEL (single vertical line of n bolts at the
plate end — the classic fin-plate case), stated so it can be validated
against the D8 SCI "Green Book" worked example when the published numbers
are pasted:
    Ant = t * (e1 - d0/2)
    Anv = 2*t*((n-1)*p1 + e1) - n*t*d0
    Veff,1,Rd = fu*Ant/gamma_M2 + fy*Anv/(sqrt3*gamma_M0)

Connections are defined by an engineer-side-table (Robot has no
connection server) stored on the bridge as `bridge.connections` — same
default-and-warn discipline as the bracing registry. PURE core takes
plain numbers and runs in offline tests.

Author: Principal Structural Software Architecture Team
"""

from __future__ import annotations

import math
from typing import Any

from tools.eurocode_params import GAMMA_M0, GAMMA_M2, fu_for_grade, fy_for_grade

#: Constant caveat attached to every result (repo discipline).
CONNECTION_NOTE = (
    "EN 1993-1-8 simple shear connection only (fin plate / double angle / "
    "end plate). No moment connections or base plates (v1). Block shear "
    "uses the v1 single-line end-bolt geometry model — validate against "
    "the D8 Green Book example when published numbers are available."
)

#: Bolt grades -> nominal ultimate strength fub (MPa), EN 1993-1-8 §3.
BOLT_GRADES: dict[str, float] = {
    "4.6": 400.0,
    "5.6": 500.0,
    "8.8": 800.0,
    "10.9": 1000.0,
}
#: Table 3.4 alpha_v per bolt class.
ALPHA_V: dict[str, float] = {
    "4.6": 0.6,
    "5.6": 0.6,
    "8.8": 0.6,
    "4.8": 0.5,
    "5.8": 0.5,
    "6.8": 0.5,
    "10.9": 0.5,
}
#: EN 1993-1-8 Table 4.1 correlation factor beta_w per grade.
BETA_W: dict[str, float] = {
    "S235": 0.80,
    "S275": 0.85,
    "S355": 0.90,
    "S460": 1.00,
}
#: Shear planes per connection type (fin/end plate = single shear,
#: double angle = double shear).
SHEAR_PLANES = {"fin_plate": 1, "end_plate": 1, "double_angle": 2}

CONNECTION_DEFAULTS: dict[str, Any] = {
    "connection_type": "fin_plate",
    "bolt_grade": "8.8",
    "bolt_diameter_mm": 20,
    "bolt_rows": 2,  # n bolts in the vertical line
    "bolt_columns": 1,  # horizontal lines (1 for v1 single-line model)
    "pitch_mm": 60.0,  # p1 vertical pitch
    "gauge_mm": 60.0,  # p2 horizontal pitch
    "edge_dist_mm": 30.0,  # e2 side edge distance
    "end_dist_mm": 30.0,  # e1 end distance
    "plate_thickness_mm": 10.0,
    "plate_grade": "S275",
    "weld_leg_mm": None,
}


class ConnectionRegistry:
    """Engineer-side connection table: bar_id -> {joint_end: conn}."""

    def __init__(self) -> None:
        self._entries: dict[int, dict[str, dict[str, Any]]] = {}

    def set_connection(self, bar_id: int, joint_end: str = "end", **kwargs) -> dict[str, Any]:
        bar_id = int(bar_id)
        joint_end = str(joint_end or "end").lower()
        if joint_end not in ("start", "end"):
            raise ValueError("joint_end must be 'start' or 'end'")
        conn = dict(CONNECTION_DEFAULTS)
        conn.update({k: v for k, v in kwargs.items() if v is not None})
        if conn["bolt_diameter_mm"] is None:
            conn["bolt_diameter_mm"] = CONNECTION_DEFAULTS["bolt_diameter_mm"]
        if conn["bolt_grade"] not in BOLT_GRADES:
            raise ValueError(
                f"bolt_grade must be one of {sorted(BOLT_GRADES)}, got '{conn['bolt_grade']}'"
            )
        if conn["connection_type"] not in SHEAR_PLANES:
            raise ValueError(
                f"connection_type must be one of {sorted(SHEAR_PLANES)}, "
                f"got '{conn['connection_type']}'"
            )
        if conn["bolt_columns"] != 1:
            raise ValueError(
                "v1 block-shear model supports a single bolt column only "
                "(bolt_columns=1); multi-column layouts are NOT_CHECKABLE."
            )
        self._entries.setdefault(bar_id, {})[joint_end] = conn
        return dict(conn)

    def get(self, bar_id: int, joint_end: str = "end") -> dict[str, Any] | None:
        entry = self._entries.get(int(bar_id), {}).get(str(joint_end or "end").lower())
        return dict(entry) if entry else None

    def all_connections(self) -> list[dict[str, Any]]:
        out = []
        for bid, ends in self._entries.items():
            for end, conn in ends.items():
                out.append({"bar_id": bid, "joint_end": end, **conn})
        return out

    def remove(self, bar_id: int) -> bool:
        return self._entries.pop(int(bar_id), None) is not None

    def clear(self) -> int:
        n = len(self._entries)
        self._entries.clear()
        return n

    def __len__(self) -> int:
        return len(self._entries)


# ----------------------------------------------------------------------
# Pure resistance functions (EN 1993-1-8 Table 3.4 / §3.10.2 / §4.5.3)
# ----------------------------------------------------------------------


def bolt_area(d_mm: float) -> float:
    """Gross bolt shank area A = pi*d^2/4 (mm2)."""
    d = float(d_mm)
    return math.pi * d * d / 4.0


def bolt_shear_resistance(
    bolt_grade: str, d_mm: float, n_bolts: int, planes: int = 1, gamma_m2: float = GAMMA_M2
) -> float:
    """Total Fv,Rd (N) = alpha_v * fub * A / gamma_M2, per bolt x n x planes."""
    fub = BOLT_GRADES[bolt_grade] * 1e6  # Pa
    alpha_v = ALPHA_V.get(bolt_grade, 0.6)
    per_bolt = alpha_v * fub * bolt_area(d_mm) * 1e-6 / gamma_m2
    return per_bolt * int(n_bolts) * int(planes)


def _hole_diameter(d_mm: float) -> float:
    """Standard clearance hole d0 = d + 2 mm."""
    return float(d_mm) + 2.0


def bearing_resistance(
    fu_mpa: float,
    d_mm: float,
    t_mm: float,
    e1_mm: float,
    e2_mm: float,
    p1_mm: float,
    p2_mm: float,
    n_bolts: int,
    fub_mpa: float,
    gamma_m2: float = GAMMA_M2,
) -> float:
    """Total Fb,Rd (N) for the bolt group on one ply, Table 3.4.

    v1 model: single column of ``n_bolts`` bolts (``bolt_columns=1``).
    alpha_d and k1 use the END-bolt / EDGE-bolt branches (conservative for
    a single-line group):
        alpha_b = min(alpha_d, fub/fu, 1.0),  alpha_d = e1/(3*d0)
        k1      = min(2.8*e2/d0 - 1.7, 2.5)
    Per-bolt Fb,Rd = k1 * alpha_b * fu * d * t / gamma_M2, summed over the
    group (load sharing assumed equal).
    """
    d0 = _hole_diameter(d_mm)
    alpha_d = float(e1_mm) / (3.0 * d0)
    alpha_b = min(alpha_d, float(fub_mpa) / float(fu_mpa), 1.0)
    k1 = min(2.8 * float(e2_mm) / d0 - 1.7, 2.5)
    per_bolt = (
        k1 * alpha_b * float(fu_mpa) * 1e6 * float(d_mm) * 1e-3 * float(t_mm) * 1e-3 / gamma_m2
    )
    return per_bolt * max(int(n_bolts), 1)


def block_shear_resistance(
    fy_mpa: float,
    fu_mpa: float,
    t_mm: float,
    e1_mm: float,
    p1_mm: float,
    n_bolts: int,
    d_mm: float,
    gamma_m2: float = GAMMA_M2,
    gamma_m0: float = GAMMA_M0,
) -> float:
    """Veff,1,Rd (N) per EN 1993-1-8 §3.10.2 with the v1 geometry model
    (single vertical line of n bolts at the plate end):
        Ant = t*(e1 - d0/2)
        Anv = 2*t*((n-1)*p1 + e1) - n*t*d0
    """
    d0 = _hole_diameter(d_mm)
    ant = float(t_mm) * (float(e1_mm) - d0 / 2.0)  # mm2
    anv = (
        2.0 * float(t_mm) * (float(n_bolts) - 1.0) * float(p1_mm)
        + 2.0 * float(t_mm) * float(e1_mm)
        - float(n_bolts) * float(t_mm) * d0
    )
    # MPa * mm2 = N — no further scaling.
    return float(fu_mpa) * ant / gamma_m2 + float(fy_mpa) * anv / (math.sqrt(3.0) * gamma_m0)


def weld_resistance(
    fu_mpa: float,
    weld_leg_mm: float,
    weld_length_mm: float,
    plate_grade: str = "S275",
    gamma_m2: float = GAMMA_M2,
) -> float:
    """Fw,Rd (N) for two fillet welds (both sides of the fin plate),
    directional method §4.5.3 pure-shear branch:
        Fw,Rd = (fu / (sqrt3 * beta_w * gamma_M2)) * a * L
    with throat a = 0.707 * leg and total weld length 2 * weld_length.
    """
    beta_w = BETA_W.get(str(plate_grade or "S275").upper(), 0.85)
    throat = 0.707 * float(weld_leg_mm)
    a_area = 2.0 * throat * float(weld_length_mm)  # mm2 (both sides)
    # MPa * mm2 = N — no further scaling.
    return (float(fu_mpa) / (math.sqrt(3.0) * beta_w * gamma_m2)) * a_area


def check_simple_shear_connection(
    conn: dict[str, Any],
    v_ed_n: float,
    member_fu_mpa: float | None = None,
    member_web_t_mm: float | None = None,
) -> dict[str, Any]:
    """Full simple-shear connection check (PURE).

    ``conn`` is a connection dict (ConnectionRegistry shape).
    ``member_fu_mpa`` / ``member_web_t_mm`` are the BEAM properties used
    for the web-bearing check (None -> bearing-on-web skipped with a
    note). Returns per-check utilizations, the governing mode and
    PASS / FAIL / NOT_CHECKABLE.
    """
    plate_grade = str(conn.get("plate_grade") or "S275").upper()
    if conn.get("bolt_grade") not in BOLT_GRADES:
        return {
            "status": "NOT_CHECKABLE",
            "reason": "no usable connection definition — a valid bolt_grade is required.",
            "note": CONNECTION_NOTE,
        }
    fu_plate = fu_for_grade(plate_grade) or 430.0
    fy_plate = fy_for_grade(plate_grade, conn.get("plate_thickness_mm", 10.0)) or 275.0
    fub = BOLT_GRADES[conn["bolt_grade"]]
    d = float(conn["bolt_diameter_mm"])
    n = int(conn.get("bolt_rows", 2))
    t = float(conn.get("plate_thickness_mm", 10.0))
    e1 = float(conn.get("end_dist_mm", 30.0))
    e2 = float(conn.get("edge_dist_mm", 30.0))
    p1 = float(conn.get("pitch_mm", 60.0))
    p2 = float(conn.get("gauge_mm", 60.0))
    planes = SHEAR_PLANES.get(conn.get("connection_type", "fin_plate"), 1)
    v_ed = abs(float(v_ed_n))

    checks: dict[str, float] = {}

    def add(key: str, rd_n: float) -> None:
        checks[key] = v_ed / rd_n if rd_n > 0.0 else float("inf")

    if conn.get("bolt_grade") in BOLT_GRADES:
        add("bolt_shear", bolt_shear_resistance(conn["bolt_grade"], d, n, planes))
    add("bearing_plate", bearing_resistance(fu_plate, d, t, e1, e2, p1, p2, n, fub))
    if member_fu_mpa and member_web_t_mm:
        add(
            "bearing_web",
            bearing_resistance(member_fu_mpa, d, float(member_web_t_mm), e1, e2, p1, p2, n, fub),
        )
    add("block_shear", block_shear_resistance(fy_plate, fu_plate, t, e1, p1, n, d))
    weld_leg = conn.get("weld_leg_mm")
    if weld_leg:
        weld_len = (n - 1) * p1 + 2.0 * e1
        add("weld", weld_resistance(fu_plate, float(weld_leg), weld_len, plate_grade))

    if not checks:
        return {
            "status": "NOT_CHECKABLE",
            "reason": "no checkable connection defined.",
            "note": CONNECTION_NOTE,
        }
    gov_key = max(checks, key=checks.get)
    util = checks[gov_key]
    return {
        "status": "PASS" if util <= 1.0 else "FAIL",
        "v_ed_kN": round(v_ed / 1e3, 3),
        "utilization": round(util, 4),
        "governing": gov_key,
        "checks": {k: round(v, 4) for k, v in checks.items()},
        "plate_grade": plate_grade,
        "bolt_grade": conn["bolt_grade"],
        "bolt_diameter_mm": d,
        "bolts": n,
        "note": CONNECTION_NOTE,
    }
