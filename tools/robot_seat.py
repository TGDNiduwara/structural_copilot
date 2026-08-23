"""
tools/robot_seat.py
===================
Cross-process Robot "seat" ownership registry (no new dependencies).

WHY THIS EXISTS
---------------
Robot.exe is effectively a single seat on this machine AND two
independently-started processes can end up with two live COM sessions on
that seat (the interactive Streamlit app via ``GetActiveObject`` attach,
the headless batch chain via a fresh launch). On 2026-08-22 ~19:04 the
app attached to the batch chain's robot.exe, both drove one project, a
\"Do you want to save changes?\" modal appeared, the chain's dialog
watcher force-killed its own robot mid-solve, and a SECOND robot.exe was
spawned -> RPC corruption and a split \"two sessions\" state.

This module records WHO owns the seat so any process can answer \"who am
I stepping on?\" BEFORE attaching/spawning. A seat is proven owned when:
  * a seat file exists, AND
  * its owner_pid is a live process, AND
  * it references a robot.exe pid that is still alive.
Stale seats (dead owner or dead robot) are freely re-claimed.

Import-safe on non-Windows (tasklist usage is deferred to call time).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import time
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger("structural_copilot.robot_seat")

#: Owner kinds we recognise. A batch run must NEVER be attached-to by the
#: interactive app, and a second batch must detect the first one.
OWNER_KIND_APP = "app"
OWNER_KIND_BATCH = "batch"

_SEAT_FILE_NAME = "robot_seat.json"
_RUNTIME_DIR_NAME = "runtime"
_HEARTBEAT_SECONDS = 30.0
_HEADERS = {"schema": 1, "project": "structural_multi_app_agent"}


def _repo_root() -> str:
    # This file lives at <root>/tools/robot_seat.py
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _seat_dir() -> str:
    d = os.path.join(_repo_root(), _RUNTIME_DIR_NAME)
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return d


def _seat_path() -> str:
    return os.path.join(_seat_dir(), _SEAT_FILE_NAME)


def _pid_alive(pid: Optional[int]) -> bool:
    """True when a Windows process with the given pid exists (tasklist)."""
    if not pid:
        return False
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True, timeout=15,
        ).stdout
        return str(pid) in out
    except Exception:  # noqa: BLE001
        return False


def _robots_running() -> Set[int]:
    """Live robot.exe PIDs via tasklist (no COM needed)."""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq robot.exe",
             "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=20,
        ).stdout
    except Exception:  # noqa: BLE001
        return set()
    pids: Set[int] = set()
    for line in out.splitlines():
        parts = [p.strip().strip('"') for p in line.split('","')]
        if len(parts) >= 2 and parts[0].lower() == "robot.exe":
            try:
                pids.add(int(parts[1]))
            except ValueError:
                continue
    return pids


def _own_robot_alive(robot_pids: List[int]) -> bool:
    if not robot_pids:
        return False
    return bool(set(robot_pids) & _robots_running())


class SeatBusyError(RuntimeError):
    """Raised when this process tries to take the Robot seat while another
    LIVE process still owns it. Carries a full diagnostic in `message`."""


def _read_seat() -> Optional[Dict[str, Any]]:
    try:
        with open(_seat_path(), encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return None
    if not raw.get("owner_pid"):
        return None
    return raw


def _atomic_write(payload: Dict[str, Any]) -> None:
    """Write + atomic replace so concurrent readers never see a torn file."""
    path = _seat_path()
    fd, tmp = tempfile.mkstemp(prefix=".seat-", suffix=".tmp",
                               dir=_seat_dir(), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def seat_status() -> Dict[str, Any]:
    """Full diagnostic snapshot of the current seat state."""
    seat = _read_seat() or {}
    present = "owner_pid" in seat
    owner_alive = _pid_alive(seat.get("owner_pid")) if present else True
    robot_pids = seat.get("robot_pids") or []
    robots_alive = _own_robot_alive(robot_pids) if present else False
    return {
        "present": present,
        "schemaVersion": seat.get("schema"),
        "owner_pid": seat.get("owner_pid"),
        "owner_kind": seat.get("owner_kind"),
        "connected_via": seat.get("connected_via"),
        "robot_pids": sorted(robot_pids),
        "acquired_at": seat.get("acquired_at"),
        "last_seen": seat.get("last_seen"),
        "owner_alive": owner_alive if present else None,
        "robots_alive": robots_alive if present else None,
        "seat_available": (not present) or (not owner_alive or not robots_alive),
        "robots_running": sorted(_robots_running()),
    }


def claim_seat(owner_pid: Optional[int], owner_kind: str,
               robot_pids: Optional[List[int]] = None,
               connected_via: str = "attached",
               force: bool = False) -> Dict[str, Any]:
    """Take the seat for ``owner_pid``/``owner_kind``.

    Raises SeatBusyError when a LIVE, still-owning OTHER process holds it
    (and ``force`` is False). Returns the seat_status() now in place.
    """
    pid = int(owner_pid or os.getpid())
    kind = owner_kind if owner_kind in (OWNER_KIND_APP, OWNER_KIND_BATCH) \
        else OWNER_KIND_APP
    now = time.time()

    existing = _read_seat()
    if existing and not force:
        ex_owner = existing.get("owner_pid")
        if ex_owner == pid:
            # Same process already owns it — heartbeat and continue.
            pass
        elif _pid_alive(ex_owner) and _own_robot_alive(existing.get("robot_pids") or []):
            raise SeatBusyError(
                "Robot seat is owned by ANOTHER live process "
                f"(owner_pid={ex_owner}, "
                f"kind={existing.get('owner_kind') or '?'}, "
                f"robot pid(s)={sorted(existing.get('robot_pids') or [])}, "
                f"since {existing.get('acquired_at')}). "
                "Attaching or spawning a second Robot session here would "
                "corrupt both sessions (RPC drops, phantom save dialogs, "
                "stale ids). Inspect with tools.robot_seat.seat_status() "
                "or close the owning process first.")
        else:
            logger.info(
                "Stale Robot seat overwritten (owner=%s kind=%s robot_pids=%s).",
                ex_owner, existing.get("owner_kind"),
                sorted(existing.get("robot_pids") or []))

    payload = {
        **_HEADERS,
        "owner_pid": pid,
        "owner_kind": kind,
        "connected_via": str(connected_via),
        "robot_pids": sorted(int(p) for p in (robot_pids or []) if p),
        "acquired_at": now,
        "last_seen": now,
    }
    _atomic_write(payload)
    logger.info(
        "Robot seat claimed by pid %s (kind=%s, connected_via=%s, robot_pids=%s).",
        pid, kind, connected_via, payload["robot_pids"])
    return seat_status()


def heartbeat(owner_pid: Optional[int]) -> None:
    """Refresh last_seen if pid still owns the seat."""
    seat = _read_seat()
    if seat and seat.get("owner_pid") == int(owner_pid or os.getpid()):
        seat["last_seen"] = time.time()
        try:
            _atomic_write(seat)
        except OSError:
            pass


def release_seat(owner_pid: Optional[int]) -> None:
    """Clear the seat only when this process is its owner."""
    seat = _read_seat()
    if seat and seat.get("owner_pid") == int(owner_pid or os.getpid()):
        try:
            os.unlink(_seat_path())
        except OSError:
            pass
        logger.info("Robot seat released by pid %s.", owner_pid)