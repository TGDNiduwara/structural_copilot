"""
tools/section_data.py
=====================
[EUROCODE — eurocode_scope.md §5/§6, probe-verified GetValue map]
Live cross-section data extraction + derived torsion/warping constants.

Verified GetValue map (tools/probe_section_data.py, live Robot 2027):
  [0]=A, [4]=Iy (major), [5]=Iz (minor), [6]/[7]=b/2, [8]/[9]=h/2,
  [12]=h, [13]=b, [14]=tw, [15]=tf, [16]=r (root radius), [19]=Wpl,y,
  [20]=Wpl,z, [36]=nominal depth (mm).
  It (torsion) and Iw (warping) are NOT exposed anywhere (probed 0-150
  + ElasticParams) -> COMPUTED from the live dimensions via the standard
  closed forms below (per eurocode_scope.md §5).

Verified ShapeType codes: IPE=20, IPN=25, HEA=10, HEB=12, HEM=14
  (doubly-symmetric I); UPE=37, UPN=38 (channels); L=1 (angle).

The extraction core is PURE — it takes a GetValue-style callable so it
runs in offline tests. read_section_props() is the COM-facing wrapper.

Author: Principal Structural Software Architecture Team
"""

from __future__ import annotations

import math
from typing import Any, Callable, Dict, Optional

#: ShapeType codes verified as doubly-symmetric rolled I-sections.
SHAPE_TYPE_I: set = {10, 12, 14, 20, 25}
#: ShapeType code -> engineering shape kind (verified subset; anything
#: unlisted is "other" until probed).
SHAPE_TYPE_KIND: Dict[int, str] = {
    1: "angle", 10: "i", 12: "i", 14: "i", 20: "i", 25: "i",
    37: "channel", 38: "channel",
    # [CHS 2026-08-23] live-probed ShapeType codes (UKST catalog):
    #   36 = circular hollow section (CHS): h=b=D, tw=tf=t, r=0
    #   47 = rectangular/square hollow section (RHS/SHS): full dims incl. r
    36: "circular_hollow", 47: "rect_hollow",
}


def shape_kind(shape_type: Any) -> str:
    """Engineering shape kind from the Robot ShapeType code (data, not
    name-parsing): 'i' | 'channel' | 'angle' | 'other'."""
    return SHAPE_TYPE_KIND.get(int(shape_type), "other")


def extract_section_props(getvalue: Callable[[int], Any],
                          shape_type: Any) -> Dict[str, Any]:
    """PURE: reads the probe-verified GetValue map into a props dict.

    ``getvalue(i)`` returns the Data.GetValue(i) value (or raises for
    unsupported indices). Returns a dict with all values in SI units
    (m / m2 / m3 / m4) plus the derived Wy and a ``complete`` flag.
    """
    def v(i: int) -> float:
        try:
            return float(getvalue(i)) or 0.0
        except Exception:
            return 0.0

    area, iy, iz = v(0), v(4), v(5)
    h, b = v(12), v(13)
    tw, tf, r = v(14), v(15), v(16)
    wpl_y = v(19)
    wy = (2.0 * iy / h) if h > 0.0 and iy > 0.0 else 0.0
    complete = (h > 0.0 and b > 0.0 and tw > 0.0 and tf > 0.0
                and iy > 0.0 and iz > 0.0)
    return {
        "shape_type": int(shape_type),
        "shape_kind": shape_kind(shape_type),
        "area_m2": area,
        "iy_m4": iy,
        "iz_m4": iz,
        "h_m": h,
        "b_m": b,
        "tw_m": tw,
        "tf_m": tf,
        "r_m": r,
        "wy_m3": wy,
        "wpl_y_m3": wpl_y,
        "complete": complete,
    }


def has_full_dims(props: Dict[str, Any]) -> bool:
    """True when all dimensions needed for classification/LTB are live."""
    return bool(props.get("complete")) and props.get("shape_kind") in (
        "i", "channel", "angle", "circular_hollow", "rect_hollow")


def flange_outstand(b_m: float, tw_m: float, r_m: float) -> float:
    """Flange outstand c = (b - tw)/2 - r (Table 5.2, rolled I-sections)."""
    return max((float(b_m) - float(tw_m)) / 2.0 - float(r_m), 0.0)


def web_clear_height(h_m: float, tf_m: float, r_m: float) -> float:
    """Clear web height c = h - 2*tf - 2*r (Table 5.2, rolled I-sections)."""
    return max(float(h_m) - 2.0 * float(tf_m) - 2.0 * float(r_m), 0.0)


def it_from_dims(h_m: float, b_m: float, tw_m: float, tf_m: float,
                 r_m: float) -> float:
    """St. Venant torsion constant (m4) for a rolled I-section.

    Thin-wall three-rectangle model + fillet-corner correction:
      It = (2*b*tf^3 + (h - 2*tf)*tw^3)/3 + 2*0.105*(r + tw/2)^4
    The corner coefficient 0.105 reproduces published rolled values within
    ~10% (this formula UNDER-estimates, which lowers Mcr — conservative).
    """
    web = max(float(h_m) - 2.0 * float(tf_m), 0.0)
    d = float(r_m) + float(tw_m) / 2.0
    corner = 2.0 * 0.105 * d ** 4
    return (2.0 * float(b_m) * float(tf_m) ** 3
            + web * float(tw_m) ** 3) / 3.0 + corner


def iw_from_dims(iz_m4: float, h_m: float, tf_m: float) -> float:
    """Warping constant (m6) for a doubly-symmetric I-section:
    Iw = Iz * (h - tf)^2 / 4  (flange-centroid model — standard)."""
    return float(iz_m4) * (float(h_m) - float(tf_m)) ** 2 / 4.0


def read_section_props(bridge,
                       section_name: str) -> Optional[Dict[str, Any]]:
    """COM-facing wrapper: loads a section label's Data and extracts the
    probe-verified props (None if the section cannot be loaded)."""
    try:
        # Lazy import: section_data is imported BY robot_tool, so importing
        # robot_tool at module level here would be circular.
        from tools.robot_tool import RobotEnum
        data = bridge.structure.Labels.Get(
            RobotEnum.I_LT_BAR_SECTION, str(section_name)).Data
        return extract_section_props(data.GetValue, data.ShapeType)
    except Exception:
        return None
