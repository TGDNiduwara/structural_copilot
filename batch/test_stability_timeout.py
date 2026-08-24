"""Step-2/3 validation: pre-solve stability + solve timeout/force-kill."""

import sys
import time

sys.path.insert(0, r"c:\Users\dinat\Downloads\structural_multi_app_agent\structural_copilot")
from batch.headless_driver import HeadlessSession, MechanismError, _robot_pids


def check(tag, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {tag} {detail}", flush=True)
    if not cond:
        raise SystemExit(f"assertion failed: {tag}")


def robot_count():
    return len(_robot_pids())


BASE = {
    "project": "2D",
    "nodes": [{"id": 1, "x": 0.0, "y": 0.0, "z": 0.0}, {"id": 2, "x": 6.0, "y": 0.0, "z": 0.0}],
    "bars": [{"id": 1, "n1": 1, "n2": 2, "section": "IPE 300"}],
    "supports": [{"node": 1, "type": "pinned"}, {"node": 2, "type": "pinned"}],
    "cases": [{"id": 1, "name": "Dead"}],
    "loads": [{"kind": "bar_uniform", "bar": 1, "case": 1, "direction": "Z", "value": 10.0}],
}

base_count = robot_count()

# ---- S2a: known-good baseline -> validate ok, solve ok ----
with HeadlessSession(visible=False) as s:
    s.build_from_spec(BASE)
    st = s.validate_stability()
    check("S2a baseline validate ok", bool(st.get("ok")), st["message"])
    r = s.solve_all(["static"])
    check("S2a baseline solve ok", r["static"]["status"] == "ok")

# ---- S2b: fixed-base column (the dialog repro) -> stable, solve returns ----
with HeadlessSession(visible=False) as s:
    s.new_2d_frame()
    s.bridge.create_node(1, 0.0, 0.0, 0.0)
    s.bridge.create_node(2, 0.0, 0.0, 5.0)
    s.bridge.create_bar(1, 1, 2, "HEA100")
    s.bridge.set_support(1, "fixed")
    s.bridge.create_load_case(1, "C")
    s.bridge.apply_nodal_load(2, 1, fx_kn=0.0, fz_kn=-300.0, my_knm=0.0)
    st = s.validate_stability()
    check("S2b fixed column validate ok (mathematically stable)", bool(st.get("ok")), st["message"])
    r = s.solve_all(["static"])
    fx = s.bridge.structure.Results.Bars.Forces.Value(1, 1, 0.5).FX
    print(f"  [S2b] solved axial FX = {fx:.1f} (no dialog, no hang)", flush=True)
    check("S2b solve returns under Interactive=0", r["static"]["status"] == "ok")

# ---- S2c: pinned column + lateral load -> mechanism flagged BEFORE solve ----
with HeadlessSession(visible=False) as s:
    s.new_2d_frame()
    s.bridge.create_node(1, 0.0, 0.0, 0.0)
    s.bridge.create_node(2, 0.0, 0.0, 5.0)
    s.bridge.create_bar(1, 1, 2, "HEA100")
    s.bridge.set_support(1, "pinned")
    s.bridge.create_load_case(1, "C")
    s.bridge.apply_nodal_load(2, 1, fx_kn=1.0, fz_kn=-10.0, my_knm=0.0)
    st = s.validate_stability()
    check(
        "S2c pinned+lateral mechanism flagged",
        (not st.get("ok")) and st.get("mechanism"),
        st["message"],
    )
    t0 = time.time()
    try:
        s.solve_all(["static"])
        check("S2c solve_all refuses", False, "no MechanismError raised")
    except MechanismError as exc:
        dt = time.time() - t0
        check("S2c MechanismError raised fast (no Calculate)", dt < 5.0, f"{dt:.2f}s: {exc}")

# ---- S3: solve timeout + force-kill (solve_timeout_s=0.2) ----
pre = robot_count()
with HeadlessSession(visible=False, solve_timeout_s=0.2) as s:
    s.build_from_spec(BASE)
    t0 = time.time()
    try:
        s.solve_all(["static"])
        check("S3 TimeoutError raised", False, "no TimeoutError")
    except TimeoutError:
        dt = time.time() - t0
        check("S3 TimeoutError after ~0.5s", 0.3 <= dt <= 15, f"{dt:.2f}s")
    except Exception as exc:  # noqa: BLE001
        check("S3 expected TimeoutError", False, f"{type(exc).__name__}: {exc}")
post = robot_count()
check(
    "S3 force-kill left no orphaned robot.exe",
    post == base_count,
    f"before={base_count} after={post}",
)

print("\nSTEP 2 + STEP 3 TESTS PASSED", flush=True)
