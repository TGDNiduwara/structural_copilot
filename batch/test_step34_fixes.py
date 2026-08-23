"""
batch/test_step34_fixes.py
==========================
Offline (no-Robot) regression tests for the Step 3-4 bugfixes:

  * S4.1 angle-section candidate expansion + catalog activation breadth
  * S4.2 section-input leak guard (_validate_section_input)
  * S4.3 headless dialog patterns now include the save-changes prompt
  * S4.4 (tool schema text) create_structure_from_spec chunking guidance
  * S4.5 spec_integrity_issues pre-build bar-count guard
  * S3  seat registry claim/release/busy semantics (temp dir)

Run: venv/Scripts/python.exe batch/test_step34_fixes.py
"""
from __future__ import annotations

import os
import tempfile

ROOT = r"c:\Users\dinat\Downloads\structural_multi_app_agent\structural_copilot"
import sys
sys.path.insert(0, ROOT)

from tools.robot_tool import RobotBridge, RobotEnum  # noqa: E402
from batch.headless_driver import DEFAULT_DIALOG_PATTERNS  # noqa: E402
from agent.tool_registry import TOOL_SCHEMAS, ToolExecutor  # noqa: E402

fails = []


def check(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    print(f"  [{tag}] {name} {detail}")
    if not cond:
        fails.append(name)


print("STEP 4.2 - section input leak guard")
try:
    RobotBridge._validate_section_input("IPE  chord")
    check("'IPE  chord' rejected", False)
except ValueError as e:
    check("'IPE  chord' rejected", "placeholder" in str(e).lower(),
          f"-> {str(e)[:80]}")
check("'IPE300' accepted", RobotBridge._validate_section_input("IPE300") == "IPE300")
check("'IPE 300' accepted", RobotBridge._validate_section_input("IPE 300") == "IPE 300")
check("'L 120x120x5' accepted",
      RobotBridge._validate_section_input("L 120x120x5") == "L 120x120x5")
try:
    RobotBridge._validate_section_input("")
    check("empty rejected", False)
except ValueError:
    check("empty rejected", True)
try:
    RobotBridge._validate_section_input("<chord_placeholder>")
    check("placeholder punctuation rejected", False)
except ValueError:
    check("placeholder punctuation rejected", True)

print("== S4.1: angle section name candidates")
cands = RobotBridge._section_label_candidates("L 120X120X5")
for want in ("L 120x120x5", "L 120X120X5", "L 120 x 120 x 5",
             "L120x120x5", "L120X120X5"):
    check(fcandidate := f"variant generated: {want}", want in cands)
# verify EURO is first so angles are searched before other catalogs
check("EURO listed first", RobotEnum.SECTION_DATABASES[0] == "EURO")

print("== S4.5: spec_integrity_issues")
bad_dup = {"nodes": [{"id": 1}, {"id": 1}], "bars": []}
issues = RobotBridge.spec_integrity_issues(bad_dup)
check("duplicate node id detected", any("duplicate node" in i for i in issues),
      str(issues))
bad_bar = {"nodes": [{"id": 1, "x": 0, "z": 0}, {"id": 2, "x": 1, "z": 0}],
           "bars": [{"id": 1, "n1": 1, "n2": 2}, {"id": 2, "n1": 1, "n2": 9}]}
issues = RobotBridge.spec_integrity_issues(bad_bar)
check("dangling bar ref detected", any("not defined" in i for i in issues))
good = {"nodes": [{"id": 1, "x": 0, "z": 0}, {"id": 2, "x": 1, "z": 0}],
        "bars": [{"id": 1, "n1": 1, "n2": 2}], "supports": []}
check("good spec returns no issues", RobotBridge.spec_integrity_issues(good) == [])

print("== S4.3: headless dialog patterns include save prompt")
check("'save changes to structure' in DEFAULT_DIALOG_PATTERNS",
      "save changes to structure" in DEFAULT_DIALOG_PATTERNS)
check("save prompt button=No",
      DEFAULT_DIALOG_PATTERNS.get("save changes to structure", {}).get("button_text") == "No")

print("== S4.4: create_structure_from_spec schema guidance")
schemas = {s["name"]: s for s in TOOL_SCHEMAS}
desc = schemas["create_structure_from_spec"]["description"]
check("chunking guidance present", "RELIABILITY" in desc and "~20 bars" in desc)
check("list_available_sections guidance present",
      "list_available_sections" in desc)

print("== S3: robot_session_status schema registered")
check("schema present", "robot_session_status" in schemas)
check("dispatchable", "_tool_robot_session_status" in dir(ToolExecutor))

print("== S4.1: list_available_sections angle family")
from tools.section_sizing import available_sections
ang = available_sections("L")
check("angle family returns RESOLVABLE names",
      "L 120x120x10" in ang and "L 50x50x5" in ang,
      f"(n={len(ang)})")
check("no bare leg sizes (the catalog-miss root cause)",
      "L 120" not in ang and "L 100x100x5" not in ang)
schemas_l = {s["name"]: s for s in TOOL_SCHEMAS}
las = schemas_l["list_available_sections"]["description"]
check("L-family guidance in description", "leg" in las.lower() and "x" in las,
      "-> description tells the LLM angles need leg-leg-thickness")

print("== S3: seat registry (temp-dir isolated)")
import tools.robot_seat as rs
_tmp = tempfile.mkdtemp(prefix="seat_test_")
rs._seat_dir = lambda: _tmp  # isolate from the repo runtime/ dir

# Simulate a genuinely LIVE previous owner + live robot (can't use real
# pids in an offline test - tasklist would say 'not found' -> stale).
_real_pid_alive, _real_own_alive = rs._pid_alive, rs._own_robot_alive
rs._pid_alive = lambda pid: pid in (1111, 1112)
rs._own_robot_alive = lambda rp: any(p in (5001, 5002) for p in rp)
try:
    s0 = rs.seat_status()
    check("empty seat available", s0["seat_available"] is True)
    c1 = rs.claim_seat(1111, rs.OWNER_KIND_BATCH, [5001], "launched")
    check("claim records ownership",
          c1["owner_pid"] == 1111 and c1["owner_kind"] == "batch"
          and c1["robots_alive"] is not None)
    try:
        rs.claim_seat(2222, rs.OWNER_KIND_APP, [5002], "attached")
        check("busy seat raises for other live owner", False)
    except rs.SeatBusyError as exc:
        check("busy seat raises for other live owner", "owned by ANOTHER" in str(exc))
    # [SEAT HARDENING] a claim with NO robot pid must fail loudly, never
    # write a malformed record that silently disarms the cross-process guard
    try:
        rs.claim_seat(1111, rs.OWNER_KIND_APP, [], "attached")
        check("empty robot_pids claim REFUSED", False)
    except RuntimeError as exc:
        check("empty robot_pids claim REFUSED",
              "at least one real robot.exe pid" in str(exc))
    check("malformed seat never written",
          rs.seat_status().get("owner_pid") != 1111 or
          rs.seat_status().get("robot_pids"))
    # same-owner re-claim is a heartbeat, not a conflict
    c2 = rs.claim_seat(1111, rs.OWNER_KIND_BATCH, [5001], "launched")
    check("same-owner re-claim allowed", c2["owner_pid"] == 1111)
    check("release only by owner",
          (rs.release_seat(7777) or True) and rs.seat_status()["present"] is True)
    rs.release_seat(1111)
    check("owner releases seat", rs.seat_status()["present"] is False)
    # stale seat (owner dead / robot dead) is freely reclaimed
    rs.claim_seat(3333, rs.OWNER_KIND_APP, [5003], "attached")
    rs._pid_alive = lambda pid: False
    rs._own_robot_alive = lambda rp: False
    c3 = rs.claim_seat(4444, rs.OWNER_KIND_BATCH, [5004], "launched")
    check("stale seat overwritten", c3["owner_pid"] == 4444 and c3["owner_kind"] == "batch")
finally:
    rs._pid_alive, rs._own_robot_alive = _real_pid_alive, _real_own_alive

print()
if fails:
    print(f"FAILED: {len(fails)} check(s): {fails}")
    raise SystemExit(1)
print("ALL STEP 3-4 OFFLINE FIX CHECKS PASSED")