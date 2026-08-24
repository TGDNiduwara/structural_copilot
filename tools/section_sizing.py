"""
tools/section_sizing.py
=======================
Span-aware steel section sizing (Part B of the geometry/section-scaling
refactor — the "1m bridge got IPE 200" bug).

suggest_section() is a FIRST-PASS sizing heuristic only — it exists to stop
every template default from being a fixed string that ignores the actual
scale of the structure. It is NOT a final design: use it to pick a starting
section, then verify against real demand (solve + utilization / proportions
check).

Why static catalog tables instead of walking the live Robot catalog?
---------------------------------------------------------------------
The spec functions that call suggest_section() are PURE (no COM) so the
same template code runs in offline tests, in batch/design_space.py, and
before any Robot connection exists. The tables below are the standard Euro
profile depth series. The safety net that guarantees a suggested name
exists in Robot is the existing _get_or_create_section_label() / catalog
activation pipeline, which raises a clear error if a generated name is
unknown to the live catalogs.

Author: Principal Structural Software Architecture Team
"""

from __future__ import annotations

import logging
import math
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger("structural_copilot.section_sizing")

#: Nominal section depths (mm) for the Euro profile families used by the
#: templates. Rounded standard series from the EN 10365 / ArcelorMittal
#: tables (also the series Robot's EURO catalog exposes).
FAMILY_SIZES: Dict[str, List[int]] = {
    "IPE": [80, 100, 120, 140, 160, 180, 200, 220, 240, 270, 300, 330,
            360, 400, 450, 500, 550, 600],
    "HEA": [100, 120, 140, 160, 180, 200, 220, 240, 260, 280, 300, 320,
            340, 360, 400, 450, 500, 550, 600, 650, 700, 800, 900, 1000],
    "HEB": [100, 120, 140, 160, 180, 200, 220, 240, 260, 280, 300, 320,
            340, 360, 400, 450, 500, 550, 600, 650, 700, 800, 900, 1000],
    "HEM": [100, 120, 140, 160, 180, 200, 220, 240, 260, 280, 300, 320,
            340, 360, 400, 450, 500, 550, 600, 650, 700, 800, 900, 1000],
    "IPN": [80, 100, 120, 140, 160, 180, 200, 220, 240, 260, 280, 300,
            320, 340, 360, 380, 400],
    "UPN": [80, 100, 120, 140, 160, 180, 200, 220, 240, 260, 280, 300,
            320, 340, 360, 380, 400],
    "UPE": [80, 100, 120, 140, 160, 180, 200, 220, 240, 270, 300, 330,
            360, 400],
    "L": [20, 25, 30, 35, 40, 45, 50, 60, 70, 80, 90, 100, 120, 150,
          200, 250],
    "CHS": [42, 48, 60, 76, 89, 114, 140, 168, 219],
    "RHS": [100, 120, 150, 200, 250],
    "SHS": [80, 100, 120, 150, 200],
}

#: First-pass span-to-depth ratios per element type (engineering judgement;
#: deliberately coarse — this is a starting-point heuristic, not a design).
_DEPTH_TO_SPAN = {
    "beam": 18.0,        # span/15..span/20 band -> span/18 default
    "truss_chord": 18.0, # chord member depth band, same as beams
}

#: Catalog-style section names available for the LLM tools, built from the
#: SAME nominal series suggest_section()/the geometry templates use
#: (FAMILY_SIZES). Names are "family + space + size" (e.g. "IPE 300") — the
#: form Robot's EURO catalog and _get_or_create_section_label accept.
def section_families() -> List[str]:
    """Sorted family codes available from the nominal catalog table."""
    return sorted(FAMILY_SIZES)


#: Equal-leg angle names VERIFIED against Robot's live EURO catalog
#: (probe 2026-08-23). Bare leg names like 'L 100' and thin-webs like
#: 'L 100x100x5' do NOT resolve; these forms DO. available_sections('L')
#: returns exactly this list so the LLM only ever sees resolvable names.
L_SECTION_NAMES: List[str] = [
    "L 40x40x5", "L 45x45x5", "L 50x50x5", "L 60x60x6", "L 65x65x6",
    "L 80x80x8", "L 90x90x8", "L 100x100x10", "L 120x120x10",
    "L 150x150x10", "L 150x150x15",
]

#: Leg-size -> verified resolvable thickness for suggest_section web/brace.
_L_WEB_THICKNESS: Dict[int, int] = {
    40: 5, 45: 5, 50: 5, 60: 6, 65: 6, 70: 8, 80: 8, 90: 8,
    100: 10, 120: 10, 150: 10,
}

#: Hollow-section names VERIFIED against Robot's live UKST catalog
#: (probe 2026-08-23): CHS d x t / RHS b x h x t / SHS b x b x t.
#: EURO/AISC/DIN/ARCLR/CISC/CHINA/JAPAN returned NONE for these forms;
#: UKST is the catalog that carries them. Only these exact names are
#: advertised so the LLM never sees a non-resolvable guess.
CHS_SECTION_NAMES: List[str] = [
    "CHS 42.4x3.2", "CHS 48.3x3.2", "CHS 60.3x3.2",
    "CHS 76.1x3.2", "CHS 88.9x3.2", "CHS 88.9x4",
    "CHS 114.3x4", "CHS 114.3x5", "CHS 139.7x5",
    "CHS 139.7x8", "CHS 168.3x6", "CHS 219.1x8",
]
RHS_SECTION_NAMES: List[str] = [
    "RHS 100x50x4", "RHS 120x80x5", "RHS 150x100x6",
    "RHS 200x100x6", "RHS 250x150x8",
]
SHS_SECTION_NAMES: List[str] = [
    "SHS 80x80x4", "SHS 100x100x5", "SHS 120x120x6",
    "SHS 150x150x8", "SHS 200x200x10",
]
#: Family code -> verified-name table (L / CHS / RHS / SHS).
_VERIFIED_SECTION_NAMES: Dict[str, List[str]] = {
    "L": L_SECTION_NAMES,
    "CHS": CHS_SECTION_NAMES,
    "RHS": RHS_SECTION_NAMES,
    "SHS": SHS_SECTION_NAMES,
}


def available_sections(family: Optional[str] = None) -> List[str]:
    """Valid catalog-style section names ("IPE 300", "HEA 200", ...) from
    the same nominal series suggest_section() draws on, optionally filtered
    to one family (case-insensitive: "IPE" / "ipe" / "L").

    The names are guaranteed to resolve against Robot's live catalogs at
    build time by the existing _get_or_create_section_label() safety net.

    [FIX 2026-08-23] family='L' returns VERIFIED equal-leg angle names
    ('L 50x50x5', 'L 120x120x10'), NOT bare leg sizes ('L 120') — the bare
    forms were the root cause of tonight's angle catalog-miss failures.
    """
    if family:
        key = str(family).strip().upper()
        if key not in FAMILY_SIZES:
            raise ValueError(
                f"Unknown section family '{family}'. Known families: "
                f"{sorted(FAMILY_SIZES)}")
        if key in _VERIFIED_SECTION_NAMES:
            return list(_VERIFIED_SECTION_NAMES[key])
        return [f"{key} {size}" for size in FAMILY_SIZES[key]]
    out: List[str] = []
    for fam in sorted(FAMILY_SIZES):
        if fam in _VERIFIED_SECTION_NAMES:
            out.extend(_VERIFIED_SECTION_NAMES[fam])
        else:
            out.extend(f"{fam} {size}" for size in FAMILY_SIZES[fam])
    return out
#: Column sizing: depth = height / (k * lambda_target) with radius of
#: gyration ~= 0.25 * depth for H-family and target slenderness 100.
_COLUMN_RG_FACTOR = 0.25
_COLUMN_LAMBDA_TARGET = 100.0
#: Light web/brace members: depth or leg ~ span / _WEB_DENOMINATOR.
_WEB_DENOMINATOR = 120.0   # angle-family (L) legs, mm rule below
_BRACE_DENOMINATOR = 45.0  # I-family brace depth rule
_WEB_LEG_MIN_MM = 40.0
_WEB_LEG_MAX_MM = 120.0


def _nearest(sizes: List[int], target_mm: float, catalog: str,
             notes: Optional[List[str]] = None) -> str:
    """Nearest catalog size to ``target_mm``, clamped to family bounds.
    Appends a human-readable note when clamping actually occurred."""
    lo, hi = sizes[0], sizes[-1]
    if target_mm < lo:
        if notes is not None:
            notes.append(
                f"auto-section: target {catalog} depth {target_mm:.0f} mm "
                f"is below the catalog minimum {lo} mm; clamped to "
                f"{catalog} {lo}.")
        logger.warning(
            "suggest_section: target %s depth %.0f mm below min %d; "
            "clamped to %s %d.", catalog, target_mm, lo, catalog, lo)
        target_mm = lo
    elif target_mm > hi:
        if notes is not None:
            notes.append(
                f"auto-section: target {catalog} depth {target_mm:.0f} mm "
                f"EXCEEDS the catalog maximum {hi} mm; clamped to "
                f"{catalog} {hi}. VERIFY against actual demand.")
        logger.warning(
            "suggest_section: target %s depth %.0f mm above max %d; "
            "clamped to %s %d — VERIFY against actual demand.",
            catalog, target_mm, hi, catalog, hi)
        target_mm = hi
    best = min(sizes, key=lambda s: abs(s - target_mm))
    return f"{catalog} {best}"

def suggest_section(
    element_type: str,
    span_m: float,
    catalog: str = "IPE",
    depth_to_span: float = None,
    notes: Optional[List[str]] = None,
) -> str:
    """Suggests a starting catalog section for a member spanning ``span_m``.

    FIRST-PASS ESTIMATE ONLY — the returned section is a scale-appropriate
    starting point (chosen from the requested catalog family by a
    span-to-depth / slenderness heuristic), NOT a final design. Always
    verify with a solve + utilization / proportions check before relying on
    it.

    Heuristics (deliberately coarse):
      * beam / truss_chord : depth = span / depth_to_span (default span/18,
        the middle of the standard span/15..span/20 band).
      * column             : depth sized for slenderness lambda ~ 100 with
        radius of gyration ~ 0.25 * depth (H-family columns), i.e.
        depth ~= height / 25 for the given storey height ``span_m``.
      * web / brace        : light members — L-family leg ~ span/120
        (clamped to a sensible 40..120 mm), I-family depth ~ span/45.

    The resulting name is matched to the NEAREST catalog size in
    ``FAMILY_SIZES`` for the requested family and clamped to the family
    bounds; clamps are appended to ``notes`` (and logged) so a span too
    small or too large for the catalog is never silent.

    Parameters
    ----------
    element_type : "beam" | "truss_chord" | "column" | "web" | "brace"
    span_m : governing span / storey height / brace length in meters
    catalog : catalog family (IPE, HEA, HEB, HEM, IPN, UPN, UPE, L)
    depth_to_span : optional explicit span-to-depth ratio for beam/chord
    notes : optional list; clamp warnings are appended here
    """
    element_type = str(element_type or "beam").lower()
    catalog = str(catalog or "IPE").strip().upper()
    span_m = max(float(span_m), 0.001)

    sizes = FAMILY_SIZES.get(catalog)
    if sizes is None:
        raise ValueError(
            f"Unknown section catalog family '{catalog}'. Known families: "
            f"{sorted(FAMILY_SIZES)}")

    if element_type == "column":
        # Slenderness-based: depth = H / (r_factor * lambda_target).
        depth_m = span_m / (_COLUMN_RG_FACTOR * _COLUMN_LAMBDA_TARGET)
    elif element_type == "web":
        if catalog == "L":
            leg_mm = span_m * 1000.0 / _WEB_DENOMINATOR
            leg_mm = min(max(leg_mm, _WEB_LEG_MIN_MM), _WEB_LEG_MAX_MM)
            leg = min(sizes, key=lambda s: abs(s - leg_mm))
            # [FIX 2026-08-23] Use a RESOLVABLE thickness per leg (probed
            # live): the old fixed "x5" produced 'L 100x100x5' / 'L 120x120x5'
            # which do NOT exist in Robot's EURO catalog.
            t = _L_WEB_THICKNESS.get(leg, 5)
            result = f"L {leg}x{leg}x{t}"
            if notes is not None:
                notes.append(
                    f"auto-section: {element_type} spanning {span_m:.3g} m -> "
                    f"{result} (first-pass heuristic; verify against demand).")
            return result
        depth_m = span_m / _BRACE_DENOMINATOR
    elif element_type == "brace":
        if catalog == "L":
            leg_mm = span_m * 1000.0 / _BRACE_DENOMINATOR
            leg_mm = min(max(leg_mm, _WEB_LEG_MIN_MM), _WEB_LEG_MAX_MM)
            leg = min(sizes, key=lambda s: abs(s - leg_mm))
            # [FIX 2026-08-23] Same resolvable-thickness rule as web/L.
            t = _L_WEB_THICKNESS.get(leg, 5)
            result = f"L {leg}x{leg}x{t}"
            if notes is not None:
                notes.append(
                    f"auto-section: {element_type} spanning {span_m:.3g} m -> "
                    f"{result} (first-pass heuristic; verify against demand).")
            return result
        depth_m = span_m / _BRACE_DENOMINATOR
    else:
        ratio = float(depth_to_span) if depth_to_span else \
            _DEPTH_TO_SPAN.get(element_type, 18.0)
        depth_m = span_m / ratio

    result = _nearest(sizes, depth_m * 1000.0, catalog, notes)
    if notes is not None:
        notes.append(
            f"auto-section: {element_type} spanning {span_m:.3g} m -> "
            f"{result} (first-pass heuristic; verify against demand).")
    return result

def section_depth_mm(section_name: str) -> Optional[float]:
    """Best-effort nominal depth (mm) parsed from a catalog section name.

    Handles the names used across this codebase:
      IPE 200 / IPE300 / HEA 200 / HEB 300   -> 200 / 300 / 200 / 300
      L 50x50x5                              -> 50 (leg)
      W 12X26                                -> 12 inches * 25.4
      UB 305x165x40                          -> 305
    Returns None for names with no parseable leading size.
    """
    name = str(section_name or "").strip().upper()
    if not name:
        return None
    m = re.match(r"^([A-Z]+)\s*([0-9]+\.[0-9]*|[0-9]+)", name)
    if not m:
        return None
    family, num = m.group(1), float(m.group(2))
    if family == "W":
        return num * 25.4
    return float(num)


def check_section_proportions(
    spec: Dict[str, Any],
    min_ratio: float = 10.0,
    max_ratio: float = 25.0,
    max_column_ratio: float = 40.0,
) -> List[Dict[str, Any]]:
    """Flags bars whose span-to-depth ratio is far outside structural norms.

    Pure function over a spec dict (no COM). For each bar it computes the
    member length from its node coordinates and the section depth from its
    section name, then compares span/depth against a typical band:

      * horizontal members (beams / truss chords):  10 <= ratio <= 25
      * vertical members (columns):                 8  <= ratio <= 40
        (columns are intentionally more slender; a 6 m column on HEA 200,
         ratio 30, is a common real-world proportion and must not alarm)

    Returns a list of warning dicts (empty when nothing is egregious):
      [{"bar_id", "section", "length_m", "depth_mm", "span_to_depth",
        "issue": "deep" | "shallow"}]
    """
    nodes = {}
    for n in spec.get("nodes") or []:
        try:
            nodes[int(n["id"])] = (float(n.get("x", 0.0)),
                                   float(n.get("y", 0.0)),
                                   float(n.get("z", 0.0)))
        except (TypeError, ValueError, KeyError):
            continue

    warnings: List[Dict[str, Any]] = []
    for b in spec.get("bars") or []:
        try:
            bar_id = int(b["id"])
            n1, n2 = int(b["n1"]), int(b["n2"])
        except (TypeError, ValueError, KeyError):
            continue
        p1, p2 = nodes.get(n1), nodes.get(n2)
        if p1 is None or p2 is None:
            continue
        length = math.sqrt((p2[0] - p1[0]) ** 2 +
                           (p2[1] - p1[1]) ** 2 +
                           (p2[2] - p1[2]) ** 2)
        depth = section_depth_mm(b.get("section") or "")
        if not depth or length <= 0.0:
            continue
        ratio = length / (depth / 1000.0)

        is_column = (abs(p1[0] - p2[0]) < 1e-6 and abs(p1[1] - p2[1]) < 1e-6)
        issue = None
        if is_column:
            if ratio < 8.0:
                issue = "deep"
            elif ratio > max_column_ratio:
                issue = "shallow"
        else:
            if ratio < min_ratio:
                issue = "deep"
            elif ratio > max_ratio:
                issue = "shallow"
        if issue:
            warnings.append({
                "bar_id": bar_id,
                "section": str(b.get("section")),
                "length_m": round(length, 3),
                "depth_mm": round(depth, 1),
                "span_to_depth": round(ratio, 1),
                "issue": issue,
                "note": ("section depth is large relative to the member "
                         "span" if issue == "deep" else
                         "section depth is small relative to the member span"
                         " — likely under-sized"),
            })
    return warnings


