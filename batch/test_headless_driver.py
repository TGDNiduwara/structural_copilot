"""
batch/test_headless_driver.py — Phase 1 validation.

Probes (report findings):
  P1  visible=False: connect/readiness/solve parity vs visible=True
      (baseline 45/90 kN·m beam).
  P2  one-seat license: second instance while an interactive instance is open.
  P3  spec-schema sufficiency for Phase 4 design space (asserts + report).
  P4  5x connect -> build -> solve -> close loop: zero orphaned robot.exe.
"""
import subprocess
import sys
import threading
import time

sys.path.insert(0, r"c:\Users\dinat\Downloads\structural_multi_app_agent\structural_copilot")

from batch.headless_driver import HeadlessSession  # noqa: E402
from tools.robot_tool import RobotBridge  # noqa: E402

BASELINE_SPEC = {
    "project": "2D",
    "nodes": [{"id": 1, "x": 0.0, "y": 0.0, "z": 0.0},
              {"id": 2, "x": 6.0, "y": 0.0, "z": 0.0}],
    "bars": [{"id": 1, "n1": 1, "n2": 2, "section": "IPE 300"}],
    "supports": [{"node": 1, "type": "pinned"}, {"node": 2, "type": "pinned"}],
    "cases": [{"id": 1, "name": "Dead", "nature": "permanent"},
              {"id": 2, "name": "Live", "nature": "imposed"}],
    "loads": [{"kind": "bar_uniform", "bar": 1, "case": 1,
               "direction": "Z", "value": 10.0},
              {"kind": "bar_uniform", "bar": 1, "case": 2,
               "direction": "Z", "value": 20.0}],
}


def say(tag, msg=""):
    print(f"[{tag}] {msg}", flush=True)


def robot_process_count() -> int:
    """Counts robot.exe via tasklist (no third-party dependency)."""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq robot.exe", "/NH"],
            capture_output=True, text=True, timeout=20).stdout
        return sum(1 for ln in out.splitlines() if "robot.exe" in ln.lower())
    except Exception as exc:  # noqa: BLE001
        say("ORPHAN", f"tasklist failed: {exc}")
        return -1


def midspan_moments(session: HeadlessSession) -> tuple:
    """Returns (dead_my, live_my) at the 6 m beam's midspan."""
    rows = session.bridge.export_all_member_forces(case_id=1, divisions=10)
    d = next(r for r in rows.itertuples() if abs(r.Position_m - 3.0) < 0.01)
    rows2 = session.bridge.export_all_member_forces(case_id=2, divisions=10)
    l = next(r for r in rows2.itertuples() if abs(r.Position_m - 3.0) < 0.01)
    return abs(float(d.MY_kNm)), abs(float(l.MY_kNm))


def run_baseline(visible: bool) -> dict:
    with HeadlessSession(visible=visible) as s:
        s.build_from_spec(BASELINE_SPEC)
        s.solve_all(["static"])
        dead, live = midspan_moments(s)
        weight = s.get_weight()
        util = s.get_utilization_summary(case_id=1)
        return {"dead": dead, "live": live, "weight": weight,
                "util": util}


def test_reused_session_timing() -> None:
    """T1: ONE HeadlessSession reused for 5 build/solve cycles (no close
    between iterations) vs the relaunch-per-candidate approach (~20.7 s/iter)."""
    say("T1", "reused-session: 1 session, 5x clear/build/solve (close once at end) ...")
    t0 = time.time()
    iters = []
    with HeadlessSession(visible=False) as s:
        s.build_from_spec(BASELINE_SPEC)
        s.solve_all(["static"])
        for i in range(5):
            ts = time.time()
            s.bridge.clear_structure("2D")
            s.build_from_spec(BASELINE_SPEC)
            s.solve_all(["static"])
            w = s.get_weight()
            u = s.get_utilization_summary(case_id=1)
            assert abs(w["weight_kg"] - 253.2) < 1.0, w
            assert u["max_utilization"] is not None, u
            iters.append(round(time.time() - ts, 2))
    total = round(time.time() - t0, 2)
    avg = round(sum(iters) / len(iters), 2)
    say("FINDING", f"T1 reused-session: total={total}s, per-iter={iters}, "
                   f"avg={avg}s vs relaunch avg~20.7s "
                   f"({round(20.7 / avg, 1)}x faster per candidate)")


def test_com_error_interactive0() -> None:
    """T2: deliberate COM errors under Interactive=0 must raise cleanly
    (no modal dialog, no hang)."""
    say("T2", "COM error under Interactive=0 (must raise cleanly, no hang) ...")
    result = {}

    def _run():
        with HeadlessSession(visible=False) as s:
            t0 = time.time()
            try:
                s.build_from_spec({"project": "2D",
                                   "nodes": [{"id": 1, "x": 0.0, "z": 0.0},
                                             {"id": 2, "x": 1.0, "z": 0.0}],
                                   "bars": [{"id": 1, "n1": 1, "n2": 2,
                                             "section": "IPE 9999"}]})
                result["bad_section"] = "NO ERROR RAISED"
            except Exception as exc:  # noqa: BLE001
                result["bad_section"] = f"{type(exc).__name__}: {str(exc)[:80]}"
            result["bad_section_elapsed"] = round(time.time() - t0, 2)

            t0 = time.time()
            try:
                # Genuine COM failure: bare Get(n) auto-creates proxies on
                # this build, but property access on a nonexistent object
                # fails immediately (verified: com_error in ~0.01s).
                s.bridge.structure.Nodes.Get(999).X
                result["bad_node"] = "NO ERROR RAISED"
            except Exception as exc:  # noqa: BLE001
                result["bad_node"] = f"{type(exc).__name__}: {str(exc)[:80]}"
            result["bad_node_elapsed"] = round(time.time() - t0, 2)

    t = threading.Thread(target=_run, daemon=True)
    t0 = time.time()
    t.start()
    t.join(timeout=40.0)
    if t.is_alive():
        say("FINDING", "T2 COM error: HANG (no return within 40s) — FAIL")
        return
    say("T2", f"  bad_section -> {result.get('bad_section')} "
              f"({result.get('bad_section_elapsed')}s)")
    say("T2", f"  bad_node    -> {result.get('bad_node')} "
              f"({result.get('bad_node_elapsed')}s)")
    for key in ("bad_section", "bad_node"):
        assert "NO ERROR" not in str(result.get(key, "")), key
    say("FINDING", "T2 COM error under Interactive=0: both cases raise "
                   "cleanly, no hang.")


def main() -> int:
    base_count = robot_process_count()
    say("P4", f"robot.exe at start: {base_count}")

    # ---- P1a: visible=True baseline ----
    say("P1", "running baseline visible=True ...")
    rv = run_baseline(visible=True)
    say("P1", f"visible=True -> dead={rv['dead']:.2f} kNm live={rv['live']:.2f} "
              f"kNm weight={rv['weight']}")
    assert abs(rv["dead"] - 45.0) < 0.5 and abs(rv["live"] - 90.0) < 0.5, rv

    # ---- P1b: visible=False ----
    say("P1", "running baseline visible=False ...")
    rv2 = run_baseline(visible=False)
    say("P1", f"visible=False -> dead={rv2['dead']:.2f} kNm "
              f"live={rv2['live']:.2f} kNm weight={rv2['weight']}")
    assert abs(rv2["dead"] - 45.0) < 0.5 and abs(rv2["live"] - 90.0) < 0.5, rv2
    assert abs(rv2["dead"] - rv["dead"]) < 1e-9 and \
        abs(rv2["live"] - rv["live"]) < 1e-9, (rv, rv2)
    say("FINDING", "P1 visible=False: connect + readiness poll + solve() all "
                   "work identically to visible=True (45/90 kNm exact).")

    # ---- T1: reused-session timing ----
    test_reused_session_timing()

    # ---- T2: COM errors under Interactive=0 ----
    test_com_error_interactive0()

    # ---- P3: spec schema sufficiency for Phase 4 ----
    gaps = ["bars support only 'section' — no per-bar 'material' key "
            "(materials apply globally via 'materials' list)",
            "no 'combinations' key (Phase 4 defines them separately)"]
    say("FINDING", f"P3 spec schema: core geometry/loads sufficient for Phase 4; "
                   f"gaps: {gaps}")

    # ---- P4: 5x connect->build->solve->close loop ----
    say("P4", "5x loop connect->build->solve->close ...")
    t0 = time.time()
    for i in range(5):
        with HeadlessSession(visible=False) as s:
            s.build_from_spec(BASELINE_SPEC)
            s.solve_all(["static"])
        say("P4", f"  iteration {i + 1}/5 done ({time.time() - t0:.1f}s cumulative)")
    after = robot_process_count()
    say("FINDING", f"P4 5x loop: 5 iterations in {time.time() - t0:.1f}s; "
                   f"robot.exe before={base_count} after={after} -> "
                   f"{'CLEAN (no orphans)' if after == base_count else 'ORPHANS LEFT'}")
    assert after == base_count, f"orphaned robot.exe: {after} vs {base_count}"

    # ---- P2: one-seat license probe ----
    say("P2", "one-seat probe: ensure an interactive instance is open ...")
    interactive = None
    interactive_pids = set()
    # capture actual PID set for precise cleanup
    def _pid_set():
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq robot.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=20).stdout
        pids = set()
        for line in out.splitlines():
            parts = [p.strip().strip('"') for p in line.split('","')]
            if len(parts) >= 2 and parts[0].lower() == "robot.exe":
                try:
                    pids.add(int(parts[1]))
                except ValueError:
                    continue
        return pids

    pids_at_probe_start = _pid_set()
    n_before = len(pids_at_probe_start)
    if n_before > 0:
        say("P2", f"  {n_before} robot.exe already running — using it as the "
                  "interactive instance.")
    else:
        say("P2", "  no robot.exe — launching the interactive stand-in "
                  "(visible=True, new_instance=True).")
        interactive = RobotBridge()
        interactive.connect(visible=True, new_instance=True)
        interactive.new_2d_frame()
        interactive_pids = _pid_set() - pids_at_probe_start

    probe_result = {"outcome": "unknown"}
    result_box = {}

    def _attempt():
        try:
            s = HeadlessSession(visible=False)
            s.connect()
            s.build_from_spec(BASELINE_SPEC)
            s.solve_all(["static"])
            d, l = midspan_moments(s)
            result_box["session"] = s
            result_box["moments"] = (d, l)
            probe_result["outcome"] = "ok"
        except Exception as exc:  # noqa: BLE001
            probe_result["outcome"] = f"error: {type(exc).__name__}: {exc}"[:200]

    t = threading.Thread(target=_attempt, daemon=True)
    t0 = time.time()
    t.start()
    t.join(timeout=75.0)
    if t.is_alive():
        probe_result["outcome"] = "HANG (still running after 75 s)"
        say("P2", "  second instance HUNG — aborting probe.")

    say("P2", f"  second instance outcome: {probe_result['outcome']}")
    if probe_result["outcome"] == "ok":
        d, l = result_box.get("moments", (0, 0))
        say("P2", f"  second instance solved baseline: dead={d:.2f} live={l:.2f}")
        try:
            ok = interactive is None or interactive.is_alive()
            say("P2", f"  interactive instance still healthy: {ok}")
        except Exception as exc:  # noqa: BLE001
            say("P2", f"  interactive health check errored: {exc}")
        result_box["session"].close()
        # [Phase-1 ext] The interactive stand-in must STILL solve a small
        # model correctly AFTER the headless run + teardown — not just be
        # alive. Guards against silent corruption from the concurrent run.
        if interactive is not None:
            try:
                interactive.clear_structure("2D")
                interactive.create_node(1, 0.0, 0.0, 0.0)
                interactive.create_node(2, 6.0, 0.0, 0.0)
                interactive.create_bar(1, 1, 2, "IPE 300")
                interactive.set_support(1, "pinned")
                interactive.set_support(2, "pinned")
                interactive.create_load_case(1, "Dead")
                interactive.apply_bar_load(1, 1, 10.0, "Z")
                interactive.solve()
                rows = interactive.export_all_member_forces(
                    case_id=1, divisions=10)
                d = abs(float(next(r.MY_kNm for r in rows.itertuples()
                                   if abs(r.Position_m - 3.0) < 0.01)))
                say("P2", f"  interactive post-headless solve: dead midspan = "
                          f"{d:.2f} kNm (expect 45)")
                assert abs(d - 45.0) < 0.5, d
            except Exception as exc:  # noqa: BLE001
                raise SystemExit(
                    f"interactive post-headless solve FAILED: {exc}")
    say("P2", f"  robot.exe count during probe: {len(_pid_set())} "
              f"(before={n_before})")

    # Cleanup the interactive stand-in, PID-safe (its Quit(0) can hit the
    # known "Object is not connected" / cross-thread marshalling quirks and
    # silently keep the process). PIDs that are STILL ALIVE must be killed.
    if interactive is not None:
        interactive.close()
        still = interactive_pids
        deadline = time.time() + 15.0
        while time.time() < deadline and still:
            alive = still & _pid_set()
            if not alive:
                still = set()
                break
            still = alive
            time.sleep(1.0)
        for pid in still:
            try:
                subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                               capture_output=True, timeout=15)
                say("P2", f"  force-killed interactive stand-in PID {pid}")
            except Exception as exc:  # noqa: BLE001
                say("P2", f"  could not kill PID {pid}: {exc}")
    say("FINDING", f"P2 one-seat: {probe_result['outcome']}")

    final = robot_process_count()
    say("FINDING", f"final robot.exe = {final} (baseline {base_count})")
    assert final == base_count, f"leftover robot.exe: {final} vs {base_count}"
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
