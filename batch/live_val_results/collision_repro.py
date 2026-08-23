# collision_repro.py - reproduces TONIGHT'S original bug as a real test.
# Run ONLY while a batch process holds the seat (live foreign owner).
# Simulates the Streamlit app's connect path (RobotBridge().connect with
# new_instance=False, exactly what ToolExecutor._ensure_robot does) from a
# SEPARATE process, and verifies it raises SeatBusyError BEFORE touching
# COM - so it neither attaches to nor spawns a second robot.exe.
from __future__ import annotations
import sys
import subprocess
ROOT = r"c:\Users\dinat\Downloads\structural_multi_app_agent\structural_copilot"
sys.path.insert(0, ROOT)


def _robot_pids():
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq robot.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=20).stdout
    except Exception:
        return set()
    pids = set()
    for line in out.splitlines():
        parts = [p.strip().strip('"') for p in line.split('","')]
        if len(parts) >= 2 and parts[0].lower() == "robot.exe":
            try:
                pids.add(int(parts[1]))
            except ValueError:
                continue
    return pids


def main():
    from tools.robot_seat import seat_status
    st = seat_status()
    print("SEAT STATUS at repro start:")
    print(f"  seat_available={st['seat_available']} "
          f"owner_pid={st['owner_pid']} kind={st['owner_kind']} "
          f"robot_pids={st['robot_pids']} owner_alive={st['owner_alive']} "
          f"robots_alive={st['robots_alive']}")
    robots_before = _robot_pids()
    print(f"  robots_running before={sorted(robots_before)}")
    if st["seat_available"]:
        print("  !!! seat is AVAILABLE - this repro needs a LIVE foreign "
              "owner. Aborting cleanly (nothing touched).")
        return 2

    from tools.robot_tool import RobotBridge
    from tools.robot_seat import SeatBusyError
    bridge = RobotBridge()
    try:
        bridge.connect(visible=True, new_instance=False)  # the APP path
        print("RESULT: connect() SUCCEEDED (BAD - attached to/overrode the "
              "foreign owner's session). This test FAILED.")
        try:
            bridge.close()
        except Exception:
            pass
        return 1
    except SeatBusyError as exc:
        print(f"RESULT: connect() raised SeatBusyError - PASS")
        print(f"  message: {str(exc)[:400]}")
    except Exception as exc:  # noqa: BLE001
        print(f"RESULT: connect() raised {type(exc).__name__} - FAIL "
              f"(expected SeatBusyError). {str(exc)[:300]}")
        try:
            bridge.close()
        except Exception:
            pass
        return 1

    robots_after = _robot_pids()
    print(f"RESULT: robots_running after={sorted(robots_after)}")
    new = robots_after - robots_before
    if new:
        print(f"RESULT: SPAWNED/ATTACHED new robot.exe {sorted(new)} - FAIL "
              "(must NOT spawn).")
        return 1
    print("RESULT: NO new robot.exe spawned, NO attach happened - the "
          "collision is prevented at the source. PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())