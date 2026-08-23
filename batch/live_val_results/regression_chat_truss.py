"""
regression_chat_truss.py
========================
End-to-end regression for tonight's original failure, driven through the
REAL chat-interface path: ToolExecutor.dispatch() with the same tool
schemas/handlers the Streamlit app uses (no LLM, no HeadlessSession).

Scenario: 20m / 3m / 6-panel Pratt truss with EXPLICIT ANGLE web members
(not IPE substitutes) -> apply self-weight -> stability check -> solve ->
verify equilibrium (reactions balance applied loads) -> export results.

Per-step PASS/FAIL with real numbers:
  1. no angle-section resolution failure
  2. no leaked placeholder section strings
  3. robot_session_status shows ONE consistent session throughout
  4. reaction totals balance applied loads

Run ONLY after CHAIN_DONE + LIVE_BACKLOG_DONE exist and no batch/chain/
backlog python processes remain (seat free, fresh app connect).
"""
from __future__ import annotations

import json
import sys

ROOT = r"c:\Users\dinat\Downloads\structural_multi_app_agent\structural_copilot"
sys.path.insert(0, ROOT)

from agent.tool_registry import ToolExecutor  # noqa: E402

VAL = ROOT + r"\batch\live_val_results"
STEPS: list = []


def _call(ex, tool, args):
    """Dispatch through the REAL chat-interface path and return the dict
    the LLM would have received."""
    raw = ex.dispatch(tool, args)
    try:
        return json.loads(raw)
    except Exception:
        return {"status": "error", "raw": str(raw)[:300]}


def _step(name, ok, detail):
    STEPS.append({"name": name, "ok": bool(ok), "detail": detail})
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}", flush=True)


def _fz_sum(preview):
    """Sum of the FZ reaction column across preview records."""
    total = 0.0
    for row in preview or []:
        fz = row.get("FZ_kN")
        if fz is None:
            fz = row.get("FZ")
        if fz is None:
            continue
        try:
            total += float(fz)
        except (TypeError, ValueError):
            continue
    return total


def main():
    print("=" * 78)
    print("REGRESSION: 20m/3m/6-panel Pratt truss, explicit angle web, "
          "self-weight, stability, solve, equilibrium, export")
    print("=" * 78)

    # ---- Step 0: FRESH APP LAUNCH + connect (exactly app.py does) ----------- #
    # app.py: st.session_state.tool_executor = ToolExecutor(robot_visible=True)
    # and every Robot tool call goes through _ensure_robot() -> connect().
    ex = ToolExecutor(robot_visible=True)
    ex._ensure_robot()          # the app's first-connection trigger
    import os
    seat_path = ROOT + r"\runtime\robot_seat.json"
    seat_raw = open(seat_path, encoding="utf-8").read() \
        if os.path.exists(seat_path) else "(MISSING - connect failed to claim)"
    print("\n-- SEAT FILE AFTER FRESH APP CONNECT --")
    print(seat_raw)

    before = _call(ex, "robot_session_status", {})
    detail = before.get("detail", {})
    print("\n-- robot_session_status BEFORE (fresh app connect) --")
    print(json.dumps(detail, indent=1)[:900])
    seat_ok = (detail.get("connected") is True
               and detail.get("seat", {}).get("owner_kind") == "app")
    _step("fresh app connect claims seat (no SeatBusyError)", seat_ok,
          f"connected={detail.get('connected')} "
          f"pid={detail.get('connected_pid')} "
          f"via={detail.get('connected_via')} "
          f"seat_owner={detail.get('seat', {}).get('owner_pid')} "
          f"seat_file={seat_raw.strip()[:120]}")

    # ---- Step 1: build the truss with an EXPLICIT angle web ------------------ #
    angle_cands = ("L 50x50x5", "L 60x60x6", "L 80x80x8")
    built, used_angle = None, None
    for angle in angle_cands:
        r = _call(ex, "create_truss", {
            "span": 20.0, "height": 3.0, "panels": 6, "web_section": angle})
        if r.get("status") != "error":
            built, used_angle = r, angle
            break
        print(f"  angle {angle!r} rejected: {str(r)[:200]}")
    if built is None:
        _step("create_truss explicit angle web", False,
              f"ALL angle names failed: {list(angle_cands)}")
    else:
        n_nodes = built.get("nodes") or built.get("node_count")
        n_bars = built.get("bars") or built.get("bar_count")
        _step("create_truss explicit angle web", True,
              f"web_section={used_angle!r}; nodes={n_nodes} bars={n_bars} "
              f"status={built.get('status')}")

    # ---- Step 2: load case + self-weight -------------------------------------- #
    lc = _call(ex, "create_load_case", {"case_id": 1, "case_name": "SW",
                                        "nature": "permanent"})
    sw = _call(ex, "apply_self_weight", {"case_id": 1})
    sw_total = sw.get("total_self_weight_kn") if isinstance(sw, dict) else None
    _step("apply_self_weight", lc.get("status") == "ok"
          and isinstance(sw_total, (int, float)) and sw_total > 0,
          f"case_created={lc.get('status')} "
          f"self_weight_total_kn={sw_total} "
          f"bars_in_summary={sw.get('bars') if isinstance(sw, dict) else None}")

    # ---- Step 3: stability ------------------------------------------------------ #
    stab = _call(ex, "check_model_stability", {})
    _step("check_model_stability", stab.get("ok") is True,
          json.dumps(stab)[:250])

    # ---- Step 4: solve ---------------------------------------------------------- #
    sol = _call(ex, "solve", {"timeout_s": 120})
    _step("solve", sol.get("status") in ("ok", "ok_with_warning"),
          json.dumps(sol)[:300])

    # ---- Step 5: session consistency -------------------------------------------- #
    after = _call(ex, "robot_session_status", {})
    d_after = after.get("detail", {})
    same_pid = (detail.get("connected_pid") == d_after.get("connected_pid")
                and detail.get("connected_pid") is not None)
    _step("robot_session_status consistent (ONE session)", same_pid,
          f"pid before={detail.get('connected_pid')} "
          f"after={d_after.get('connected_pid')} "
          f"via={d_after.get('connected_via')}")

    # ---- Step 6: reactions + equilibrium ---------------------------------------- #
    reac = _call(ex, "export_reactions", {"case_id": 1})
    preview = reac.get("preview") if isinstance(reac, dict) else None
    sum_fz = round(_fz_sum(preview), 4) if preview else None
    eq_ok, eq_note = False, ""
    if sum_fz is not None and sw_total:
        rel = abs(abs(sum_fz) - sw_total) / sw_total
        eq_ok = rel < 0.02
        eq_note = (f"sum(FZ)={sum_fz} kN vs applied self-weight "
                   f"{sw_total:.4f} kN -> rel err {rel*100:.2f}% "
                   f"(threshold 2%)")
    _step("equilibrium: reactions balance applied loads", eq_ok, eq_note)

    # ---- Step 7: export member forces ------------------------------------------- #
    mf = _call(ex, "export_member_forces", {"case_id": 1, "divisions": 4})
    rows = mf.get("rows") if isinstance(mf, dict) else None
    _step("export_member_forces", isinstance(rows, int) and rows > 0,
          f"rows={rows} status={mf.get('status') if isinstance(mf, dict) else '?'}")

    # ---- Step 8: exported spec has NO placeholder sections ------------------ #
    spec = _call(ex, "export_structure_spec", {})
    geom = spec.get("geometry") if isinstance(spec, dict) else None
    bars_list = geom.get("bars") if isinstance(geom, dict) else None
    bad = []
    if bars_list:
        for b in bars_list:
            sec = str(b.get("section") or "")
            if "  " in sec or any(w in sec.lower() for w in
                                  ("chord", "web ", "beam", "placeholder")):
                bad.append((b.get("id"), sec))
    _step("no leaked placeholder section strings in exported spec",
          isinstance(spec, dict) and bars_list and not bad,
          f"checked {len(bars_list or [])} bars; "
          f"bad={bad if bad else 'none'} "
          f"loads={len(geom.get('loads') or []) if isinstance(geom, dict) else '?'}")

    with open(VAL + r"\regression_chat_truss.json", "w",
              encoding="utf-8") as fh:
        json.dump({
            "steps": STEPS,
            "session_before": before.get("detail", {}),
            "session_after": after.get("detail", {}),
            "angle_candidates": list(angle_cands), "used_angle": used_angle,
            "self_weight_total_kn": sw_total, "stability": stab,
            "reactions_preview": preview, "sum_fz": sum_fz,
        }, fh, indent=2, default=str)

    failed = [s for s in STEPS if not s["ok"]]
    print("\n" + "=" * 78)
    print(f"REGRESSION: {len(STEPS) - len(failed)}/{len(STEPS)} steps PASS")
    if failed:
        print("FAILED steps:", [s["name"] for s in failed])
        return 1
    print("ALL REGRESSION STEPS PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())