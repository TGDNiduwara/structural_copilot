"""
tools/eurocode_members.py
=========================
[EUROCODE Phase E — eurocode_scope.md §7]
Integration wrapper: one per-bar verdict across ALL Eurocode governing
checks, with the single worst-governing check reported — the same
enumeration style as Governing_Check today.

For a solved case, each bar gets all four checks:
    elastic    : get_utilization_ratios        (existing, first-yield / fy)
    buckling   : check_euler_buckling          (existing, minor-axis Euler)
    ltb        : check_lateral_torsional_buckling (§6.3.2.2 + §6.3.3)
    connection : check_connection_capacity     (EN 1993-1-8, defined joints)

Worst-governing ranking (repo convention): FAIL > NOT_CHECKABLE > PASS.
A NOT_CHECKABLE bar is reported but does not by itself fail the bar
(it is "not certified"); a FAIL from any check fails the bar. The
governing check name is reported alongside the overall status.

PURE-adjacent: reads only via the bridge; the heavy lifting is in the
individual modules. Forces are exported once and reused.

Author: Principal Structural Software Architecture Team
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from tools.ltb_check import check_lateral_torsional_buckling

#: Worst-governing ranking (highest wins).
_STATUS_RANK = {"FAIL": 2, "NOT_CHECKABLE": 1, "PASS": 0}
_CHECK_ORDER = ["elastic", "buckling", "ltb", "connection"]


def worst_of(per: Dict[str, Dict[str, Any]]) -> tuple:
    """PURE: worst-governing (governing_check, overall_status) over the
    four per-check status dicts. FAIL > NOT_CHECKABLE > PASS; ties break
    toward the earlier check in _CHECK_ORDER."""
    ranked = [(ch, _STATUS_RANK.get(per[ch]["status"], -1))
              for ch in _CHECK_ORDER if ch in per]
    worst_check, worst_rank = max(
        ranked, key=lambda t: (t[1], -_CHECK_ORDER.index(t[0])))
    overall = "FAIL" if worst_rank == 2 else (
        "NOT_CHECKABLE" if worst_rank == 1 else "PASS")
    return worst_check, overall


def check_eurocode_members(
    bridge,
    case_id: int,
    bar_ids: Optional[List[int]] = None,
) -> Dict[str, Any]:
    """Worst-of-all-checks per bar for a SOLVED case.

    Returns {"case_id", "bars": [{bar_id, section, overall_status,
    governing_check, elastic, buckling, ltb, connection, warnings}]}.
    """
    bridge._ensure_connected()
    real_bars = bridge._real_bar_ids()
    ids = [int(b) for b in bar_ids] if bar_ids else real_bars
    for b in ids:
        if b not in real_bars:
            raise ValueError(f"bar {b} does not exist in the model.")

    df = bridge.export_all_member_forces(case_id=int(case_id), divisions=8)

    # 1) elastic utilization (existing)
    util_df = bridge.get_utilization_ratios(case_id=int(case_id))
    util_by_bar: Dict[int, Dict[str, Any]] = {}
    if util_df is not None and not util_df.empty:
        for _, row in util_df.iterrows():
            try:
                util_by_bar[int(row["Bar_ID"])] = row.to_dict()
            except (TypeError, ValueError):
                continue

    # 2) Euler buckling (existing) — compression members only
    from batch.buckling_check import check_euler_buckling
    euler_by_bar: Dict[int, Dict[str, Any]] = {}
    for bar_id in ids:
        sub = df[df["Bar_ID"] == bar_id] if df is not None else None
        if sub is None or sub.empty:
            continue
        axial_kN = float(sub["FX_kN"].iloc[len(sub) // 2])
        if axial_kN >= 0.0:
            continue
        try:
            res = check_euler_buckling(bridge, bar_id, int(case_id),
                                       axial_force_kn=axial_kN)
            euler_by_bar[bar_id] = {
                "status": ("PASS" if res.get("pass_fail") else "FAIL")
                           if res.get("applies") else "N/A",
                "detail": res.get("note", ""),
                "utilization": None,
            }
        except Exception:
            euler_by_bar[bar_id] = {"status": "NOT_CHECKABLE",
                                    "reason": "Euler check failed",
                                    "utilization": None}

    # 3) LTB + interaction (§6.3.2.2 + §6.3.3)
    ltb = check_lateral_torsional_buckling(bridge, int(case_id), ids)
    ltb_by_bar = {r["bar_id"]: r for r in ltb["bars"]}

    # 4) connections (defined joints only)
    conn_by_bar: Dict[int, Dict[str, Any]] = {}
    for conn in bridge.connections.all_connections():
        bar_id = int(conn["bar_id"])
        if bar_id not in ids:
            continue
        try:
            res = bridge.check_connection_capacity(
                bar_id, conn["joint_end"], case_id=int(case_id))
            conn_by_bar[bar_id] = {"status": res.get("status"),
                                   "utilization": res.get("utilization"),
                                   "governing": res.get("governing"),
                                   "joint_end": conn["joint_end"]}
        except Exception as exc:
            conn_by_bar[bar_id] = {"status": "NOT_CHECKABLE",
                                   "reason": f"connection check failed: {exc}",
                                   "utilization": None}

    rows = []
    for bar_id in ids:
        u = util_by_bar.get(bar_id, {})
        e = euler_by_bar.get(bar_id, {})
        l = ltb_by_bar.get(bar_id, {})
        c = conn_by_bar.get(bar_id, {})
        per = {
            "elastic": {"status": u.get("Status", "N/A"),
                        "utilization": u.get("Utilization")},
            "buckling": {"status": e.get("status", "N/A"),
                         "utilization": e.get("utilization")},
            "ltb": {"status": l.get("status", "N/A"),
                    "utilization": l.get("utilization"),
                    "source": l.get("lcr_lt_source")},
            "connection": {"status": c.get("status", "N/A"),
                           "utilization": c.get("utilization"),
                           "governing": c.get("governing")},
        }
        worst_check, overall = worst_of(per)
        rows.append({
            "bar_id": bar_id,
            "section": u.get("Section") or l.get("section") or "",
            "overall_status": overall,
            "governing_check": worst_check,
            "checks": per,
            "warnings": list(l.get("warnings") or []),
        })
    return {"case_id": int(case_id), "bars": rows,
            "note": "Worst-governing across elastic / Euler buckling / "
                    "LTB (§6.3.2.2) / connection (EN 1993-1-8). NOT_CHECKABLE "
                    "means not certified (never a silent pass)."}

