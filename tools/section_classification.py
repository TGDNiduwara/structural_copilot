"""
tools/section_classification.py
===============================
[EUROCODE Phase B — eurocode_scope.md §4, decision D7]
Cross-section classification per EN 1993-1-1 Table 5.2.

Eurocode resistance formulas differ for Class 1-4 sections (plastic vs
elastic vs effective-width), so classification MUST precede any resistance
calculation. This module implements Table 5.2 parts 1 (internal
compression parts — webs) and 2 (outstand flanges, rolled) as pure lookup
data:

    Internal parts, subject to bending:     C1 c/t <= 72e   C2 <= 83e
                                            C3 <= 124e
    Internal parts, subject to compression: C1 c/t <= 33e   C2 <= 38e
                                            C3 <= 42e
    Outstand flanges (rolled):              C1 c/t <= 9e    C2 <= 10e
                                            C3 <= 14e
    e = sqrt(235 / fy)

Geometric c values follow the Table 5.2 definitions for ROLLED sections:
    flange outstand c = (b - tw)/2 - r
    web clear height  c = h - 2*tf - 2*r
(both include the root radius, per the rolled-section branch of the table).

Stress states supported in v1:
    "bending"      -> web bending limits, flange outstand limits
    "compression"  -> web compression limits, flange outstand limits
    "combined"     -> conservative: the web is checked with the more
                      onerous PURE-COMPRESSION limits (a web in
                      bending+compression can only be less slender than
                      the pure-compression case); flange outstand limits.
                      Stated, never hidden.

Class 4 sections return class=4 — the CALLER (resistance/LTB module) must
then mark the member NOT_CHECKABLE per D7 (never silently apply a Class 3
formula to a Class 4 section). This module itself only classifies.

PURE — no COM. Takes a section-props dict (tools.section_data) + fy.

Author: Principal Structural Software Architecture Team
"""

from __future__ import annotations

import math
from typing import Any

from tools.section_data import flange_outstand, has_full_dims, web_clear_height

#: (C1, C2, C3) c/t limits per Table 5.2 (multiply by epsilon).
_WEB_BENDING = (72.0, 83.0, 124.0)
_WEB_COMPRESSION = (33.0, 38.0, 42.0)
_FLANGE_ROLLED = (9.0, 10.0, 14.0)


def _class_of(c_over_t: float, limits: tuple[float, float, float], eps: float) -> int:
    """Class 1-4 for an element with the given c/t and Table 5.2 limits."""
    ratio = float(c_over_t)
    if ratio <= limits[0] * eps:
        return 1
    if ratio <= limits[1] * eps:
        return 2
    if ratio <= limits[2] * eps:
        return 3
    return 4


def classify_section(
    props: dict[str, Any], fy_mpa: float, stress_state: str = "bending"
) -> dict[str, Any]:
    """Classifies a section per Table 5.2.

    Parameters
    ----------
    props : section-props dict from tools.section_data (h, b, tw, tf, r,
            shape_kind, complete)
    fy_mpa : design yield strength (MPa) at the element's actual thickness
    stress_state : "bending" | "compression" | "combined"

    Returns
    -------
    {"class": 1..4, "web_class", "flange_class", "eps",
     "web_c_t", "flange_c_t", "web_limit", "flange_limit",
     "stress_state", "reason"} — ``reason`` explains the governing element.
    """
    fy = float(fy_mpa)
    if fy <= 0.0:
        raise ValueError("classify_section requires fy_mpa > 0")
    state = str(stress_state or "bending").lower()
    if state not in ("bending", "compression", "combined"):
        raise ValueError(
            f"stress_state must be 'bending'|'compression'|'combined', got '{stress_state}'"
        )
    if not has_full_dims(props):
        return {
            "class": None,
            "web_class": None,
            "flange_class": None,
            "eps": None,
            "web_c_t": None,
            "flange_c_t": None,
            "stress_state": state,
            "reason": "section dimensions unavailable (parametric/custom "
            "without live GetValue data) — NOT_CHECKABLE",
        }

    eps = math.sqrt(235.0 / fy)
    kind = props.get("shape_kind")
    h, b, tw, tf, r = (props["h_m"], props["b_m"], props["tw_m"], props["tf_m"], props["r_m"])

    if kind == "circular_hollow":
        # EN 1993-1-1 Table 5.2 sheet 3 (circular hollow sections). The
        # single D/t series applies to BOTH compression and bending:
        #   Class 1: D/t <= 50e2   Class 2: D/t <= 70e2   Class 3: D/t <= 90e2
        D = max(h, b)  # h == b == outer diameter for CHS
        t = tw  # wall thickness
        d_over_t = D / t if t > 0.0 else float("inf")
        e2 = eps * eps
        cls = (
            1
            if d_over_t <= 50.0 * e2
            else (2 if d_over_t <= 70.0 * e2 else (3 if d_over_t <= 90.0 * e2 else 4))
        )
        reason = (
            f"circular hollow (Table 5.2 sheet 3): D/t={d_over_t:.2f} "
            f"vs 50e2={50.0 * e2:.2f}/70e2={70.0 * e2:.2f}/90e2={90.0 * e2:.2f}"
        )
        if cls >= 4:
            reason += (
                "; Class 4 - effective width per EN 1993-1-5 is out "
                "of v1 scope, so the section is NOT_CHECKABLE (D7)."
            )
        return {
            "class": cls,
            "web_class": cls,
            "flange_class": cls,
            "eps": round(eps, 4),
            "web_c_t": round(d_over_t, 3),
            "flange_c_t": round(d_over_t, 3),
            "web_clear_mm": round(D * 1000.0, 2),
            "flange_outstand_mm": round(t * 1000.0, 2),
            "stress_state": state,
            "reason": reason,
        }

    if kind == "rect_hollow":
        # EN 1993-1-1 Table 5.2 sheet 1 (internal compression parts) for
        # rectangular/square hollow sections. Flat width c = b - 3t / h - 3t
        # (allows for the corner radius r ~= 1.5t, standard UK practice).
        web_c = max(h - 3.0 * tw, 0.0)
        fl_c = max(b - 3.0 * tf, 0.0)
        web_c_t = web_c / tw if tw > 0.0 else float("inf")
        fl_c_t = fl_c / tf if tf > 0.0 else float("inf")
        limits = _WEB_COMPRESSION if state in ("compression", "combined") else _WEB_BENDING
        web_cls = _class_of(web_c_t, limits, eps)
        fl_cls = _class_of(fl_c_t, limits, eps)
        overall = max(web_cls, fl_cls)
        reason = (
            f"rectangular hollow (Table 5.2 sheet 1, internal parts): "
            f"web (h-3t)/t={web_c_t:.2f}, flange (b-3t)/t={fl_c_t:.2f}"
        )
        if overall >= 4:
            reason += (
                "; Class 4 - effective width per EN 1993-1-5 is out "
                "of v1 scope, so the section is NOT_CHECKABLE (D7)."
            )
        return {
            "class": overall,
            "web_class": web_cls,
            "flange_class": fl_cls,
            "eps": round(eps, 4),
            "web_c_t": round(web_c_t, 3),
            "flange_c_t": round(fl_c_t, 3),
            "web_clear_mm": round(web_c * 1000.0, 2),
            "flange_outstand_mm": round(fl_c * 1000.0, 2),
            "stress_state": state,
            "reason": reason,
        }

    web_c = web_clear_height(h, tf, r)
    fl_c = flange_outstand(b, tw, r)
    web_c_t = web_c / tw if tw > 0.0 else float("inf")
    fl_c_t = fl_c / tf if tf > 0.0 else float("inf")

    web_limits = _WEB_COMPRESSION if state in ("compression", "combined") else _WEB_BENDING
    web_cls = _class_of(web_c_t, web_limits, eps)
    fl_cls = _class_of(fl_c_t, _FLANGE_ROLLED, eps)

    overall = max(web_cls, fl_cls)
    if overall >= 4:
        reason = (
            "Class 4 (slender) — web and/or flange exceed the Class 3 "
            "limit; effective width per EN 1993-1-5 is out of v1 "
            "scope, so the section is NOT_CHECKABLE (D7)."
        )
    elif web_cls > fl_cls:
        reason = f"web governs (Class {web_cls}): c/tw={web_c_t:.2f}"
    else:
        reason = f"flange governs (Class {fl_cls}): c/tf={fl_c_t:.2f}"
    return {
        "class": overall,
        "web_class": web_cls,
        "flange_class": fl_cls,
        "eps": round(eps, 4),
        "web_c_t": round(web_c_t, 3),
        "flange_c_t": round(fl_c_t, 3),
        "web_clear_mm": round(web_c * 1000.0, 2),
        "flange_outstand_mm": round(fl_c * 1000.0, 2),
        "stress_state": state,
        "reason": reason,
    }
