"""
tools/ltb_check.py
==================
[EUROCODE Phase C — eurocode_scope.md §5, decisions D4/D6/D7/D8]
Lateral-torsional buckling + beam-column interaction per EN 1993-1-1.

SCOPE (eurocode_scope.md §5, verbatim):
  * §6.3.2.2 (general method) ONLY — §6.3.2.3 (rolled I-sections, less
    conservative) is NOT implemented.
  * Doubly-symmetric rolled I-sections only, detected via the VERIFIED
    ShapeType map (IPE=20, IPN=25, HEA=10, HEB=12, HEM=14). Everything
    else -> NOT_CHECKABLE (D7 — never a guessed value).
  * Properties: Iz, h, b, tf, tw, r, Wy read LIVE from Robot
    (probe-verified GetValue map). It/Iw are NOT exposed by Robot so they
    are COMPUTED from the live geometry (section_data.it_from_dims /
    iw_from_dims) — the closed forms reproduce published values within
    ~10% (It, conservative) and <1% (Iw).
  * Mcr: closed form for doubly-symmetric I-sections with C1 taken from
    the ACTUAL EXPORTED moment diagram shape (ENV 1993-1-1 Annex F
    tables — withdrawn-annex material used as standard practice; stated,
    never hidden). Load assumed at the SHEAR CENTER (zg = 0) in v1.
  * lambda_LT = sqrt(Wy·fy/Mcr); chi_LT from the LTB buckling curve
    (Tables 6.3/6.4 + 6.5), general method with lambda_LT,0 = 0.4 and
    beta = 0.75; Mb,Rd = chi_LT·Wy·fy/gamma_M1.
  * Beam-column interaction: §6.3.3 eqs. (6.61)/(6.62) with ANNEX B
    interaction factors kyy/kzz/kzy/kyz (Cmy=Cmz=CmLt=1.0 conservative),
    using flexural buckling chi_y/chi_z from §6.3.1.2 (Table 6.2 curves).
  * Unbraced length Lcr_LT comes from the bracing side-table
    (tools.bracing_registry) with the DEFAULT-AND-WARN contract: a
    defaulted full-length value is a CONSERVATIVE assumption, never a
    verified bracing condition, and every result carries the source.

The pure core (everything below the COM wrapper) takes plain numbers and
runs in offline tests; the COM wrapper gathers section props / material /
forces / bracing from the bridge exactly like batch/buckling_check.py.

Author: Principal Structural Software Architecture Team
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

from tools.section_data import (read_section_props, has_full_dims,
                                it_from_dims, iw_from_dims)
from tools.section_classification import classify_section
from tools.eurocode_params import (GAMMA_M1, IMPERFECTION_ALPHA,
                                   rolled_i_buckling_curve,
                                   alpha_lt_for_rolled_i,
                                   LAMBDA_LT_0, BETA_LT,
                                   effective_yield_strength)
from tools.robot_tool import RobotEnum

#: Constant caveat attached to every result (same discipline as
#: EULER_NOTE / the elastic-utilization note — impossible to miss).
LTB_NOTE = (
    "EN 1993-1-1 §6.3.2.2 (general method) only; §6.3.2.3 not implemented. "
    "Doubly-symmetric rolled I-sections only. It/Iw computed from live "
    "geometry; C1 from the exported moment shape (ENV Annex F); load at "
    "the shear center. Class 4 / non-I / unavailable-dimension sections "
    "-> NOT_CHECKABLE."
)

_EPS = 1e-9


def c1_from_moment_shape(stations: Sequence[Tuple[float, float]]) \
        -> Tuple[float, str]:
    """C1 factor from the ACTUAL moment diagram shape (ENV 1993-1-1 Annex F).

    ``stations``: [(pos_frac in [0,1], moment in N·m)] exported along the
    bar. Classification:
      * uniform moment                       -> C1 = 1.00
      * end-moment dominant                  -> C1 = 1.77 - 1.04ψ + 0.27ψ²
        with ψ = M_small/M_large (signs retained)
      * interior peak, quarter-point |M| > 0.625·max  -> C1 = 1.13 (UDL)
      * interior peak, quarter-point |M| <= 0.625·max -> C1 = 1.35
        (concentrated load)
      * anything unrecognised / no moment    -> C1 = 1.00 (conservative)
    """
    pts = sorted((float(p), float(m))
                 for p, m in stations if 0.0 <= float(p) <= 1.0)
    if not pts:
        return 1.0, "no stations (C1=1.0 conservative)"
    abs_m = [abs(m) for _, m in pts]
    m_max = max(abs_m)
    if m_max <= _EPS:
        return 1.0, "no bending moment"
    if min(abs_m) >= 0.99 * m_max:
        return 1.0, "uniform moment"
    if abs(pts[0][1]) >= 0.99 * m_max or abs(pts[-1][1]) >= 0.99 * m_max:
        m_small, m_large = min(pts[0][1], pts[-1][1]), max(pts[0][1], pts[-1][1])
        psi = (m_small / m_large) if abs(m_large) > _EPS else 0.0
        c1 = 1.77 - 1.04 * psi + 0.27 * psi * psi
        return c1, f"end moments (psi={psi:.3f})"

    def m_at(p: float) -> float:
        for (p1, m1), (p2, m2) in zip(pts, pts[1:]):
            if p1 <= p <= p2:
                t = (p - p1) / (p2 - p1) if p2 > p1 else 0.0
                return m1 + t * (m2 - m1)
        return pts[-1][1]

    q = max(abs(m_at(0.25)), abs(m_at(0.75))) / m_max
    if q >= 0.625:
        return 1.13, "distributed load (UDL)"
    return 1.35, "concentrated load"


def mcr_closed_form(e_pa: float, g_pa: float, iz_m4: float, it_m4: float,
                    iw_m6: float, lcr_lt_m: float, c1: float = 1.0) -> float:
    """Elastic critical moment (N·m) for a doubly-symmetric I-section,
    load at the shear center, kz = kw = 1 (ENV 1993-1-1 Annex F closed
    form):
        Mcr = C1 · π²·E·Iz/L² · sqrt(Iw/Iz + L²·G·It/(π²·E·Iz))
    """
    l = float(lcr_lt_m)
    if l <= 0.0:
        raise ValueError("lcr_lt must be > 0 for Mcr")
    ei = math.pi ** 2 * float(e_pa) * float(iz_m4)
    inner = float(iw_m6) / float(iz_m4) + (l * l * float(g_pa) * float(it_m4)) / ei
    return float(c1) * ei / (l * l) * math.sqrt(max(inner, 0.0))


def lt_slenderness(wy_m3: float, fy_pa: float, mcr_nm: float) -> float:
    """lambda_LT = sqrt(Wy·fy / Mcr)."""
    return math.sqrt(max(float(wy_m3) * float(fy_pa) / float(mcr_nm), 0.0))


def chi_lt_reduction(alpha_lt: float, lambda_lt: float,
                     lambda_lt0: float = LAMBDA_LT_0,
                     beta: float = BETA_LT) -> float:
    """chi_LT per §6.3.2.2 general method (lambda_LT,0 plateau, beta=0.75):
        Phi_LT = 0.5·[1 + alpha_LT·(lambda_LT - lambda_LT,0) + beta·lambda_LT²]
        chi_LT = 1 / (Phi_LT + sqrt(Phi_LT² - beta·lambda_LT²))
        chi_LT <= 1.0 and <= 1/lambda_LT²
    Returns 1.0 when lambda_LT <= lambda_LT,0 (buckling may be ignored)."""
    if lambda_lt <= lambda_lt0:
        return 1.0
    lam2 = float(lambda_lt) ** 2
    phi = 0.5 * (1.0 + float(alpha_lt) * (float(lambda_lt) - lambda_lt0)
                 + beta * lam2)
    chi = 1.0 / (phi + math.sqrt(max(phi * phi - beta * lam2, 0.0)))
    return max(0.0, min(chi, 1.0, 1.0 / lam2))


def mb_rd(chi_lt: float, wy_m3: float, fy_pa: float,
          gamma_m1: float = GAMMA_M1) -> float:
    """Design lateral-torsional buckling resistance Mb,Rd (N·m)."""
    return float(chi_lt) * float(wy_m3) * float(fy_pa) / float(gamma_m1)


def flexural_chi(alpha: float, lambda_bar: float) -> float:
    """chi for flexural buckling §6.3.1.2:
        Phi = 0.5·[1 + alpha·(lambda_bar - 0.2) + lambda_bar²]
        chi = 1 / (Phi + sqrt(Phi² - lambda_bar²)), chi <= 1."""
    lam2 = float(lambda_bar) ** 2
    phi = 0.5 * (1.0 + float(alpha) * (float(lambda_bar) - 0.2) + lam2)
    chi = 1.0 / (phi + math.sqrt(max(phi * phi - lam2, 0.0)))
    return max(0.0, min(chi, 1.0))


def interaction_annex_b(
    ne_d: float,
    ncr_y: float, ncr_z: float,
    chi_y: float, chi_z: float,
    nrk: float,           # A·fy/gamma_M1
    my_ed: float, mb_rd_val: float,   # My,Ed and chi_LT·My,Rk/gamma_M1
    mz_ed: float, mz_rk: float,       # Mz,Ed and Wz·fy/gamma_M1
    lambda_y: float, lambda_z: float,
    cmy: float = 1.0, cmz: float = 1.0, cmlt: float = 1.0,
) -> Dict[str, Any]:
    """§6.3.3 eqs. (6.61)/(6.62) with ANNEX B interaction factors.

    All forces in N / N·m. Cm factors default to 1.0 (conservative).
    Returns {"eq61", "eq62", "kyy", "kzz", "kzy", "kyz", "ny", "nz"}.
    """
    n = float(ne_d)
    n_y = n / (float(chi_y) * float(nrk)) if float(chi_y) * float(nrk) > _EPS else float("inf")
    n_z = n / (float(chi_z) * float(nrk)) if float(chi_z) * float(nrk) > _EPS else float("inf")
    ny_lim = float(n_y) if math.isfinite(n_y) else 0.0
    nz_lim = float(n_z) if math.isfinite(n_z) else 0.0

    def mu(ncr: float, chi: float) -> float:
        if ncr > _EPS and chi > _EPS:
            denom = 1.0 - chi * n / ncr
            return (1.0 - n / ncr) / denom if abs(denom) > _EPS else 1.0
        return 1.0

    muy, muz = mu(ncr_y, chi_y), mu(ncr_z, chi_z)

    kyy = cmy * cmlt * (1.0 + (lambda_y - 0.2) * ny_lim)
    kyy = min(kyy, cmy * cmlt * (1.0 + 0.8 * ny_lim))
    kzz = cmz * cmlt * (1.0 + (2.0 * lambda_z - 0.6) * nz_lim)
    kzz = min(kzz, cmz * cmlt * (1.0 + 1.4 * nz_lim))
    # Annex B, Class 1/2 sections, lambda_z >= 0.4:
    kzy = cmy * cmlt * (1.0 + (2.0 * lambda_z - 0.6) * nz_lim)
    kzy = min(kzy, cmy * cmlt * (1.0 + 1.4 * nz_lim))
    kyz = 0.6 * kzz

    m_y = float(my_ed) / float(mb_rd_val) if float(mb_rd_val) > _EPS else 0.0
    m_z = float(mz_ed) / float(mz_rk) if float(mz_rk) > _EPS else 0.0
    eq61 = ny_lim + kyy * m_y + kyz * m_z
    eq62 = nz_lim + kzy * m_y + kzz * m_z
    return {"eq61": eq61, "eq62": eq62, "kyy": kyy, "kzz": kzz,
            "kzy": kzy, "kyz": kyz, "ny": ny_lim, "nz": nz_lim}

def check_ltb_member(
    props: Dict[str, Any],
    fy_pa: float,
    length_m: float,
    stations: Sequence[Tuple[float, float]],
    axial_n: float = 0.0,
    lcr_lt: Optional[float] = None,
    lcr_lt_source: str = "defaulted",
    lcr_y: Optional[float] = None,
    lcr_z: Optional[float] = None,
    lcr_y_source: str = "defaulted",
    lcr_z_source: str = "defaulted",
    e_pa: float = 210e9,
    g_pa: float = 81e9,
    gamma_m1: float = GAMMA_M1,
    warnings: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Full LTB + beam-column interaction check for ONE member (PURE).

    Parameters
    ----------
    props : section-props dict (tools.section_data) with live dims
    fy_pa : design yield strength (Pa) at the flange thickness
    length_m : physical bar length (m)
    stations : [(pos_frac, moment N·m)] along the bar (MY, major axis)
    axial_n : axial force (N); negative = compression
    lcr_lt / lcr_y / lcr_z : unbraced lengths (m), resolved by the bracing
        side-table BEFORE calling (defaults to ``length_m`` when None)
    *_source : bracing source tag ("explicit"|"brace_points"|"defaulted")
    warnings : bracing/other warnings to carry into the result

    Returns a result dict with PASS / FAIL / NOT_CHECKABLE + the full
    numerical trace (Mcr, lambda_LT, chi_LT, Mb,Rd, interaction ratios).
    """
    warnings = list(warnings or [])
    length = float(length_m)
    base = {"length_m": round(length, 4), "note": LTB_NOTE,
            "warnings": warnings}

    if props.get("shape_kind") != "i" or not has_full_dims(props):
        return {**base, "status": "NOT_CHECKABLE",
                "reason": "LTB v1 scope: doubly-symmetric rolled I-sections "
                          "with live dimensions only (D7)."}
    if fy_pa is None or fy_pa <= 0.0:
        return {**base, "status": "NOT_CHECKABLE",
                "reason": "material has no usable fy (RE=0 / no grade)."}

    cls = classify_section(props, fy_pa / 1e6, "bending")
    if cls.get("class") is None or cls["class"] >= 4:
        return {**base, "status": "NOT_CHECKABLE", "classification": cls,
                "reason": cls.get("reason") or "Class 4 — NOT_CHECKABLE (D7)."}

    iz = props["iz_m4"]
    iy = props["iy_m4"]
    h, b, tw, tf, r = (props["h_m"], props["b_m"], props["tw_m"],
                       props["tf_m"], props["r_m"])
    area = props["area_m2"]
    wy = props["wy_m3"]
    it = it_from_dims(h, b, tw, tf, r)
    iw = iw_from_dims(iz, h, tf)

    c1, c1_case = c1_from_moment_shape(stations)
    moments = [abs(m) for _, m in stations]
    m_ed = max(moments) if moments else 0.0
    if m_ed <= _EPS:
        return {**base, "status": "PASS", "applies": False,
                "reason": "no significant bending moment (LTB not applicable)",
                "m_ed_knm": 0.0}

    l_lt = float(lcr_lt) if lcr_lt is not None else length
    mcr = mcr_closed_form(e_pa, g_pa, iz, it, iw, l_lt, c1)
    lam_lt = lt_slenderness(wy, fy_pa, mcr)
    alpha_lt = alpha_lt_for_rolled_i(h, b)
    chi_lt = chi_lt_reduction(alpha_lt, lam_lt)
    mb_rd_val = mb_rd(chi_lt, wy, fy_pa, gamma_m1)
    util_ltb = m_ed / mb_rd_val if mb_rd_val > _EPS else float("inf")

    result = {**base, "applies": True, "status": "PASS",
              "section": props.get("name", ""), "shape_type": props["shape_type"],
              "classification": cls,
              "c1": round(c1, 4), "c1_case": c1_case,
              "lcr_lt_m": round(l_lt, 4), "lcr_lt_source": lcr_lt_source,
              "mcr_knm": round(mcr / 1e3, 4),
              "lambda_lt": round(lam_lt, 4),
              "alpha_lt": alpha_lt, "chi_lt": round(chi_lt, 4),
              "mb_rd_knm": round(mb_rd_val / 1e3, 4),
              "m_ed_knm": round(m_ed / 1e3, 4),
              "util_ltb": round(util_ltb, 4),
              "it_m4": it, "iw_m6": iw,
              "fy_MPa": round(fy_pa / 1e6, 1)}

    # Beam-column interaction (§6.3.3 Annex B) — compression only.
    if axial_n < -_EPS:
        return _interaction(result, props, fy_pa, length, axial_n, m_ed,
                            mb_rd_val, util_ltb, gamma_m1, e_pa,
                            lcr_y, lcr_y_source, lcr_z, lcr_z_source)
    result["utilization"] = round(util_ltb, 4)
    result["governing"] = "ltb"
    if util_ltb > 1.0:
        result["status"] = "FAIL"
        result["reason"] = f"governing utilization {util_ltb:.2f} > 1.0 (ltb)."
    return result

def _interaction(result: Dict[str, Any], props: Dict[str, Any], fy_pa: float,
                 length: float, axial_n: float, m_ed: float,
                 mb_rd_val: float, util_ltb: float, gamma_m1: float,
                 e_pa: float, lcr_y: Optional[float], lcr_y_source: str,
                 lcr_z: Optional[float], lcr_z_source: str) -> Dict[str, Any]:
    """§6.3.3 eqs. (6.61)/(6.62) branch for a compression member."""
    iy, iz = props["iy_m4"], props["iz_m4"]
    h, b, tf = props["h_m"], props["b_m"], props["tf_m"]
    area = props["area_m2"]
    wy = props["wy_m3"]
    ly = float(lcr_y) if lcr_y is not None else length
    lz = float(lcr_z) if lcr_z is not None else length
    ncr_y = math.pi ** 2 * e_pa * iy / (ly * ly)
    ncr_z = math.pi ** 2 * e_pa * iz / (lz * lz)
    lam_y = math.sqrt(max(area * fy_pa / ncr_y, 0.0)) if ncr_y > _EPS else float("inf")
    lam_z = math.sqrt(max(area * fy_pa / ncr_z, 0.0)) if ncr_z > _EPS else float("inf")
    alpha_y = IMPERFECTION_ALPHA[rolled_i_buckling_curve(h, b, tf, "y")]
    alpha_z = IMPERFECTION_ALPHA[rolled_i_buckling_curve(h, b, tf, "z")]
    chi_y = flexural_chi(alpha_y, lam_y)
    chi_z = flexural_chi(alpha_z, lam_z)
    nrk = area * fy_pa / gamma_m1
    my_rk = wy * fy_pa / gamma_m1
    wz = (2.0 * iz / b) if b > 0.0 else 0.0
    mz_rk = wz * fy_pa / gamma_m1
    inter = interaction_annex_b(abs(axial_n), ncr_y, ncr_z, chi_y, chi_z,
                                nrk, m_ed, mb_rd_val, 0.0, mz_rk,
                                lam_y, lam_z)
    result.update({
        "lcr_y_m": round(ly, 4), "lcr_y_source": lcr_y_source,
        "lcr_z_m": round(lz, 4), "lcr_z_source": lcr_z_source,
        "axial_n_kN": round(axial_n / 1e3, 3),
        "chi_y": round(chi_y, 4), "chi_z": round(chi_z, 4),
        "lambda_y": round(lam_y, 4), "lambda_z": round(lam_z, 4),
        "ncr_y_kN": round(ncr_y / 1e3, 2), "ncr_z_kN": round(ncr_z / 1e3, 2),
        "eq61": round(inter["eq61"], 4), "eq62": round(inter["eq62"], 4),
        "kyy": round(inter["kyy"], 4), "kzz": round(inter["kzz"], 4),
    })
    util = max(util_ltb, inter["eq61"], inter["eq62"])
    result["utilization"] = round(util, 4)
    result["governing"] = ("eq62" if inter["eq62"] >= inter["eq61"]
                           else "eq61") if util > util_ltb + 1e-9 else "ltb"
    if util > 1.0:
        result["status"] = "FAIL"
        result["reason"] = (f"governing utilization {util:.2f} > 1.0 "
                            f"({result['governing']}).")
    return result

# ----------------------------------------------------------------------
# COM-facing wrapper (mirrors batch/buckling_check.py consumption style)
# ----------------------------------------------------------------------

def _bar_stations(df, bar_id: int, length_m: float) -> List[Tuple[float, float]]:
    """[(pos_frac, MY in N·m)] for one bar from the force export frame."""
    sub = df[df["Bar_ID"] == int(bar_id)] if df is not None else None
    if sub is None or sub.empty:
        return []
    out = []
    for _, row in sub.iterrows():
        pos = float(row["Position_m"])
        frac = pos / length_m if length_m > 0.0 else 0.0
        out.append((round(frac, 6), float(row.get("MY_kNm", 0.0)) * 1e3))
    return out


def _bar_axial_at_max_moment(df, bar_id: int) -> float:
    """FX (N) at the max-|MY| station (the critical LTB station)."""
    sub = df[df["Bar_ID"] == int(bar_id)] if df is not None else None
    if sub is None or sub.empty:
        return 0.0
    idx = int(sub["MY_kNm"].abs().idxmax())
    return float(sub.loc[idx, "FX_kN"]) * 1e3


def _check_one_bar(bridge, bar_id: int, sec_name: str, length_m: float,
                   df, case_id: int) -> Dict[str, Any]:
    """Gathers live section / material / bracing / forces for one bar and
    runs the pure check_ltb_member."""
    base = {"bar_id": int(bar_id)}
    if not sec_name:
        return {**base, "status": "NOT_CHECKABLE",
                "reason": "bar has no section assigned."}
    props = read_section_props(bridge, sec_name)
    if props is None:
        return {**base, "status": "NOT_CHECKABLE",
                "reason": f"could not load section data for '{sec_name}'."}
    props["name"] = sec_name

    bar_obj = bridge.structure.Bars.Get(bar_id)
    try:
        re_mpa, mat_name, _reason = bridge._bar_strength_mpa(bar_obj)
    except Exception as exc:
        return {**base, "status": "NOT_CHECKABLE",
                "reason": f"material lookup failed: {exc}"}
    fy_mpa, fy_source = effective_yield_strength(
        re_mpa, mat_name, props["tf_m"] * 1000.0)
    if fy_mpa is None:
        return {**base, "status": "NOT_CHECKABLE",
                "reason": f"no design strength for material '{mat_name}'."}

    resolved = bridge.bracing.resolve(bar_id, length_m)
    stations = _bar_stations(df, bar_id, length_m)
    axial_n = _bar_axial_at_max_moment(df, bar_id)
    warnings = list(resolved.get("warnings") or [])
    result = check_ltb_member(
        props, fy_mpa * 1e6, length_m, stations, axial_n=axial_n,
        lcr_lt=resolved.get("lcr_lt_m"), lcr_lt_source=resolved.get("lcr_lt_source"),
        lcr_y=resolved.get("lcr_y_m"), lcr_y_source=resolved.get("lcr_y_source"),
        lcr_z=resolved.get("lcr_z_m"), lcr_z_source=resolved.get("lcr_z_source"),
        warnings=warnings)
    result["bar_id"] = int(bar_id)
    result["section"] = sec_name
    result["material"] = mat_name
    result["fy_source"] = fy_source
    return result


def check_lateral_torsional_buckling(
    bridge,
    case_id: int,
    bar_ids: Optional[List[int]] = None,
    divisions: int = 8,
) -> Dict[str, Any]:
    """[EUROCODE Phase C] Per-bar LTB + beam-column interaction check for a
    SOLVED case. Reads section props (live), material fy (RE capped by EN
    grade at the flange thickness), forces (export_all_member_forces) and
    the bracing side-table (default-and-warn) — exactly the consumption
    pattern of batch/buckling_check.py.

    Returns {"case_id", "bars": [per-bar results], "note": LTB_NOTE}.
    """
    bridge._ensure_connected()
    real_bars = bridge._real_bar_ids()
    if int(case_id) not in [num for num, _ in bridge._iter_all_cases()]:
        raise ValueError(
            f"case/combination {case_id} does not exist "
            f"(real cases: {sorted(num for num, _ in bridge._iter_all_cases())}).")
    ids = [int(b) for b in bar_ids] if bar_ids else real_bars
    for b in ids:
        if b not in real_bars:
            raise ValueError(
                f"bar {b} does not exist in the model (real bars: "
                f"{sorted(real_bars)[:10]}).")

    df = bridge.export_all_member_forces(case_id=int(case_id),
                                         divisions=max(2, int(divisions)))
    bars_srv = bridge.structure.Bars
    rows = []
    for bar_id in ids:
        length_m = bridge._bar_length(bar_id)
        sec_name = ""
        try:
            bar_obj = bars_srv.Get(bar_id)
            sec_name = str(bar_obj.GetLabelName(RobotEnum.I_LT_BAR_SECTION))
        except Exception:
            sec_name = ""
        rows.append(_check_one_bar(bridge, bar_id, sec_name, length_m,
                                   df, int(case_id)))
    return {"case_id": int(case_id), "bars": rows, "note": LTB_NOTE}





