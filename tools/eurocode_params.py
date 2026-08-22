"""
tools/eurocode_params.py
========================
[EUROCODE — eurocode_scope.md decisions D1, D2, D3]
Shared EN 1993 parameter tables and constants.

Everything here is DATA (as the scope doc demands: tables are lookup data,
never derived formulas), except the tiny pure lookups that read them.

D1 — Partial safety factors: EN recommended values as CONFIGURABLE module
constants (National-Annex override point). Formulas must import these,
never inline a gamma.

D2 — Steel grades S235/S275/S355/S460 per EN 10025-2: thickness-dependent
fy (Table 7) and nominal fu (Table 3.1). Source-of-truth rule: Robot's
material RE is used where authoritative, but capped by the EN table value
at the section's actual flange thickness.

D3 — Buckling curves: imperfection factors (Table 6.1), rolled-I curve
selection (Table 6.2), LTB curve/alpha_LT (Tables 6.3/6.4 + 6.5), and the
§6.3.2.2 general-method constants (lambda_LT,0 = 0.4, beta = 0.75).

Pure Python — no COM.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

# ----------------------------------------------------------------------
# D1 — Partial safety factors (EN recommended values; National-Annex
# override point)
# ----------------------------------------------------------------------
GAMMA_M0: float = 1.0    # cross-section resistance
GAMMA_M1: float = 1.0    # member buckling resistance
GAMMA_M2: float = 1.25   # connections (bolts / welds / bearing)

# ----------------------------------------------------------------------
# D2 — Steel grades, EN 10025-2 (fy thickness bands in mm, fu nominal)
# ----------------------------------------------------------------------
#: (t_min_exclusive, t_max_inclusive_mm, fy_MPa) bands per EN 10025-2 T7.
GRADE_FY_BANDS: Dict[str, List[Tuple[float, float, float]]] = {
    "S235": [(0, 16, 235), (16, 40, 225), (40, 63, 215), (63, 80, 215),
             (80, 100, 215), (100, 150, 195), (150, 200, 185),
             (200, 250, 175)],
    "S275": [(0, 16, 275), (16, 40, 265), (40, 63, 255), (63, 80, 245),
             (80, 100, 235), (100, 150, 225), (150, 200, 215),
             (200, 250, 205)],
    "S355": [(0, 16, 355), (16, 40, 345), (40, 63, 335), (63, 80, 325),
             (80, 100, 315), (100, 150, 295), (150, 200, 285),
             (200, 250, 275)],
    "S460": [(0, 16, 460), (16, 40, 440), (40, 63, 430), (63, 80, 410),
             (80, 100, 400), (100, 150, 380), (150, 200, 360),
             (200, 250, 340)],
}
#: Nominal ultimate strength fu (EN 10025-2 Table 3.1, t <= 100 mm band).
GRADE_FU_MPA: Dict[str, float] = {
    "S235": 360.0, "S275": 430.0, "S355": 470.0, "S460": 550.0,
}
GRADES: Tuple[str, ...] = ("S235", "S275", "S355", "S460")


def fy_for_grade(grade: str, t_mm: float) -> Optional[float]:
    """EN 10025-2 nominal yield strength (MPa) for ``grade`` at thickness
    ``t_mm`` (flange thickness for flanges, web thickness for webs, per the
    standard's location-dependent rule). Returns None for unknown grades."""
    grade = str(grade or "").strip().upper()
    bands = GRADE_FY_BANDS.get(grade)
    if not bands:
        return None
    for lo, hi, fy in bands:
        if lo < float(t_mm) <= hi:
            return float(fy)
    # Above the last band (t > 250 mm): use the lowest published value.
    return float(bands[-1][2])


def fu_for_grade(grade: str) -> Optional[float]:
    """EN 10025-2 nominal ultimate strength (MPa)."""
    return GRADE_FU_MPA.get(str(grade or "").strip().upper())


_GRADE_RE = re.compile(r"\bS(235|275|355|460)\b")


def effective_yield_strength(re_mpa: Optional[float], material_name: str,
                             t_mm: float) -> Tuple[Optional[float], str]:
    """D2 source-of-truth rule: the design fy for an element.

    When the material name declares an EN grade (e.g. "S355", "S355 J2"),
    the EN 10025-2 nominal fy at thickness ``t_mm`` caps the value — a
    material declared S355 must not be credited 355 MPa on a flange thicker
    than 16 mm. When no grade is declared, Robot's RE is used as-is
    (the repo's verified source of truth).

    Returns (fy_MPa or None, source_description).
    """
    name = str(material_name or "").strip().upper()
    m = _GRADE_RE.search(name)
    grade = ("S" + m.group(1)) if m else None
    en_fy = fy_for_grade(grade, t_mm) if grade else None
    if en_fy is None:
        return (re_mpa, "RE (no declared EN grade)")
    if re_mpa is None:
        return (en_fy, f"EN 10025-2 {grade} @ t={t_mm:g} mm")
    return (min(re_mpa, en_fy),
            f"min(RE, EN 10025-2 {grade} @ t={t_mm:g} mm)")


# ----------------------------------------------------------------------
# D3 — Buckling curves
# ----------------------------------------------------------------------
#: EN 1993-1-1 Table 6.1 imperfection factors.
IMPERFECTION_ALPHA: Dict[str, float] = {
    "a0": 0.13, "a": 0.21, "b": 0.34, "c": 0.49, "d": 0.76,
}
#: LTB imperfection factors (Table 6.5) keyed by curve name.
ALPHA_LT: Dict[str, float] = {
    "a": 0.21, "b": 0.34, "c": 0.49, "d": 0.76,
}
#: EN 1993-1-1 §6.3.2.2 general-method constants.
LAMBDA_LT_0: float = 0.4    # plateau limit
BETA_LT: float = 0.75


def rolled_i_buckling_curve(h_m: float, b_m: float, tf_m: float,
                            axis: str) -> str:
    """EN 1993-1-1 Table 6.2: buckling curve for a rolled I-section.

    ``axis``: "y" (major) or "z" (minor). All dimensions in metres.
    """
    hb = float(h_m) / float(b_m)
    tf = float(tf_m) * 1000.0  # mm
    if tf > 100.0:
        return "d"
    if hb <= 1.2:
        return "a" if axis == "y" else "b"
    if hb <= 3.0:
        return "b" if axis == "y" else "c"
    return "c" if axis == "y" else "d"


def alpha_lt_for_rolled_i(h_m: float, b_m: float) -> float:
    """EN 1993-1-1 Tables 6.3/6.4 + 6.5: alpha_LT for a rolled I-section
    (h/b <= 2 -> curve a; h/b > 2 -> curve b)."""
    curve = "a" if float(h_m) / float(b_m) <= 2.0 else "b"
    return ALPHA_LT[curve]


def lt_buckling_curve_for_rolled_i(h_m: float, b_m: float) -> str:
    """The LTB buckling curve letter for a rolled I-section (Table 6.3/6.4)."""
    return "a" if float(h_m) / float(b_m) <= 2.0 else "b"
