"""
tools/robot_tool.py
====================
Thread-safe COM bridge to Autodesk Robot Structural Analysis Professional.

This module wraps the Robot Structural Analysis Object Model (RobotOM) via
pywin32. Every public method that touches COM re-enters COM apartment
threading correctly by calling `pythoncom.CoInitialize()` / `CoUninitialize()`
around the call when invoked from a worker thread (e.g. a Streamlit
ScriptRunContext thread or an LLM tool-calling executor thread).

Requires:
    - Autodesk Robot Structural Analysis Professional installed and licensed
      on the local Windows machine (COM server is registered as
      "Robot.Application" at install time).
    - pywin32 (`pip install pywin32`)

Author: Principal Structural Software Architecture Team
"""

from __future__ import annotations

import functools
import logging
import math
import os
import re
import threading
import time
import numpy as np
from dataclasses import dataclass
from typing import Optional, List, Tuple, Dict, Any

from tools.geometry_primitives import (
    nodes_along_curve, connect_chords, radial_ring, circular_arc_fn,
    generate_straight_chord, generate_arc_chord, connect_web_pattern,
    connect_bracing, apply_support_pattern)
from tools.section_sizing import suggest_section, check_section_proportions
from tools.bracing_registry import BracingRegistry
from tools.connection_check import (ConnectionRegistry,
                                    check_simple_shear_connection)
from tools.section_data import read_section_props
from tools.eurocode_params import fu_for_grade

import pandas as pd

try:
    import pythoncom
    import pywintypes  # noqa: F401 — needed for specific exception catch
    import win32com.client as win32
    from win32com.client import CastTo
except ImportError:  # pragma: no cover - allows import on non-Windows CI
    pythoncom = None
    pywintypes = None
    win32 = None
    CastTo = None

logger = logging.getLogger("structural_copilot.robot_tool")
logger.setLevel(logging.INFO)

from tools.win_dialogs import _robot_pids

# Robot Object Model enumeration constants.
# These mirror the public RobotOM type library enums. They are declared
# locally (rather than relying solely on win32com's makepy-generated
# constants module) so the bridge still imports cleanly even before the
# gencache has been built for the first time.
# --------------------------------------------------------------------------

class RobotEnum:
    # IRobotProjectType
    I_PT_SHELL = 0
    I_PT_BAR_2D = 3           # Frame2D
    I_PT_BAR_3D = 4           # Frame3D

    # IRobotObjectType
    I_OT_BAR = 1
    I_OT_NODE = 0

    # IRobotLabelType — [FIX R5] values verified live against the COM server
    # by sweeping labels.Create(k, ...) and inspecting the returned Data class:
    #     0 -> IRobotNodeSupportData      3 -> IRobotBarSectionData
    #     4 -> IRobotBarReleaseData       5 -> IRobotBarOffsetData
    #     8 -> IRobotMaterialData
    # The previous values (section=5, support=8) silently created bar-offset
    # and material labels, so no real sections or supports were ever applied.
    I_LT_SUPPORT = 0             # I_LT_NODE_SUPPORT
    I_LT_BAR_SECTION = 3
    I_LT_BAR_RELEASE = 4
    I_LT_MATERIAL = 8            # [WP4] verified live (IRobotMaterialData)
    I_LT_THICKNESS = 11          # [WP4] verified live (IRobotThicknessData)

    # Section catalog identifiers — SHORT codes as they appear in Robot's
    # project Preferences SectionsActive/SectionsFound lists (verified live;
    # they match the catalog file names, e.g. EuroPro.xml -> "EURO").
    # LoadFromDBase(2) only searches catalogs that are ACTIVE in the project
    # preferences — fresh Robot profiles activate a US default set (AISC,
    # AISI, ARBU, NDS, RUSER, SJI), so EURO must be activated explicitly
    # (see _ensure_section_catalog_active).
    SECTION_DATABASES = [
        "EURO", "AISC", "DIN", "ARCLR", "UKST", "CISC", "CHINA", "JAPAN",
    ]

    # IRobotNodeSupportFixed flags (DOF): UX, UZ, RY for 2D frame
    I_DOF_UX = 1
    I_DOF_UZ = 1
    I_DOF_RY = 1

    # IRobotCaseNature — I_CN_PERMANENT = 0 per the type library.
    I_CN_PERMANENT = 0
    I_CN_IMPOSED = 1
    I_CAT_STATIC_LINEAR = 1
    I_CAT_DYNAMIC_MODAL = 11      # [WP7] verified live (IRobotCaseAnalizeType)
    I_CAT_COMB = 0                # [P5] verified live: combinations need
                                  # analize=I_CAT_COMB(0), NOT STATIC_LINEAR(1)

    # IRobotCombinationType — [P5] verified live via CreateCombination
    I_CBT_ULS = 0                 # ultimate / "effort"
    I_CBT_SLS = 1                 # serviceability
    I_CBT_ALS = 2                 # accidental
    I_CBT_SPC = 3                 # special
    CBT_NAMES = {"ULS": 0, "SLS": 1, "ALS": 2, "ACC": 2, "SPC": 3}

    # IRobotBarLoadDistributionType
    I_BLDT_UNIFORM = 0

    # IRobotLoadRecordType — [FIX R7] verified against the type library:
    # 0 = node force, 5 = bar uniform, 3 = concentrated bar force.
    I_LRT_NODE_FORCE = 0
    I_LRT_BAR_UNIFORM = 5
    I_LRT_BAR_FORCE_CONCENTRATED = 3

    # IRobotBarForceConcentrateRecordValues (verified live for Milestone A):
    # components FX/FY/FZ and the relative location REL (0..1) along the bar.
    I_BFCRV_FX = 0
    I_BFCRV_FY = 1
    I_BFCRV_FZ = 2
    I_BFCRV_REL = 13

    # IRobotSimpleValueList / result columns for bar forces
    I_RESULT_FX = 0
    I_RESULT_FZ = 2
    I_RESULT_MY = 4


def _require_pywin32():
    """Raises RuntimeError if pywin32 is not available."""
    if pythoncom is None or win32 is None:
        raise RuntimeError(
            "pywin32 is not available. RobotBridge requires Windows "
            "with pywin32 installed (`pip install pywin32`)."
        )


def com_thread_safe(method):
    """
    Decorator that ensures every COM call executed by a RobotBridge method
    runs inside a properly initialized COM apartment on the CALLING thread.

    Streamlit reruns scripts on worker threads, and LLM tool-executors may
    dispatch tool calls from a ThreadPoolExecutor -- both scenarios require
    a fresh CoInitialize() per thread because COM apartments are
    thread-affine (STA). We track initialization per-thread using a
    thread-local flag so we do not double-initialize/uninitialize on
    re-entrant calls from the same thread.
    """

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        _require_pywin32()

        tls = self._thread_local
        already_init = getattr(tls, "com_initialized", False)

        if not already_init:
            pythoncom.CoInitialize()
            tls.com_initialized = True

        try:
            with self._lock:
                return method(self, *args, **kwargs)
        finally:
            # [FIX M1] Properly cleanup COM apartment on the outermost call.
            # We only uninitialize when this was the outermost com_thread_safe
            # call on this thread (already_init was False), AND we are in the
            # finally block of that outermost call. However, to avoid
            # invalidating COM objects held by the bridge across calls, we
            # track a reference count instead.
            if not already_init:
                # Deliberately NOT calling CoUninitialize here.
                # RobotBridge holds COM object references (robot_app,
                # project, structure) across many separate method calls on
                # the same thread. Tearing the apartment down after every
                # call invalidates those references for the next call, which
                # was causing "Object is not connected to server" errors.
                # The apartment lives for the life of the thread; it is
                # cleaned up via close() or when the thread exits.
                pass

    return wrapper


@dataclass
class SectionDensity:
    """Standard steel density lookup (kg/m) fallback table, used when Robot
    label unit weight metadata is unavailable for a given section."""

    default_density_kg_m3: float = 7850.0


class RobotBridge:
    """
    High-level, thread-safe wrapper around the Autodesk Robot Structural
    Analysis COM Object Model.

    Usage:
        robot = RobotBridge()
        robot.connect()
        robot.new_2d_frame()
        robot.create_node(1, 0, 0, 0)
        ...
        robot.solve()
        df = robot.export_all_member_forces()
        robot.close()
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._thread_local = threading.local()
        self.robot_app = None
        self.project = None
        self.structure = None
        self._connected = False
        # [FIX R3] Launch circuit-breaker bookkeeping: timestamps of recent
        # robot.exe launches, used to cap how many fresh instances may be
        # started in a sliding window (prevents endless relaunch loops).
        self._launch_timestamps: List[float] = []
        self._section_assignments: Dict[int, str] = {}   # bar_id -> section label name
        self._node_coords: Dict[int, Tuple[float, float, float]] = {}
        self._bar_endpoints: Dict[int, Tuple[int, int]] = {}
        # In-memory project type ('2D' | '3D'); set by new_2d_frame /
        # new_3d_frame / clear_structure, consumed by export_structure_spec.
        self._project_type: str = "3D"
        # [WP4] Bookkeeping for grillage panels (node grid + bar ids).
        self._panel_meta: Dict[int, Dict[str, Any]] = {}
        # [EUROCODE Phase A] Engineer-specified bracing / unbraced lengths
        # side-table (Robot has no such property). Session-scoped; the
        # batch runner reaches it via bridge.bracing.
        self.bracing: BracingRegistry = BracingRegistry()
        # [EUROCODE Phase D] Engineer-specified connection side-table
        # (Robot has no connection-design server).
        self.connections: ConnectionRegistry = ConnectionRegistry()
        # [OBS] PID of the robot.exe process this bridge is connected to
        # (None when not connected). Captured at connect() for observability.
        self.connected_pid: Optional[int] = None
        # [SEAT] Cross-process seat ownership (see tools/robot_seat.py). The
        # pid that claimed the seat on this process's behalf, and its kind.
        self._seat_owner_pid: Optional[int] = None
        self._seat_kind: str = "app"
        # [INSTABILITY] Set by _guarded_calculate when the solver reported
        # an instability and the dialog was auto-answered 'Yes' (continue).
        # Surfaced in the solve tool result so the LLM/user is FORCED to see
        # that the model has a suspected mechanism - never silently ignored.
        self._last_instability_warning: Optional[str] = None

    # ------------------------------------------------------------------ #
    # Observability helpers
    # ------------------------------------------------------------------ #

    @property
    def pid(self) -> Optional[int]:
        """PID of the connected robot.exe (or None)."""
        if not self._connected:
            return None
        return self.connected_pid

    # ------------------------------------------------------------------ #
    # Connection lifecycle
    # ------------------------------------------------------------------ #

    def is_alive(self) -> bool:
        """
        [FIX H8] Health-check: verifies the COM server is still responsive.
        Returns False and resets connection state if the server is gone.

        [FIX R1] The probe property is `.Project` — the same readiness signal
        used by connect(). IRobotApplication has NO `.Name` property, so the
        old probe raised AttributeError on every healthy connection and was
        misreported as "connection lost", causing an endless relaunch loop.
        Only COM transport errors (pywintypes.com_error) indicate a lost
        server; AttributeError points at a stale gen_py wrapper cache and is
        surfaced distinctly WITHOUT marking the connection dead.
        """
        if not self._connected or self.robot_app is None:
            return False
        try:
            _ = self.robot_app.Project
            return True
        except AttributeError as exc:
            # The makepy wrapper itself is broken (stale gen_py cache) —
            # this is NOT a lost connection. Log loudly and keep the
            # connection state intact so we never spawn extra instances.
            logger.error(
                "Robot COM proxy raised AttributeError on the liveness probe "
                "(%s). This usually means a stale win32com gen_py cache. Fix: "
                "stop the app and delete the gen_py folder (locate it with: "
                "python -c \"import win32com; print(win32com.__gen_path__)\"), "
                "then restart.", exc,
            )
            return True  # The COM server object itself is still alive.
        except Exception as exc:
            if pywintypes is None or not isinstance(exc, pywintypes.com_error):
                logger.warning("Robot liveness probe failed unexpectedly: %s", exc)
            self._connected = False
            self.robot_app = None
            self.project = None
            self.structure = None
            return False

    # [FIX R3] Launch circuit-breaker limits.
    _MAX_LAUNCHES_PER_WINDOW = 2
    _LAUNCH_WINDOW_S = 90.0

    def _enforce_launch_circuit_breaker(self) -> None:
        """
        [FIX R3] Prevents runaway robot.exe spawning: at most
        _MAX_LAUNCHES_PER_WINDOW fresh COM instances may be launched within
        any sliding _LAUNCH_WINDOW_S second window. Beyond that, fail fast
        with an actionable message instead of launching another process.
        """
        now = time.time()
        self._launch_timestamps = [
            ts for ts in self._launch_timestamps
            if now - ts < self._LAUNCH_WINDOW_S
        ]
        if len(self._launch_timestamps) >= self._MAX_LAUNCHES_PER_WINDOW:
            raise RuntimeError(
                f"Robot launch circuit-breaker tripped: {len(self._launch_timestamps)} "
                f"launch attempts within {self._LAUNCH_WINDOW_S:.0f}s. Robot is "
                "likely stuck on a splash screen, license dialog, or 'select "
                "project type' prompt that needs a manual click. Open Robot "
                "manually, dismiss any dialogs, wait for a blank project, "
                "then retry."
            )

    @com_thread_safe
    def connect(self, visible: bool = True, new_instance: bool = False) -> None:
        """
        Connects to Robot Structural Analysis. Attaches to an already-running
        instance first (avoids license/instance conflicts); only launches a
        new process if none is running.
        """
        if self._connected:
            return

        # [SEAT] NEVER attach to / spawn a second Robot session over a live
        # seat owned by ANOTHER process (split-session corruption: RPC
        # drops, phantom save dialogs, force-kills). The seat registry
        # (tools/robot_seat.py) is the single cross-process authority.
        kind = "batch" if new_instance else "app"
        try:
            from tools.robot_seat import claim_seat, seat_status, SeatBusyError
            st = seat_status()
            if st["present"] and not st["seat_available"] \
                    and st["owner_pid"] != os.getpid():
                if new_instance:
                    # [SEAT] BATCH path: the previous chain stage's OWN
                    # process may still be exiting while its seat looks live
                    # (a genuine stage handoff race). Poll briefly for the
                    # seat to free/stale before refusing - an interactive app
                    # (new_instance=False) never waits and fails fast.
                    deadline = time.time() + 60.0
                    waited = 0.0
                    while time.time() < deadline:
                        time.sleep(5.0)
                        waited += 5.0
                        st = seat_status()
                        if not st["present"] or st["seat_available"] \
                                or st["owner_pid"] == os.getpid():
                            break
                        logger.info(
                            "robot seat held by batch pid %s - waiting "
                            "(%.0fs so far, robot=%s)",
                            st["owner_pid"], waited, st.get("robot_pids"))
                    else:
                        raise SeatBusyError(
                            "Robot seat stayed busy for 60s in batch "
                            "connect: another live process "
                            f"(owner_pid={st['owner_pid']}, "
                            f"kind={st['owner_kind']}, "
                            f"robot pid(s)={sorted(st['robot_pids'] or [])}) "
                            "did not release. Check for a stuck/duplicate "
                            "batch run with tools.robot_seat.seat_status().")
                else:
                    raise SeatBusyError(
                        "Robot seat is held by another live process "
                        f"(owner_pid={st['owner_pid']}, "
                        f"kind={st['owner_kind']}, "
                        f"robot pid(s)={' '.join(map(str, st['robot_pids']))}). "
                        "This process REFUSES to attach a second Robot session"
                        " - doing so corrupts both. If a batch run owns the "
                        "seat, wait for CHAIN_DONE; if the owner is dead, its "
                        "seat is auto-stale and the next connect takes over.")
        except SeatBusyError:
            raise
        except Exception as exc:  # noqa: BLE001 - seat layer must never block
            logger.warning("robot seat pre-check skipped: %s", exc)

        pids_before = _robot_pids()
        attached = False
        if not new_instance:
            try:
                self.robot_app = win32.GetActiveObject("Robot.Application")
                attached = True
                logger.info("Attached to an already-running Robot instance.")
            except Exception as exc:
                # [FIX M2] Only catch COM-specific errors, not KeyboardInterrupt etc.
                # pywintypes.com_error is the typical one, but win32com may raise
                # generic pywintypes.error too.
                if pywintypes is not None and isinstance(exc, pywintypes.com_error):
                    logger.info("No running Robot instance found; launching a new one.")
                else:
                    # For non-COM errors, re-raise unless it's a generic
                    # "object not found" type error from GetActiveObject
                    exc_str = str(exc).lower()
                    if "moniker" in exc_str or "getactiveobject" in exc_str or "object" in exc_str:
                        logger.info("No running Robot instance found; launching a new one.")
                    else:
                        raise

        if not attached:
            # [FIX R3] Circuit-breaker: refuse to spawn an endless stream of
            # robot.exe processes when the engine cannot become ready.
            self._enforce_launch_circuit_breaker()
            try:
                self.robot_app = win32.gencache.EnsureDispatch("Robot.Application")
            except Exception:
                self.robot_app = win32.Dispatch("Robot.Application")
            self._launch_timestamps.append(time.time())
            logger.info("Launched a new Robot Structural Analysis COM instance.")

        try:
            self.robot_app.Visible = 1 if visible else 0
            self.robot_app.Interactive = 1
        except Exception as exc:
            raise RuntimeError(
                "Connected to a Robot.Application COM object, but it did not "
                "respond to basic property access. This usually means a "
                "license conflict from a second instance, or Robot was still "
                "on a splash/license screen. Close all Robot windows, reopen "
                "Robot manually, wait for a blank project, then retry. "
                f"Original error: {exc}"
            ) from exc

        # The Application proxy comes back before Robot's internal engine has
        # finished attaching (splash screen / license check / UI init), so
        # touching .Project immediately raises "Object is not connected to
        # server". Poll until the engine is actually ready.
        ready_timeout = 60.0
        poll_interval = 1.0
        start = time.time()
        last_exc = None
        while time.time() - start < ready_timeout:
            try:
                _ = self.robot_app.Project
                last_exc = None
                break
            except Exception as exc:
                last_exc = exc
                time.sleep(poll_interval)
        if last_exc is not None:
            raise RuntimeError(
                "Connected to Robot.Application, but its engine did not "
                f"become ready within {ready_timeout:.0f}s (still getting: "
                f"{last_exc}). Check whether Robot is stuck on a splash "
                "screen, license prompt, or 'select project type' dialog "
                "that needs a manual click, then retry."
            ) from last_exc

        self._connected = True
        # [OBS] Determine which robot.exe PID this bridge is attached to.
        new_pids = _robot_pids()
        if attached:
            # Attached to an existing instance: use the (single) running PID.
            self.connected_pid = next(iter(new_pids), None)
        else:
            # Launched a fresh instance: the PID that appeared since we started.
            fresh = new_pids - pids_before
            self.connected_pid = next(iter(fresh), None)
        # [SEAT HARDENING] tasklist can lag a freshly launched robot.exe, and
        # claim_seat() now REFUSES an empty robot_pids list. Poll briefly so a
        # genuine connection is never denied a seat record; if the pid truly
        # cannot be found, fail loudly instead of silently leaving no seat.
        if self.connected_pid is None:
            for _ in range(24):  # up to ~6 s
                time.sleep(0.25)
                new_pids = _robot_pids()
                if attached:
                    self.connected_pid = next(iter(new_pids), None)
                else:
                    self.connected_pid = next(
                        iter(new_pids - pids_before), None)
                if self.connected_pid is not None:
                    break
        if self.connected_pid is None:
            raise RuntimeError(
                "Connected to Robot.Application but could not identify the "
                "robot.exe PID (tasklist shows no robot.exe for this session). "
                "Refusing to claim the Robot seat without a real robot PID - "
                "an empty seat record would let another process attach over "
                "this session and corrupt both. Check for a split/stale Robot "
                "session with tools.robot_seat.seat_status() and retry.")
        logger.info(
            "Robot session pid %s, connected via %s (seat owner pid %s).",
            self.connected_pid, "attach" if attached else "launch", os.getpid())
        # [SEAT] Record ownership so no other live process can attach over us.
        self._seat_owner_pid = os.getpid()
        self._seat_kind = kind
        try:
            from tools.robot_seat import claim_seat
            claim_seat(self._seat_owner_pid, kind,
                       [self.connected_pid] if self.connected_pid else [],
                       connected_via="attached" if attached else "launched")
        except Exception as exc:  # noqa: BLE001 - never fail connect on seat io
            logger.warning("robot seat claim failed (continuing): %s", exc)

    @com_thread_safe
    def close(self, save_path: Optional[str] = None) -> None:
        """Optionally saves the project, then releases the COM server."""
        if not self._connected:
            return
        pid_before = self.connected_pid
        try:
            if save_path and self.project is not None:
                self.project.SaveAs(save_path)
        finally:
            try:
                self.robot_app.Quit(0)
            except Exception as exc:
                logger.warning("Robot.Application.Quit raised: %s", exc)
            self.robot_app = None
            self.project = None
            self.structure = None
            self._connected = False
            self.connected_pid = None
            # [SEAT] Release only if we are the seat owner.
            if self._seat_owner_pid is not None:
                try:
                    from tools.robot_seat import release_seat
                    release_seat(self._seat_owner_pid)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("robot seat release failed: %s", exc)
                self._seat_owner_pid = None
            logger.info(
                "Robot COM server released (was pid=%s).", pid_before)

    def _ensure_connected(self):
        """
        [FIX H8] Checks connection health before proceeding.

        [FIX R1] Probes `.Project` — IRobotApplication has no `.Name`
        property, so the old probe raised AttributeError on every healthy
        connection and was misreported as "connection lost" (triggering an
        endless relaunch loop). Only COM transport errors are treated as a
        lost server; AttributeError is reported as a stale gen_py cache.
        """
        if not self._connected or self.robot_app is None:
            raise RuntimeError(
                "RobotBridge is not connected. Call connect() before "
                "issuing model-building commands."
            )
        # Lightweight liveness check — .Project is guaranteed to exist on
        # IRobotApplication and is the same readiness signal used in connect().
        try:
            _ = self.robot_app.Project
        except AttributeError as exc:
            raise RuntimeError(
                "Robot COM proxy is broken (AttributeError on basic property "
                "access). The win32com gen_py cache is likely stale. Fix: "
                "stop the app, delete the gen_py cache folder (locate it via: "
                "python -c \"import win32com; print(win32com.__gen_path__)\"), "
                f"then restart. Original error: {exc}"
            ) from exc
        except Exception as exc:
            self._connected = False
            raise RuntimeError(
                "Robot COM server connection lost (server may have been "
                "closed externally). Please reconnect by calling connect(). "
                f"Original error: {exc}"
            ) from exc

    @com_thread_safe
    def robot_session_status(self) -> Dict[str, Any]:
        """[DIAG] Surfaces the authoritative session picture: which robot.exe
        PID this bridge is connected to, HOW it connected (attach/launch),
        who owns the cross-process seat, and which robot processes are live.
        No COM probing beyond the already-known state — safe to call at any
        time. This is the FIRST tool an agent should call when Robot behaves
        oddly (stale bar ids 11+ after building N bars, RPC drops, phantom
        dialogs) — a split session shows up here immediately."""
        alive_now = _robot_pids()
        seat = {}
        try:
            from tools.robot_seat import seat_status
            seat = seat_status()
        except Exception as exc:  # noqa: BLE001
            logger.warning("robot_session_status: seat read failed: %s", exc)
            seat = {"error": str(exc)}
        connected_via = None
        if self._connected and self._seat_owner_pid is not None and seat \
                and seat.get("owner_pid") == self._seat_owner_pid:
            connected_via = seat.get("connected_via")
        return {
            "connected": bool(self._connected),
            "connected_pid": self.connected_pid,
            "connected_via": connected_via,
            "robots_running": sorted(alive_now),
            "this_process_pid": os.getpid(),
            "seat": {
                k: seat.get(k) for k in (
                    "present", "owner_pid", "owner_kind", "connected_via",
                    "robot_pids", "acquired_at", "owner_alive", "robots_alive",
                    "seat_available")},
            "summary": (
                f"Robot session pid {self.connected_pid or 'n/a'}, "
                f"connected via {connected_via or 'n/a'}, "
                f"seat owner {seat.get('owner_pid')} ({seat.get('owner_kind')}); "
                f"live robot.exe: {sorted(alive_now) or 'none'}"),
        }

    # ------------------------------------------------------------------ #
    # Project / model creation
    # ------------------------------------------------------------------ #

    def _guarded_project_new(self, code) -> None:
        """Calls Project.New(code) with an interactive-safe dialog guard.

        With Interactive=1 (the app's normal visible-Robot mode), Robot can
        pop its native "Do you want to save changes to Structure?" modal when
        New() discards a project that has results. That modal blocks on
        Robot's UI thread until clicked, which would freeze new_2d_frame /
        new_3d_frame / clear_structure with no way to recover.

        The guard starts a background watch_and_dismiss() thread BEFORE
        calling New() (so it is already polling when the modal appears, since
        New() blocks synchronously), auto-clicks known-safe buttons, and on an
        unrecognized dialog raises a clear actionable error instead of
        hanging or force-killing the user's visible Robot instance.
        """
        from tools.win_dialogs import SAVE_PROMPT_PATTERNS, watch_and_dismiss

        pids = ({self.connected_pid} if self.connected_pid else _robot_pids())
        result: Dict[str, Any] = {}
        watcher_done = threading.Event()

        def _run_watcher() -> None:
            try:
                result.update(watch_and_dismiss(
                    pids, SAVE_PROMPT_PATTERNS, timeout_s=15.0, poll_s=0.25,
                    on_unknown="wait"))
            except Exception as exc:  # noqa: BLE001
                result["outcome"] = "error"
                result["title"] = str(exc)
            finally:
                watcher_done.set()

        t = threading.Thread(target=_run_watcher, daemon=True,
                             name="robot-project-new-guard")
        t.start()
        try:
            self.project.New(code)
        finally:
            # Give the watcher a moment to settle; then join (it is bounded
            # by timeout_s so this never hangs the caller).
            watcher_done.wait(timeout=20.0)
            if t.is_alive():
                t.join(timeout=2.0)

        outcome = result.get("outcome")
        if outcome == "dismissed" or outcome == "dismissed_benign":
            logger.info(
                "Project.New guarded: auto-dismissed dialog %r (button=%r).",
                result.get("title"), result.get("button"))
        elif outcome == "unknown_seen":
            raise RuntimeError(
                "An unrecognized Robot dialog appeared during Project.New() "
                f"({result.get('title')!r}) and could not be auto-dismissed. "
                "Please click it in the visible Robot window (it may be a "
                "save-changes or license prompt) and retry. If this keeps "
                "happening, capture the dialog text and add a pattern to "
                "tools/win_dialogs.SAVE_PROMPT_PATTERNS.")
        elif outcome == "timeout":
            # Watcher saw no dialog (the common no-save case) OR saw one and
            # kept waiting - treat as clean, the call already succeeded.
            logger.info(
                "Project.New guarded: no blocking dialog (outcome=timeout, "
                "clean).")
        # outcome == "clean" / "error" - proceed (clean = no dialog seen).

    def _guarded_calculate(self, engine, timeout_s: float = 30.0) -> None:
        """Calls CalcEngine.Calculate() with an interactive-safe dialog guard.

        The solver can pop known dialogs during Calculate() in an interactive
        session - most notably the "Instability ... Do you want to continue?"
        modal (Interactive=1 does not suppress it). The guard reuses the SAME
        watch_and_dismiss() primitives and the batch runner's known patterns
        (instability + benign calculation messages), but NEVER force-kills on
        an unknown dialog - it raises a clear actionable error instead, since
        the visible Robot instance may be the user's own work.

        [FIX 2026-08-23] The interactive instability dialog is answered with
        "Yes" (continue), NOT "No" (abort). Live-verified: with Interactive=1
        Robot pops "Instability type 3 - continue?" for planar structures in
        3D (the batch path with Interactive=0 suppresses the dialog and the
        solver proceeds); answering "No" aborts the analysis and Calculate
        returns with ALL results silently zero. Answering "Yes" produces the
        correct statics (identical to the headless path). check_model_stability
        remains the pre-solve mechanism gate; the interactive continue matches
        what the batch solver actually does.
        """
        from batch.headless_driver import DEFAULT_DIALOG_PATTERNS
        from tools.win_dialogs import watch_and_dismiss

        # Interactive-specific override: instability = continue (Yes), so the
        # app path matches the headless solver (which suppresses this dialog
        # and proceeds). The batch runner keeps its own "No" pattern for
        # genuinely unstable CANDIDATES.
        patterns = dict(DEFAULT_DIALOG_PATTERNS)
        patterns["instabilit"] = {"action": "click", "button_text": "Yes"}

        pids = ({self.connected_pid} if self.connected_pid else _robot_pids())
        result: Dict[str, Any] = {}
        watcher_done = threading.Event()

        def _run_watcher() -> None:
            try:
                result.update(watch_and_dismiss(
                    pids, patterns, timeout_s=timeout_s,
                    poll_s=0.25, on_unknown="wait"))
            except Exception as exc:  # noqa: BLE001
                result["outcome"] = "error"
                result["title"] = str(exc)
            finally:
                watcher_done.set()

        t = threading.Thread(target=_run_watcher, daemon=True,
                             name="robot-calculate-guard")
        t.start()
        try:
            engine.Calculate()
        finally:
            watcher_done.wait(timeout=timeout_s + 10.0)
            if t.is_alive():
                t.join(timeout=2.0)

        outcome = result.get("outcome")
        if result.get("instability_seen"):
            # [INSTABILITY] The solver reported an instability and the dialog
            # was auto-answered 'Yes' (continue). Flag it LOUDLY: it is stored
            # on the bridge and surfaced in the solve tool result so the
            # LLM/user is FORCED to see that the model has a suspected
            # mechanism. (Uses the persistent instability_seen flag, not the
            # final outcome/matched keys: the benign Calculation Messages
            # dialog that follows OVERWRITES outcome/matched.)
            self._last_instability_warning = str(
                result.get("instability_title")
                or result.get("title")
                or "instability dialog")
            logger.warning(
                "Robot instability dialog auto-answered 'Yes' (continue) "
                "- solver reported instability %r; results may only be valid "
                "for the stable planes. Run check_model_stability if unsure.",
                self._last_instability_warning)
        elif outcome == "unknown_seen":
            raise RuntimeError(
                "An unrecognized Robot dialog appeared during Calculate() "
                f"({result.get('title')!r}) and could not be auto-dismissed. "
                "Please click it in the visible Robot window and retry. If "
                "this keeps happening, capture the dialog text and add a "
                "pattern to batch/headless_driver.DEFAULT_DIALOG_PATTERNS.")
        # dismissed_benign / timeout / clean / error -> proceed.

    @com_thread_safe
    def new_2d_frame(self) -> None:
        """Starts a new planar frame (Frame 2D) project."""
        self._ensure_connected()
        self.project = self.robot_app.Project
        self._guarded_project_new(RobotEnum.I_PT_BAR_2D)
        self._project_type = "2D"
        self.structure = self.project.Structure
        self._section_assignments.clear()
        self._node_coords.clear()
        self._bar_endpoints.clear()
        self.bracing.clear()
        self.connections.clear()
        logger.info("New 2D frame project created.")

    @com_thread_safe
    def new_3d_frame(self) -> None:
        """Starts a new spatial frame (Frame 3D) project."""
        self._ensure_connected()
        self.project = self.robot_app.Project
        self._guarded_project_new(RobotEnum.I_PT_BAR_3D)
        self._project_type = "3D"
        self.structure = self.project.Structure
        self._section_assignments.clear()
        self._node_coords.clear()
        self._bar_endpoints.clear()
        self.bracing.clear()
        self.connections.clear()
        logger.info("New 3D frame project created.")

    # ------------------------------------------------------------------ #
    # Geometry
    # ------------------------------------------------------------------ #

    @com_thread_safe
    def create_node(self, node_id: int, x: float, y: float = 0.0, z: float = 0.0) -> int:
        """
        Creates a structural node.

        Robot's native coordinate convention for Frame2D is (X, Z) in the
        vertical plane; for Frame3D it is full (X, Y, Z). We always pass
        the full triple to `Nodes.Create` -- Robot ignores the unused axis
        in 2D mode.
        """
        self._ensure_connected()
        self.structure.Nodes.Create(node_id, x, y, z)
        self._node_coords[node_id] = (x, y, z)
        return node_id

    @com_thread_safe
    def create_bar(
        self,
        bar_id: int,
        start_node: int,
        end_node: int,
        section_name: str = "HEA200",
    ) -> int:
        """Creates a bar element between two existing nodes and assigns a section label."""
        self._ensure_connected()
        self.structure.Bars.Create(bar_id, start_node, end_node)

        bar = self.structure.Bars.Get(bar_id)
        section_name = self._assign_bar_section(bar, bar_id, section_name)

        self._section_assignments[bar_id] = section_name
        self._bar_endpoints[bar_id] = (start_node, end_node)
        return bar_id

    def _assign_bar_section(self, bar, bar_id: int, section_name: str) -> str:
        """
        [PHASE 1 refactor] Shared section-assignment helper used by both
        create_bar and modify_bar_section: loads (or reuses) the catalog
        section label and assigns it to the given live bar object. Returns
        the canonical section name that was actually assigned.

        [LEAK-GUARD] section_name is validated BEFORE touching Robot, so a
        placeholder/label string (e.g. the notorious "IPE  chord" with a
        double space) is rejected here at the source with an actionable
        error instead of being poked into the live catalog.
        """
        section_name = self._validate_section_input(section_name)
        section_label = self._get_or_create_section_label(section_name)
        bar.SetLabel(RobotEnum.I_LT_BAR_SECTION, section_label.Name)
        return section_label.Name

    @com_thread_safe
    def modify_bar_section(self, bar_id: int, section_name: str) -> str:
        """
        [PHASE 1] Changes the section of an EXISTING bar without deleting or
        recreating it (keeps geometry, loads, and releases intact). Re-solve
        afterwards to refresh results.
        """
        self._ensure_connected()
        try:
            bar = self.structure.Bars.Get(bar_id)
        except Exception as exc:
            raise RuntimeError(
                f"Bar {bar_id} not found. Cannot modify its section. ({exc})"
            ) from exc

        assigned = self._assign_bar_section(bar, bar_id, section_name)
        self._section_assignments[bar_id] = assigned
        logger.info("Bar %s section changed to '%s'.", bar_id, assigned)
        return f"Bar {bar_id} section set to '{assigned}'."

    @staticmethod
    def _validate_section_input(section_name: str) -> str:
        """[LEAK-GUARD] Pure pre-flight check that rejects non-catalog
        strings at the SOURCE (before any Robot catalog poke) so a leaked
        placeholder/label like 'IPE  chord' never reaches the COM catalog
        lookup. Accepts real catalog styles:
          'IPE 300', 'HEA 200', 'IPE300', 'L 80x80x8', 'L 80X80X8',
          'W 12X26', 'UB 305x165x40'.
        Raises ValueError with list_available_sections guidance."""
        name = str(section_name or "").strip()
        if not name:
            raise ValueError(
                "Empty section name - pick a catalog name first via "
                "list_available_sections (e.g. 'IPE 300', 'HEA 200', "
                "'L 80x80x8').")
        collapsed = " ".join(name.split())
        if collapsed != name:
            raise ValueError(
                f"Section name {name!r} has stray/double whitespace - this "
                "looks like a leaked placeholder label instead of a real "
                "catalog name. Call list_available_sections and use an "
                "exact catalog name (e.g. 'IPE 300', 'L 80x80x8').")
        if any(ch in name for ch in "<>{}[]()\""):
            raise ValueError(
                f"Section name {name!r} contains placeholder punctuation. "
                "Use a real catalog name from list_available_sections.")
        tokens = collapsed.split()
        # First token: family code (letters). Remaining tokens: the SIZE,
        # which must contain at least one digit ('300', '80x80x8', '12X26').
        if len(tokens) == 1:
            if not re.search(r"\d", collapsed):
                raise ValueError(
                    f"Section name {name!r} is not a catalog section - "
                    "call list_available_sections for valid names.")
        else:
            for tok in tokens[1:]:
                if not re.search(r"\d", tok):
                    raise ValueError(
                        f"Section segment {tok!r} in {name!r} is not a size. "
                        "This looks like a label/placeholder leaked into a "
                        "section field - use an exact catalog name from "
                        "list_available_sections (e.g. 'IPE 300').")
        _BAD_LABEL_TOKENS = ("chord", "top", "bottom", "web", "beam",
                             "column", "member", "section", "default",
                             "auto", "use", "e.g", "plate", "group")
        low = f" {collapsed.lower()} "
        for bad in _BAD_LABEL_TOKENS:
            if f" {bad} " in low:
                raise ValueError(
                    f"Section name {name!r} contains label token {bad!r}- "
                    "this is a description placeholder, not a catalog name. "
                    "Call list_available_sections first.")
        return collapsed

    @staticmethod
    def _section_label_candidates(section_name: str) -> List[str]:
        """[FIX R5/R6] Name variants to try against the section catalogs:
        the name as given, a spaced variant ("IPE300" -> "IPE 300"), and
        upper-cased forms. Robot catalog names are "family + space + size"
        (e.g. "IPE 300"), but users/LLMs often write "IPE300".

        [ANGLE] For L-family sections the catalog also accepts several
        'x'-separator spellings ("L 80x80x8" vs "L 80X80X8" vs
        "L 80 x 80 x 8"), so all of those are generated too — the live
        catalog decides which one is real.
        """
        name = " ".join(str(section_name or "").split())
        spaced = re.sub(r"^([A-Za-z]+)(\d)", r"\1 \2", name)
        candidates = [name]
        for extra in (spaced, spaced.upper(), name.upper()):
            if extra not in candidates:
                candidates.append(extra)
        # Angle-family extra spellings ("L 80x80x8" / "L 80X80X8" /
        # "L 80 x 80 x 8" / "L80X80X8"): the live catalog may accept any.
        m = re.match(r"^L\s*(\d+)\s*[xX]\s*(\d+)\s*[xX]\s*(\d+)\s*$", name)
        if m:
            a_n, b_n, t_n = m.groups()
            for form in (
                    f"L {a_n}x{b_n}x{t_n}",
                    f"L {a_n}X{b_n}X{t_n}",
                    f"L {a_n} x {b_n} x {t_n}",
                    f"L{a_n}x{b_n}x{t_n}",
                    f"L{a_n}X{b_n}X{t_n}",
                    f"L {a_n}X{b_n}x{t_n}",
                    f"L {a_n}x{b_n}X{t_n}"):
                if form not in candidates:
                    candidates.append(form)
        return candidates

    def _ensure_section_catalog_active(self, db_code: str) -> None:
        """
        [FIX R6] LoadFromDBase(2) only searches section catalogs that are
        ACTIVE in the project preferences. Fresh Robot profiles activate
        only a US default set; this ensures the requested catalog (e.g.
        EURO, which holds IPE/HEA/HEB) is active before loading sections.
        """
        try:
            pref = self.robot_app.Project.Preferences
            active = pref.SectionsActive
            found = False
            try:
                for i in range(1, int(active.Count) + 1):
                    try:
                        if str(active.Get(i)).strip().upper() == db_code.upper():
                            found = True
                            break
                    except Exception:
                        continue
            except Exception:
                pass
            if not found:
                active.Add(db_code)
                logger.info("Section catalog '%s' activated.", db_code)
        except Exception as exc:
            logger.warning(
                "Could not ensure section catalog '%s' is active: %s", db_code, exc
            )

    def _get_or_create_section_label(self, section_name: str):
        """
        Fetches a bar-section label by catalog name, creating it and loading
        real catalog geometry if absent.

        [FIX R5/R6] Two bugs previously prevented sections from ever being
        defined: (a) the label type was 5 (bar offset) instead of 3 (bar
        section); (b) the required catalog was never activated in the
        project preferences, so catalog lookups silently failed and Robot
        fell back to a default profile. Both are fixed — sections now load
        via LoadFromDBase2(name, catalog) with real geometry, and a clear
        error is raised when a name is genuinely unknown.
        """
        labels = self.structure.Labels
        candidates = self._section_label_candidates(section_name)

        # 1) Reuse a label that has already been created/loaded.
        for cand in candidates:
            try:
                label = labels.Get(RobotEnum.I_LT_BAR_SECTION, cand)
                if label is not None:
                    return label
            except Exception:
                continue

        # 2) Create + load from the section catalogs.
        last_error: Optional[Exception] = None
        for cand in candidates:
            label = labels.Create(RobotEnum.I_LT_BAR_SECTION, cand)
            try:
                section_data = CastTo(label.Data, "IRobotBarSectionData")
            except Exception:
                section_data = label.Data

            loaded_from = None
            for db in RobotEnum.SECTION_DATABASES:
                self._ensure_section_catalog_active(db)
                try:
                    if section_data.LoadFromDBase2(cand, db):
                        loaded_from = db
                        break
                except Exception as exc:
                    last_error = exc

            if loaded_from is not None:
                labels.Store(label)
                logger.info(
                    "Section '%s' loaded from catalog '%s'.", cand, loaded_from
                )
                return label
            try:
                labels.Delete(RobotEnum.I_LT_BAR_SECTION, cand)
            except Exception:
                pass

        raise RuntimeError(
            f"Section '{section_name}' was not found in any Robot section "
            f"catalog (tried names: {candidates}; catalogs: "
            f"{RobotEnum.SECTION_DATABASES}). Use catalog-style names such "
            "as 'IPE 300', 'HEA 200', 'HEB 300', 'W 12X26', or "
            "'UB 305x165x40'."
            + (f" Last COM error: {last_error}." if last_error else "")
        )

    # ------------------------------------------------------------------ #
    # Supports
    # ------------------------------------------------------------------ #

    @com_thread_safe
    def set_support(
        self,
        node_id: int,
        support_type: str = "fixed",
        spring_stiffness: Optional[Dict[str, float]] = None,
    ) -> None:
        """
        Applies a support condition to a node.

        support_type: one of {"fixed", "pinned", "roller_x", "roller_z"}
        plus "spring" — an elastic-linear spring support requiring
        ``spring_stiffness``: a dict of DOF -> stiffness
        (UX/UY/UZ in kN/m, RX/RY/RZ in kNm/rad), e.g. {"UZ": 100000.0}.
        The fixed/pinned/roller_* behaviour is unchanged.
        """
        self._apply_node_support(node_id, support_type, spring_stiffness)

    # Fixity flag sets (1 = fixed, 0 = free) shared by set_support and
    # modify_support — UX, UY, UZ, RX, RY, RZ.
    _SUPPORT_FLAG_SETS = {
        "fixed":    dict(UX=1, UY=1, UZ=1, RX=1, RY=1, RZ=1),
        "pinned":   dict(UX=1, UY=1, UZ=1, RX=0, RY=0, RZ=0),
        "roller_x": dict(UX=0, UY=1, UZ=1, RX=0, RY=0, RZ=0),
        "roller_z": dict(UX=1, UY=0, UZ=1, RX=0, RY=0, RZ=0),
    }

    # DOF -> elastic-spring stiffness member on IRobotNodeSupportData
    # (verified against the RobotOM v27 type library: translations KX/KY/KZ
    # in kN/m, rotations HX/HY/HZ in kNm/rad, plus the ElasticLinear flag).
    _SPRING_VALUE_MEMBERS = {
        "UX": "KX", "UY": "KY", "UZ": "KZ",
        "RX": "HX", "RY": "HY", "RZ": "HZ",
    }

    def _apply_node_support(self, node_id: int, support_type: str,
                            spring_stiffness: Optional[Dict[str, float]] = None) -> None:
        """
        [PHASE 1 refactor] Shared support-application helper used by both
        set_support and modify_support: validates the type, creates/reuses
        the fixity label, and assigns it to the existing node (the node and
        any connected bars are left untouched). support_type="spring"
        delegates to _apply_spring_support (elastic-linear springs).
        """
        self._ensure_connected()
        support_type = str(support_type or "").lower()
        if support_type == "spring":
            self._apply_spring_support(node_id, spring_stiffness)
            return

        # [FIX M10] Validate support_type
        valid_supports = set(self._SUPPORT_FLAG_SETS.keys())
        if support_type not in valid_supports:
            raise ValueError(
                f"Invalid support_type '{support_type}'. Must be one of "
                f"{valid_supports} or 'spring'."
            )

        labels = self.structure.Labels
        label_name = f"AUTO_{support_type.upper()}"

        try:
            support_label = labels.Get(RobotEnum.I_LT_SUPPORT, label_name)
        except Exception:
            support_label = None

        if support_label is None:
            support_label = labels.Create(RobotEnum.I_LT_SUPPORT, label_name)

            # [FIX R5] I_LT_SUPPORT is 0 (verified live); the properties are
            # VT_I4, so integers are assigned rather than booleans.
            flag_sets = self._SUPPORT_FLAG_SETS[support_type]

            try:
                support_data = CastTo(support_label.Data, "IRobotNodeSupportData")
                for dof, value in flag_sets.items():
                    setattr(support_data, dof, value)
            except Exception as exc:
                logger.warning("Support DOF assignment fallback triggered: %s", exc)

            labels.Store(support_label)

        node = self.structure.Nodes.Get(node_id)
        node.SetLabel(RobotEnum.I_LT_SUPPORT, label_name)
        logger.info("Support '%s' applied to node %s.", support_type, node_id)

    def _apply_spring_support(self, node_id: int,
                              spring_stiffness: Optional[Dict[str, float]]) -> None:
        """Applies an elastic-linear (spring) support to a node via
        IRobotNodeSupportData.ElasticLinear + KX/KY/KZ / HX/HY/HZ
        (verified against the RobotOM v27 type library)."""
        stiffness = dict(spring_stiffness or {})
        if not stiffness:
            raise ValueError(
                "support_type='spring' requires a non-empty spring_stiffness "
                "dict, e.g. {'UZ': 100000.0} (translations UX/UY/UZ in kN/m, "
                "rotations RX/RY/RZ in kNm/rad).")
        unknown = sorted(set(stiffness) - set(self._SPRING_VALUE_MEMBERS))
        if unknown:
            raise ValueError(
                f"Unknown spring stiffness DOF(s) {unknown}; valid: "
                f"{sorted(self._SPRING_VALUE_MEMBERS)}")

        labels = self.structure.Labels
        label_name = "AUTO_SPRING"
        try:
            support_label = labels.Get(RobotEnum.I_LT_SUPPORT, label_name)
        except Exception:
            support_label = None

        if support_label is None:
            support_label = labels.Create(RobotEnum.I_LT_SUPPORT, label_name)
            try:
                support_data = CastTo(support_label.Data,
                                      "IRobotNodeSupportData")
            except Exception:
                support_data = support_label.Data
            try:
                # Elastic-linear support: springed DOFs are LEFT FREE
                # (fixity flag 0) and restrained ONLY by their stiffness
                # K*/H*. [FIX 2026-08-23] The previous code set fixity=1 on
                # every springed DOF, which Robot interprets as RIGID BLOCK
                # (stiffness ignored) - live result: a UZ:5000 spring
                # deflected 0.018mm (pure bar axial shortening) instead of
                # the hand-calc 2.017mm F/K. Non-springed DOFs stay free.
                support_data.ElasticLinear = 1
                for dof in ("UX", "UY", "UZ", "RX", "RY", "RZ"):
                    setattr(support_data, dof, 0)
                for dof, value in stiffness.items():
                    member = self._SPRING_VALUE_MEMBERS[dof]
                    # [FIX 2026-08-23] RobotOM stores KX/KY/KZ in N/m and
                    # HX/HY/HZ in Nm/rad. The PUBLIC contract of
                    # set_support(spring_stiffness=...) is kN/m and kNm/rad
                    # (tool schema + README). The previous code wrote the
                    # value verbatim, so a UZ:5000 "kN/m" spring became
                    # 5000 N/m = 5 kN/m -> 1000x too soft (live-verified:
                    # hand 2.017mm vs measured 2000.018mm). Scale x1000.
                    setattr(support_data, member, float(value) * 1000.0)
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(
                    "RobotOM on this build does not expose the elastic "
                    "support API (IRobotNodeSupportData.ElasticLinear / "
                    f"K*/H*): spring support failed: {exc}") from exc
            labels.Store(support_label)

        node = self.structure.Nodes.Get(node_id)
        node.SetLabel(RobotEnum.I_LT_SUPPORT, label_name)
        logger.info("Spring support applied to node %s: %s", node_id, stiffness)

    @com_thread_safe
    def modify_support(self, node_id: int, support_type: str,
                       spring_stiffness: Optional[Dict[str, float]] = None) -> str:
        """
        [PHASE 1] Changes the support condition of an EXISTING node without
        deleting it or any connected bars. Re-solve afterwards to refresh
        results. support_type="spring" additionally accepts
        ``spring_stiffness`` (same contract as set_support).
        """
        self._apply_node_support(node_id, support_type, spring_stiffness)
        return f"Node {node_id} support set to '{support_type}'."

    # --- stability (mechanism) pre-solve check --------------------------- #
    # [STEP 2] The SAME check HeadlessSession.validate_stability() runs
    # (headless_driver delegates here), exposed standalone for the chat
    # tool. Pure numpy, 2D Euler-Bernoulli frame assembly, rank check.

    _DOF_NAMES_2D = ("UX", "UZ", "RY")

    @staticmethod
    def _support_flags_2d(support_name: str) -> Tuple[int, int, int]:
        """(UX, UZ, RY) fixity flags for a support label name. Mirrors
        _SUPPORT_FLAG_SETS; unknown labels are treated as full fixity
        (conservative for mechanism detection)."""
        name = str(support_name or "").upper()
        for marker, flags in (("PINNED", (1, 1, 0)),
                              ("ROLLER", (0, 1, 0)),
                              ("FIXED", (1, 1, 1))):
            if marker in name:
                return flags
        return (1, 1, 1)

    def _section_a_i(self, section_name: str) -> Tuple[float, float]:
        """(A, I) for a section label via the empirical GetValue map
        (0=A, 4/5=I). Falls back to unit values; exact magnitudes do not
        affect singularity detection."""
        try:
            data = self.structure.Labels.Get(
                RobotEnum.I_LT_BAR_SECTION, str(section_name)).Data
            a = float(data.GetValue(0))
            i = min(float(data.GetValue(4)), float(data.GetValue(5)))
            if a > 0.0 and i > 0.0:
                return a, i
        except Exception:  # noqa: BLE001
            pass
        return 1.0, 1.0

    @staticmethod
    def _mechanism_check(coords, bars, fixity) -> Dict[str, Any]:
        """PURE (numpy only): 2D mechanism check shared by the live
        RobotBridge.validate_stability and the offline tests.

        coords : {node_id: (x, z)}
        bars   : [(n1, n2, A, I), ...]   (node ids; E cancels for rank)
        fixity : {node_id: (UX, UZ, RY)}  (1 = fixed)

        Returns the same dict shape the HeadlessSession check returns:
        {ok, mechanism, nodes, dofs, message} with the identical messages.
        """
        node_ids = sorted(coords)
        if not node_ids:
            return {"ok": True, "mechanism": False, "nodes": [], "dofs": [],
                    "message": "no nodes in model"}
        n = len(node_ids)
        idx = {nid: k for k, nid in enumerate(node_ids)}

        k = np.zeros((3 * n, 3 * n))
        for n1, n2, a, ii in bars:
            if n1 not in idx or n2 not in idx:
                continue
            (x1, z1), (x2, z2) = coords[n1], coords[n2]
            dx, dz = x2 - x1, z2 - z1
            length = float(np.hypot(dx, dz))
            if length < 1e-12:
                continue
            c, s = dx / length, dz / length
            ea, ei = 1.0 * a, 1.0 * ii
            k_local = np.array([
                [ea / length, 0, 0, -ea / length, 0, 0],
                [0, 12 * ei / length ** 3, 6 * ei / length ** 2,
                 0, -12 * ei / length ** 3, 6 * ei / length ** 2],
                [0, 6 * ei / length ** 2, 4 * ei / length,
                 0, -6 * ei / length ** 2, 2 * ei / length],
                [-ea / length, 0, 0, ea / length, 0, 0],
                [0, -12 * ei / length ** 3, -6 * ei / length ** 2,
                 0, 12 * ei / length ** 3, -6 * ei / length ** 2],
                [0, 6 * ei / length ** 2, 2 * ei / length,
                 0, -6 * ei / length ** 2, 4 * ei / length],
            ])
            t = np.array([
                [c, -s, 0, 0, 0, 0],
                [s, c, 0, 0, 0, 0],
                [0, 0, 1, 0, 0, 0],
                [0, 0, 0, c, -s, 0],
                [0, 0, 0, s, c, 0],
                [0, 0, 0, 0, 0, 1],
            ])
            k_e = t.T @ k_local @ t
            i1, i2 = idx[n1], idx[n2]
            dofs = [3 * i1, 3 * i1 + 1, 3 * i1 + 2,
                    3 * i2, 3 * i2 + 1, 3 * i2 + 2]
            for r, dr in enumerate(dofs):
                for c_, dc in enumerate(dofs):
                    k[dr, dc] += k_e[r, c_]

        free = []
        for nid in node_ids:
            fx, fz, ry = fixity.get(nid, (0, 0, 0))
            base = 3 * idx[nid]
            for j, locked in enumerate((fx, fz, ry)):
                if not locked:
                    free.append(base + j)
        if not free:
            return {"ok": True, "mechanism": False, "nodes": [], "dofs": [],
                    "message": "all DOFs are fixed by supports"}

        kf = k[np.ix_(free, free)]
        kf = (kf + kf.T) / 2.0
        rank = int(np.linalg.matrix_rank(kf, tol=1e-8))
        if rank == len(free):
            return {"ok": True, "mechanism": False, "nodes": [], "dofs": [],
                    "message": f"no mechanism detected (rank {rank}/{len(free)})"}

        # rank-deficient -> nodes whose DOFs participate in the nullspace
        _, _, vt = np.linalg.svd(kf)
        null = vt[-1] if len(vt) else np.zeros(len(free))
        mag = float(np.max(np.abs(null))) if len(null) else 0.0
        flagged: List[int] = []
        dofs: List[str] = []
        dof_names = ("UX", "UZ", "RY")
        for j, dof in enumerate(free):
            if mag > 0 and abs(null[j]) > 0.25 * mag:
                node = node_ids[dof // 3]
                dofs.append(f"node {node} {dof_names[dof % 3]}")
                if node not in flagged:
                    flagged.append(node)
        return {
            "ok": False, "mechanism": True, "nodes": flagged, "dofs": dofs[:8],
            "message": ("likely mechanism at " + (", ".join(dofs[:8]) or "?")
                        + " — refusing to call Calculate()."),
        }

    @com_thread_safe
    def validate_stability(self) -> Dict[str, Any]:
        """[STEP 2] Detects likely kinematic mechanisms BEFORE Calculate()
        is ever called, using the 2D Euler-Bernoulli frame rank check that
        HeadlessSession.validate_stability delegates to (single source of
        truth). Returns {ok, mechanism, nodes, dofs, message}."""
        self._ensure_connected()
        st = self.structure

        nodes_coll = st.Nodes.GetAll()
        n_count = int(nodes_coll.Count) if nodes_coll is not None else 0
        coords: Dict[int, Tuple[float, float]] = {}
        node_ids: List[int] = []
        for i in range(1, n_count + 1):
            try:
                obj = nodes_coll.Get(i)
                nid = int(obj.Number)
                node_ids.append(nid)
                coords[nid] = (float(obj.X), float(obj.Z))
            except Exception:  # noqa: BLE001
                continue
        if not node_ids:
            return {"ok": True, "mechanism": False, "nodes": [], "dofs": [],
                    "message": "no nodes in model"}

        fixity: Dict[int, Tuple[int, int, int]] = {}
        for nid in node_ids:
            try:
                node = st.Nodes.Get(nid)
                if bool(node.HasLabel(RobotEnum.I_LT_SUPPORT)):
                    fixity[nid] = self._support_flags_2d(
                        node.GetLabelName(RobotEnum.I_LT_SUPPORT))
                else:
                    fixity[nid] = (0, 0, 0)
            except Exception:  # noqa: BLE001
                fixity[nid] = (0, 0, 0)

        bars: List[Tuple[int, int, float, float]] = []
        bars_coll = st.Bars.GetAll()
        b_count = int(bars_coll.Count) if bars_coll is not None else 0
        for i in range(1, b_count + 1):
            try:
                bobj = bars_coll.Get(i)
                n1, n2 = int(bobj.StartNode), int(bobj.EndNode)
                if n1 not in coords or n2 not in coords:
                    continue
                sec = str(bobj.GetLabelName(RobotEnum.I_LT_BAR_SECTION))
                a, ii = self._section_a_i(sec)
                bars.append((n1, n2, a, ii))
            except Exception:  # noqa: BLE001
                continue

        return self._mechanism_check(coords, bars, fixity)

    # --- [PHASE1_RELEASE] ---
    @com_thread_safe
    def modify_bar_release(
        self,
        bar_id: int,
        start_ux: int = 0, start_uy: int = 0, start_uz: int = 0,
        start_rx: int = 0, start_ry: int = 0, start_rz: int = 0,
        end_ux: int = 0, end_uy: int = 0, end_uz: int = 0,
        end_rx: int = 0, end_ry: int = 0, end_rz: int = 0,
    ) -> str:
        """
        [PHASE 1] Sets end releases on an EXISTING bar (connection fixity).
        Each flag: 1 = released (hinged/free in that DOF), 0 = fixed
        (continuous). Defaults are fully fixed (rigid) both ends.

        Verified live against RobotOM v27: bar-release labels are label type
        I_LT_BAR_RELEASE (4); their Data casts to IRobotBarReleaseData whose
        StartNode/EndNode return IRobotBarEndReleaseData with int properties
        UX/UY/UZ/RX/RY/RZ.
        """
        self._ensure_connected()
        try:
            bar = self.structure.Bars.Get(bar_id)
        except Exception as exc:
            raise RuntimeError(
                f"Bar {bar_id} not found. Cannot modify its releases. ({exc})"
            ) from exc

        start_flags = dict(
            UX=start_ux, UY=start_uy, UZ=start_uz,
            RX=start_rx, RY=start_ry, RZ=start_rz,
        )
        end_flags = dict(
            UX=end_ux, UY=end_uy, UZ=end_uz,
            RX=end_rx, RY=end_ry, RZ=end_rz,
        )
        # Sanitize to 0/1 (accepts booleans or ints from the LLM).
        for flags in (start_flags, end_flags):
            for k, v in flags.items():
                flags[k] = 1 if v else 0

        label_name = (
            f"REL_S{start_flags['UX']}{start_flags['UY']}{start_flags['UZ']}"
            f"{start_flags['RX']}{start_flags['RY']}{start_flags['RZ']}"
            f"_E{end_flags['UX']}{end_flags['UY']}{end_flags['UZ']}"
            f"{end_flags['RX']}{end_flags['RY']}{end_flags['RZ']}"
        )

        labels = self.structure.Labels
        try:
            release_label = labels.Get(RobotEnum.I_LT_BAR_RELEASE, label_name)
        except Exception:
            release_label = None

        if release_label is None:
            release_label = labels.Create(RobotEnum.I_LT_BAR_RELEASE, label_name)
            try:
                data = CastTo(release_label.Data, "IRobotBarReleaseData")
                for sub, flags in ((data.StartNode, start_flags),
                                   (data.EndNode, end_flags)):
                    for dof, value in flags.items():
                        setattr(sub, dof, value)
            except Exception as exc:
                logger.warning("Bar release DOF assignment issue: %s", exc)
            labels.Store(release_label)

        bar.SetLabel(RobotEnum.I_LT_BAR_RELEASE, label_name)
        logger.info("Bar %s releases set to label '%s'.", bar_id, label_name)
        return f"Bar {bar_id} end releases set (label '{label_name}')."

    # ------------------------------------------------------------------ #
    # Loads
    # ------------------------------------------------------------------ #

    @com_thread_safe
    def create_load_case(
        self,
        case_id: int,
        case_name: str = "Dead Load",
        nature: int = RobotEnum.I_CN_PERMANENT,
        analysis_type: int = RobotEnum.I_CAT_STATIC_LINEAR,
    ) -> int:
        """Creates a simple static load case; returns the case id."""
        self._ensure_connected()
        cases = self.structure.Cases
        try:
            created = cases.CreateSimple(case_id, case_name, nature, analysis_type)
        except Exception as exc:
            logger.warning("CreateSimple raised for case %s: %s", case_id, exc)
            created = None

        # [FIX R4] CreateSimple returns None (and creates nothing) when the
        # nature / analysis_type combination is rejected — verify instead of
        # assuming success, and tolerate a pre-existing case with same id.
        if created is None:
            try:
                existing = cases.Get(case_id)
            except Exception:
                existing = None
            if existing is None:
                raise RuntimeError(
                    f"Robot refused to create load case {case_id} "
                    f"(name='{case_name}', nature={nature}, "
                    f"analysis_type={analysis_type}). If a case with this id "
                    "already exists with a different definition, use a new "
                    "case id."
                )
            logger.info("Using pre-existing load case %s.", case_id)

        # [H2 DIAGNOSTIC - temporary] Verify the case number Robot actually
        # assigned matches the requested case_id. If CreateSimple silently
        # renumbers, or the FIX-R4 fallback hit a T2 auto-created proxy,
        # apply_bar_load/solve/export all operate on the WRONG case -> zeros.
        try:
            actual = int(created.Number) if created is not None else None
        except Exception as exc:  # noqa: BLE001
            actual = '<err %s>' % exc
        if created is not None and actual != int(case_id):
            logger.error(
                'H2DIAG: CreateSimple requested case %s but Robot reported Number=%s'
                ' - load/export may target the wrong case!', case_id, actual)
        else:
            logger.info(
                'H2DIAG: case %s Number readback = %s (requested %s).',
                case_id, actual, case_id)
        try:
            logger.info('H2DIAG: cases.Exist(%s) = %s', case_id,
                        bool(cases.Exist(int(case_id))))
        except Exception as exc:  # noqa: BLE001
            logger.warning('H2DIAG: cases.Exist probe failed: %s', exc)
        try:
            coll = cases.GetAll()
            n = int(coll.Count) if coll is not None else 0
            nums = []
            for k in range(1, n + 1):
                try:
                    nums.append(int(coll.Get(k).Number))
                except Exception:
                    continue
            logger.info('H2DIAG: existing case numbers: %s', nums)
        except Exception as exc:  # noqa: BLE001
            logger.warning('H2DIAG: case enumeration failed: %s', exc)

        return case_id

    def _coincident_node_pairs(self) -> List[Tuple[int, int]]:
        """[LIVE-FIX 2026-08-23] Distinct nodes sharing the same coordinate.

        Live evidence (A/B on identical models, sum(FZ) vs applied load):
          * models WITHOUT coincident nodes: bar-uniform load records are
            exact (0.00%) — single bars, flat planar trusses, twin flat
            braced trusses, twin arch with the arch elevated 0.5 m.
          * models WITH coincident nodes (arch springing node placed at the
            same coordinate as the deck-end node — what create_arch_truss
            and the compose twin-arch produce): Robot's solver silently
            drops part of the uniform-load contribution (6.9% single-plane
            arch, 15.7% twin-arch self-weight, 20.0% twin-arch deck UDL),
            while nodal loads on the SAME model are exact (0.00%).
        The dropped portion is the load on the bars incident to the
        coincident end nodes. Pure helper over the bookkeeping coordinates.
        """
        seen: Dict[Tuple[float, float, float], int] = {}
        pairs: List[Tuple[int, int]] = []
        for nid, (x, y, z) in self._node_coords.items():
            key = (round(float(x), 6), round(float(y), 6), round(float(z), 6))
            if key in seen:
                pairs.append((seen[key], int(nid)))
            else:
                seen[key] = int(nid)
        return pairs

    @com_thread_safe
    def apply_bar_load(
        self,
        bar_id: int,
        case_id: int,
        value_kn_m: float,
        direction: str = "Z",
        force_record: bool = False,
    ) -> Dict[str, Any]:
        """Applies a uniformly distributed load (kN/m) along a bar.

        [LIVE-FIX 2026-08-23] If the model contains COINCIDENT-but-distinct
        nodes (e.g. an arch springing node sharing the deck-end node's
        coordinate), Robot's solver silently under-transfers bar-uniform
        load records to reactions (verified 6.9-20% live, see
        _coincident_node_pairs). In that case this method applies the
        STATICALLY EQUIVALENT nodal loads instead (q*L split 50/50 to the
        bar's two end nodes — exact equilibrium, verified 0.00%) and returns
        a warning. Pass force_record=True to insist on the raw uniform
        record anyway (member-level UDL distribution for beam design),
        accepting the equilibrium risk. Models without coincident nodes
        always use the record path, which is live-verified exact there.
        """
        self._ensure_connected()
        # [FIX M10] Validate direction
        valid_directions = {"X", "Y", "Z"}
        if direction.upper() not in valid_directions:
            raise ValueError(
                f"Invalid direction '{direction}'. Must be one of {valid_directions}."
            )
        direction = direction.upper()

        coincident = self._coincident_node_pairs()
        if coincident and not force_record:
            return self._apply_uniform_as_nodal(
                int(bar_id), int(case_id), float(value_kn_m), direction,
                len(coincident))

        case = CastTo(self.structure.Cases.Get(case_id), "IRobotSimpleCase")

        # [FIX R7] Value indices from IRobotBarUniformRecordValues (verified
        # against the type library): I_BURV_PX=0, I_BURV_PY=1, I_BURV_PZ=2.
        # The old code wrote a bogus "distribution type" into index 0 (which
        # is PX!) and used axis_index 3 for Z — a projection slot — so the
        # load value silently never reached PZ and results were all zeros.
        # Record type 4 (I_LRT_BAR_UNIFORM) already encodes "uniform", so no
        # distribution flag is needed.
        axis_map = {"X": 0, "Y": 1, "Z": 2}   # I_BURV_PX / PY / PZ
        axis_index = axis_map[direction]

        # [FIX R4] Records.Create(type) returns the IRobotLoadRecord object;
        # the target bar is assigned via record.Objects.FromText(...).
        record = case.Records.Create(RobotEnum.I_LRT_BAR_UNIFORM)
        record.SetValue(axis_index, value_kn_m)
        record.Objects.FromText(str(bar_id))

        logger.info(
            "Applied %.2f kN/m (dir=%s) to bar %s in case %s.",
            value_kn_m, direction, bar_id, case_id,
        )
        result: Dict[str, Any] = {"method": "bar_uniform_record"}
        if coincident and force_record:
            result["warning"] = (
                "force_record=True on a coincident-node model: Robot "
                "SILENTLY UNDER-TRANSFERS bar-uniform records here "
                "(live-verified). Reactions will NOT balance the applied "
                "load; verify any results carefully."
            )
        return result

    def _apply_uniform_as_nodal(self, bar_id: int, case_id: int,
                                value_kn_m: float, direction: str,
                                n_coincident: int) -> Dict[str, Any]:
        """Nodal-lumped equivalent of a bar UDL: q*L split 50/50 onto the
        bar's two end nodes. Exact equilibrium on every topology (the
        verified-safe path for coincident-node models)."""
        length = self._bar_length(bar_id)
        total = value_kn_m * length
        ends = self._bar_endpoints.get(bar_id, (None, None))
        if length > 0.0 and ends[0] is not None:
            n1, n2 = int(ends[0]), int(ends[1])
            half = total / 2.0
            if direction == "X":
                self.apply_nodal_load(n1, case_id, fx_kn=half)
                self.apply_nodal_load(n2, case_id, fx_kn=half)
            elif direction == "Y":
                self.apply_nodal_load(n1, case_id, fy_kn=half)
                self.apply_nodal_load(n2, case_id, fy_kn=half)
            else:
                self.apply_nodal_load(n1, case_id, fz_kn=half)
                self.apply_nodal_load(n2, case_id, fz_kn=half)
            lumped_to = [n1, n2]
        else:
            lumped_to = []
        logger.warning(
            "apply_bar_load: coincident-node model (%d pair(s)) -> "
            "nodal-lumped equivalent for bar %s (%.3f kN, dir %s).",
            n_coincident, bar_id, abs(total), direction)
        return {
            "method": "nodal_lumped",
            "equivalent_total_kn": round(abs(total), 4),
            "lumped_to": lumped_to,
            "warning": (
                f"Model has {n_coincident} coincident node pair(s) (e.g. "
                "arch springing on the deck-end coordinate). Robot's "
                "solver SILENTLY UNDER-TRANSFERS bar-uniform records on "
                "such models (live-verified 6.9-20% reaction shortfall); "
                "the statically equivalent nodal loads (q*L/2 per end "
                "node) were applied instead - exact equilibrium. Use "
                "force_record=True only if the true member-level UDL "
                "distribution is required and the risk accepted."
            ),
        }

    @com_thread_safe
    def apply_nodal_load(
        self,
        node_id: int,
        case_id: int,
        fx_kn: float = 0.0,
        fz_kn: float = 0.0,
        my_knm: float = 0.0,
        fy_kn: float = 0.0,
    ) -> None:
        """Applies a concentrated nodal force/moment in a given case."""
        self._ensure_connected()
        case = CastTo(self.structure.Cases.Get(case_id), "IRobotSimpleCase")

        # [FIX R4/R7] Same corrected record pattern as apply_bar_load:
        # Records.Create() returns the record object; the target node is
        # assigned via record.Objects.FromText(...). Value indices follow
        # IRobotNodeForceRecordValues: FX=0, FY=1, FZ=2, CX=3, CY=4, CZ=5.
        record = case.Records.Create(RobotEnum.I_LRT_NODE_FORCE)
        record.SetValue(0, fx_kn)     # I_NFRV_FX
        record.SetValue(1, fy_kn)     # I_NFRV_FY (out-of-plane, 3D only)
        record.SetValue(2, fz_kn)     # I_NFRV_FZ
        record.SetValue(3, 0.0)       # I_NFRV_CX (moment about X)
        record.SetValue(4, my_knm)    # I_NFRV_CY (moment about Y == MY)
        record.SetValue(5, 0.0)       # I_NFRV_CZ
        record.Objects.FromText(str(node_id))
        logger.info(
            "Applied nodal load FX=%.2f FY=%.2f FZ=%.2f MY=%.2f to node %s (case %s).",
            fx_kn, fy_kn, fz_kn, my_knm, node_id, case_id,
        )

    # ------------------------------------------------------------------ #
    # Solve
    # ------------------------------------------------------------------ #

    @com_thread_safe
    def solve(self, timeout_s: int = 120) -> bool:
        """
        Triggers the FEA solver and blocks until Robot reports completion.

        [FIX R4] IRobotStructure has no `Calc` member in RobotOM v27 - the
        calculation engine hangs off IRobotProject.CalcEngine (verified
        live). `Calculate()` is synchronous and IRobotCalcEngine exposes no
        CalcInProgress property, so the old (nonexistent-API) polling loop
        was removed. `timeout_s` is retained for API compatibility.

        [OBS] Calculate() runs under an interactive-safe dialog guard that
        auto-dismisses known dialogs (instability, calculation messages) but
        NEVER force-kills Robot on an unrecognized one.
        """
        self._ensure_connected()
        engine = self.robot_app.Project.CalcEngine
        try:
            engine.UseStatusWindow = False
        except Exception:
            pass  # optional nicety; harmless if the version rejects it

        start = time.time()
        self._guarded_calculate(engine, timeout_s=min(float(timeout_s), 60.0))
        logger.info("Robot solver run completed in %.1fs.", time.time() - start)
        return True

    @com_thread_safe
    def export_all_member_forces(
        self,
        case_id: int = 1,
        divisions: int = 5,
    ) -> pd.DataFrame:
        """
        Extracts internal forces (FX, FZ, MY) at `divisions` equally spaced
        stations along every bar in the model, for the given load case.

        Returns
        -------
        pd.DataFrame with columns [Bar_ID, Position_m, FX_kN, FZ_kN, MY_kNm]
        """
        self._ensure_connected()
        # [FIX M11] Clamp divisions to a safe upper bound
        divisions = max(1, min(divisions, 100))

        rows: List[Dict[str, Any]] = []

        bars = self.structure.Bars
        # [FIX R4] IRobotBarServer has no Count property in RobotOM v27 —
        # enumerate via GetAll(), which returns an IRobotCollection with
        # Count + Get (verified live).
        bar_collection = bars.GetAll()
        bar_count = int(bar_collection.Count) if bar_collection is not None else 0
        server = self.structure.Results.Bars.Forces

        # [FIX H2] Enumerate actual bar IDs instead of assuming sequential
        bar_ids = self._enumerate_bar_ids(bars, bar_count)

        for bar_id in bar_ids:
            try:
                bar_obj = self.structure.Bars.Get(bar_id)
                bar_length = self._bar_length(bar_id)
            except Exception:
                continue

            for d in range(divisions + 1):
                ratio = d / divisions
                try:
                    # [FIX R7] IRobotBarForceServer.Value signature is
                    # (bar_num, case_num, point) — case SECOND, relative
                    # position (0..1) THIRD. The previous (bar, ratio, case)
                    # order silently returned zeros for every station.
                    force = server.Value(bar_id, case_id, ratio)
                    fx, fy, fz = force.FX, force.FY, force.FZ
                    mx, my, mz = force.MX, force.MY, force.MZ
                except Exception as exc:
                    logger.warning(
                        "Force extraction failed for bar %s @ %.2f: %s",
                        bar_id, ratio, exc,
                    )
                    fx = fy = fz = mx = my = mz = float("nan")

                rows.append(
                    {
                        "Bar_ID": bar_id,
                        "Position_m": round(ratio * bar_length, 4),
                        "FX_kN": round(fx, 3) if not math.isnan(fx) else fx,
                        "FY_kN": round(fy, 3) if not math.isnan(fy) else fy,
                        "FZ_kN": round(fz, 3) if not math.isnan(fz) else fz,
                        "MX_kNm": round(mx, 3) if not math.isnan(mx) else mx,
                        "MY_kNm": round(my, 3) if not math.isnan(my) else my,
                        "MZ_kNm": round(mz, 3) if not math.isnan(mz) else mz,
                    }
                )

        df = pd.DataFrame(rows, columns=[
            "Bar_ID", "Position_m", "FX_kN", "FY_kN", "FZ_kN",
            "MX_kNm", "MY_kNm", "MZ_kNm",
        ])
        return df

    @com_thread_safe
    def export_reactions(self, case_id: int = 1) -> pd.DataFrame:
        """
        Extracts support reactions for every supported node in the given case.

        Returns
        -------
        pd.DataFrame with columns [Node_ID, Support_Type, FX_kN, FZ_kN, MY_kNm]
        """
        self._ensure_connected()
        rows: List[Dict[str, Any]] = []

        nodes = self.structure.Nodes
        # [FIX R4] IRobotNodeServer has no Count property in RobotOM v27 —
        # enumerate via GetAll() (IRobotCollection), same as bars.
        node_collection = nodes.GetAll()
        node_count = int(node_collection.Count) if node_collection is not None else 0
        reactions_server = self.structure.Results.Nodes.Reactions

        # [FIX H2] Enumerate actual node IDs instead of assuming sequential
        node_ids = self._enumerate_node_ids(nodes, node_count)

        for node_id in node_ids:
            try:
                node = nodes.Get(node_id)
            except Exception:
                continue

            has_support = False
            support_type = "-"
            try:
                lbl = node.GetLabel(RobotEnum.I_LT_SUPPORT)
                if lbl is not None and lbl.Name:
                    has_support = True
                    support_type = lbl.Name.replace("AUTO_", "").title()
            except Exception:
                has_support = False

            if not has_support:
                continue

            try:
                reaction = reactions_server.Value(node_id, case_id)
                fx = reaction.FX
                fz = reaction.FZ
                my = reaction.MY
            except Exception as exc:
                logger.warning("Reaction extraction failed for node %s: %s", node_id, exc)
                fx = fz = my = float("nan")

            rows.append(
                {
                    "Node_ID": node_id,
                    "Support_Type": support_type,
                    "FX_kN": round(fx, 3) if not math.isnan(fx) else fx,
                    "FZ_kN": round(fz, 3) if not math.isnan(fz) else fz,
                    "MY_kNm": round(my, 3) if not math.isnan(my) else my,
                }
            )

        df = pd.DataFrame(rows, columns=["Node_ID", "Support_Type", "FX_kN", "FZ_kN", "MY_kNm"])
        return df

    @com_thread_safe
    def export_bill_of_materials(
        self,
        density_kg_m3: float = 7850.0,
        unit_mass_lookup: Optional[Dict[str, float]] = None,
    ) -> pd.DataFrame:
        """
        Computes total steel weight per section type using member lengths.

        Parameters
        ----------
        density_kg_m3 : fallback density if a section's unit mass (kg/m) is
            not available from the Robot section database.
        unit_mass_lookup : optional dict of {section_name: kg/m} to override
            or supply values when Robot's catalog lookup is unavailable
            (useful for offline / headless section catalogs).

        Returns
        -------
        pd.DataFrame with columns
            [Section, Count, Total_Length_m, Unit_Mass_kg_m, Total_Weight_kg]
        """
        self._ensure_connected()
        unit_mass_lookup = unit_mass_lookup or {}

        section_lengths: Dict[str, float] = {}
        section_counts: Dict[str, int] = {}

        for bar_id, section_name in self._section_assignments.items():
            length_m = self._bar_length(bar_id)
            section_lengths[section_name] = section_lengths.get(section_name, 0.0) + length_m
            section_counts[section_name] = section_counts.get(section_name, 0) + 1

        rows = []
        for section_name, total_length in section_lengths.items():
            unit_mass = unit_mass_lookup.get(section_name)
            if unit_mass is None:
                unit_mass = self._lookup_unit_mass(section_name, density_kg_m3)

            total_weight = unit_mass * total_length
            rows.append(
                {
                    "Section": section_name,
                    "Count": section_counts[section_name],
                    "Total_Length_m": round(total_length, 3),
                    "Unit_Mass_kg_m": round(unit_mass, 2),
                    "Total_Weight_kg": round(total_weight, 2),
                }
            )

        df = pd.DataFrame(
            rows,
            columns=["Section", "Count", "Total_Length_m", "Unit_Mass_kg_m", "Total_Weight_kg"],
        )
        return df

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    @com_thread_safe
    def export_node_displacements(self, case_id: int = 1) -> pd.DataFrame:
        """
        [WP6] Extracts nodal displacements (UX..UZ in meters, RX..RZ in
        radians) for every node in the given case via
        Results.Nodes.Displacements.Value(node, case).
        """
        self._ensure_connected()
        rows: List[Dict[str, Any]] = []
        server = self.structure.Results.Nodes.Displacements

        nodes = self.structure.Nodes
        node_coll = nodes.GetAll()
        node_count = int(node_coll.Count) if node_coll is not None else 0
        node_ids = self._enumerate_node_ids(nodes, node_count)

        for node_id in node_ids:
            try:
                node = nodes.Get(node_id)
                # Coordinates live on the IRobotNode interface (the base
                # object is IRobotDataObject without X/Y/Z).
                try:
                    geo = CastTo(node, "IRobotNode")
                    x, y, z = float(geo.X), float(geo.Y), float(geo.Z)
                except Exception:
                    x = y = z = float("nan")
                disp = server.Value(node_id, case_id)
                # [WP6] Verified empirically (UDL + point-load tests): Robot
                # returns translation components scaled 1e-3 vs meters (i.e.
                # ~mm expressed in m), while rotations are already in rad.
                # Convert translations to meters (x1000).
                ux, uy, uz = disp.UX * 1000.0, disp.UY * 1000.0, disp.UZ * 1000.0
                rx, ry, rz = disp.RX, disp.RY, disp.RZ
            except Exception as exc:
                logger.warning("Displacement extraction failed for node %s: %s",
                               node_id, exc)
                continue
            rows.append({
                "Node_ID": node_id,
                "X_m": round(x, 4), "Y_m": round(y, 4), "Z_m": round(z, 4),
                "UX_m": round(ux, 6), "UY_m": round(uy, 6), "UZ_m": round(uz, 6),
                "RX_rad": round(rx, 8), "RY_rad": round(ry, 8), "RZ_rad": round(rz, 8),
            })
        return pd.DataFrame(rows, columns=[
            "Node_ID", "X_m", "Y_m", "Z_m",
            "UX_m", "UY_m", "UZ_m", "RX_rad", "RY_rad", "RZ_rad",
        ])

    @com_thread_safe
    def export_bar_stresses(self, case_id: int = 1, divisions: int = 5) -> pd.DataFrame:
        """
        [WP6] Extracts bar stresses at `divisions` stations along every bar
        via Results.Bars.Stresses.Value(bar, case, pos). Columns are stress
        components in MPa (verified live against Robot's units):
          FXSX    = axial stress from normal force
          Smax/Smin   = extreme combined normal stress (signed)
          SmaxMY/SmaxMZ, SminMY/SminMZ = bending stress from MY / MZ
          ShearY/ShearZ = shear stresses
          Torsion   = torsional stress
        """
        self._ensure_connected()
        divisions = max(1, min(divisions, 100))
        rows: List[Dict[str, Any]] = []
        bars = self.structure.Bars
        bar_coll = bars.GetAll()
        bar_count = int(bar_coll.Count) if bar_coll is not None else 0
        server = self.structure.Results.Bars.Stresses
        bar_ids = self._enumerate_bar_ids(bars, bar_count)

        for bar_id in bar_ids:
            try:
                bar_length = self._bar_length(bar_id)
            except Exception:
                continue
            for d in range(divisions + 1):
                ratio = d / divisions
                try:
                    s = server.Value(bar_id, case_id, ratio)
                    # [WP6] Verified empirically: stress components come back
                    # in kPa (Pa x 1e-3); divide by 1000 to report MPa.
                    row = {
                        "Bar_ID": bar_id,
                        "Position_m": round(ratio * bar_length, 4),
                        "FXSX_MPa": round(s.FXSX * 1e-3, 4),
                        "Smax_MPa": round(s.Smax * 1e-3, 4),
                        "Smin_MPa": round(s.Smin * 1e-3, 4),
                        "SmaxMY_MPa": round(s.SmaxMY * 1e-3, 4),
                        "SmaxMZ_MPa": round(s.SmaxMZ * 1e-3, 4),
                        "SminMY_MPa": round(s.SminMY * 1e-3, 4),
                        "SminMZ_MPa": round(s.SminMZ * 1e-3, 4),
                        "ShearY_MPa": round(s.ShearY * 1e-3, 4),
                        "ShearZ_MPa": round(s.ShearZ * 1e-3, 4),
                        "Torsion_MPa": round(s.Torsion * 1e-3, 4),
                    }
                    rows.append(row)
                except Exception as exc:
                    logger.warning("Stress extraction failed for bar %s @ %.2f: %s",
                                   bar_id, ratio, exc)
        return pd.DataFrame(rows, columns=[
            "Bar_ID", "Position_m", "FXSX_MPa", "Smax_MPa", "Smin_MPa",
            "SmaxMY_MPa", "SmaxMZ_MPa", "SminMY_MPa", "SminMZ_MPa",
            "ShearY_MPa", "ShearZ_MPa", "Torsion_MPa",
        ])

    # ------------------------------------------------------------------ #
    # P4: utilization ratios (analytical code check)
    # ------------------------------------------------------------------ #
    # VERIFIED PROBE FINDING (live, Robot SA 2027 / RobotOM v27): RobotOM
    # exposes NO steel/RC member code-check server at all — no utilization
    # interface exists in the type library or on Results/Project/CalcEngine
    # (only RC-only Project.DimServer). get_utilization_ratios therefore
    # computes an ANALYTICAL elastic check: Robot's own solved bar stresses
    # (Results.Bars.Stresses, already unit-corrected to MPa — WP6) divided
    # by the material design strength RE (verified: catalog 'STEEL' carries
    # RE=235e6 Pa = fy of S235; custom materials must set RE, e.g. via
    # set_material(..., fy_mpa=...) — bars without strength data return an
    # explicit NOT_CHECKABLE row instead of a silent number).

    _SQRT3 = 1.7320508075688772

    def _bar_strength_mpa(self, bar_obj) -> Tuple[Optional[float], str, str]:
        """[P4] Returns (fy_MPa or None, material_name, reason) for a bar.

        Lookup order: the bar's own material label (RE), then the material
        referenced by the bar's section label (MaterialName -> RE).
        """
        labels = self.structure.Labels

        def _re_of_label(mat_name: str) -> Optional[float]:
            try:
                data = labels.Get(RobotEnum.I_LT_MATERIAL, mat_name).Data
                re_pa = float(data.RE or 0.0)
                return re_pa / 1e6 if re_pa > 0.0 else None
            except Exception:
                return None

        mat_name = ""
        try:
            if bar_obj.HasLabel(RobotEnum.I_LT_MATERIAL):
                mat_name = str(bar_obj.GetLabelName(RobotEnum.I_LT_MATERIAL))
        except Exception:
            mat_name = ""
        if mat_name:
            fy = _re_of_label(mat_name)
            if fy is not None:
                return fy, mat_name, ""
            return None, mat_name, (
                f"material '{mat_name}' has no design strength (RE=0); "
                "re-apply with set_material(..., fy_mpa=...)")

        # Fall back to the section's material reference.
        try:
            sec_name = str(bar_obj.GetLabelName(RobotEnum.I_LT_BAR_SECTION))
            sdata = CastTo(
                labels.Get(RobotEnum.I_LT_BAR_SECTION, sec_name).Data,
                "IRobotBarSectionData")
            sec_mat = str(sdata.MaterialName or "")
            if sec_mat:
                fy = _re_of_label(sec_mat)
                if fy is not None:
                    return fy, sec_mat, ""
                return None, sec_mat, (
                    f"material '{sec_mat}' (from section) has RE=0")
        except Exception:
            pass
        return None, "", "no material label or section material found"

    @com_thread_safe
    def get_utilization_ratios(
        self,
        case_id: int = 1,
        bar_ids: Optional[List[int]] = None,
        divisions: int = 5,
    ) -> pd.DataFrame:
        """
        [P4] Analytical utilization ratios per bar for a solved case.

        Ratios (dimensionless, >1.0 = FAIL):
          combined_normal = max|Smax,Smin| / fy  (axial + biaxial bending
            at the extreme fiber — Robot's combined extreme stress)
          axial           = |FXSX| / fy
          shear_y/z       = |ShearY| / |ShearZ| divided by fy/sqrt(3)
          torsion         = |Torsion| / (fy/sqrt(3))
        The reported Utilization is the governing (max) ratio over all
        stations, with Governing_Check naming it. NOT a Robot design-module
        result (none exists in RobotOM v27 — verified live); it is an
        elastic first-yield check using Robot's own solved stresses and the
        material design strength RE. Bars whose material carries no RE
        (custom materials without fy) get an explicit NOT_CHECKABLE row.
        """
        self._ensure_connected()
        divisions = max(1, min(divisions, 50))
        bars = self.structure.Bars
        coll = bars.GetAll()
        count = int(coll.Count) if coll is not None else 0
        all_ids = self._enumerate_bar_ids(bars, count)
        ids = list(bar_ids) if bar_ids else all_ids
        stress_srv = self.structure.Results.Bars.Stresses

        rows: List[Dict[str, Any]] = []
        for bar_id in ids:
            if bar_id not in all_ids:
                rows.append({"Bar_ID": bar_id, "Status": "NOT_IN_MODEL"})
                continue
            bar_obj = bars.Get(bar_id)
            try:
                sec_name = str(bar_obj.GetLabelName(RobotEnum.I_LT_BAR_SECTION))
            except Exception:
                sec_name = ""
            fy, mat_name, reason = self._bar_strength_mpa(bar_obj)
            if fy is None or fy <= 0.0:
                rows.append({
                    "Bar_ID": bar_id, "Section": sec_name,
                    "Material": mat_name, "fy_MPa": None,
                    "Utilization": None, "Governing_Check": "N/A",
                    "Combined_Normal": None, "Axial": None,
                    "Shear_Y": None, "Shear_Z": None, "Torsion": None,
                    "Status": "NOT_CHECKABLE", "Reason": reason,
                })
                continue

            comp = {k: 0.0 for k in
                    ("combined", "axial", "shear_y", "shear_z", "torsion")}
            # Always include the midspan station: divisions=5 (default)
            # samples 0,.2,.4,.6,.8,1 and would miss the peak moment.
            stations = sorted({round(d / divisions, 6) for d in
                               range(divisions + 1)} | {0.5})
            for ratio_pos in stations:
                try:
                    s = stress_srv.Value(bar_id, case_id, ratio_pos)
                    comp["combined"] = max(
                        comp["combined"],
                        max(abs(s.Smax), abs(s.Smin)) * 1e-3 / fy)
                    comp["axial"] = max(
                        comp["axial"], abs(s.FXSX) * 1e-3 / fy)
                    comp["shear_y"] = max(
                        comp["shear_y"],
                        abs(s.ShearY) * 1e-3 / (fy / self._SQRT3))
                    comp["shear_z"] = max(
                        comp["shear_z"],
                        abs(s.ShearZ) * 1e-3 / (fy / self._SQRT3))
                    comp["torsion"] = max(
                        comp["torsion"],
                        abs(s.Torsion) * 1e-3 / (fy / self._SQRT3))
                except Exception:
                    continue
            names = {"combined": "combined_normal", "axial": "axial",
                     "shear_y": "shear_y", "shear_z": "shear_z",
                     "torsion": "torsion"}
            gov_name, util = "N/A", 0.0
            for k, v in comp.items():
                if v > util:
                    util, gov_name = v, names[k]
            rows.append({
                "Bar_ID": bar_id, "Section": sec_name,
                "Material": mat_name, "fy_MPa": round(fy, 1),
                "Utilization": round(util, 4),
                "Governing_Check": gov_name,
                "Combined_Normal": round(comp["combined"], 4),
                "Axial": round(comp["axial"], 4),
                "Shear_Y": round(comp["shear_y"], 4),
                "Shear_Z": round(comp["shear_z"], 4),
                "Torsion": round(comp["torsion"], 4),
                "Status": "PASS" if util <= 1.0 else "FAIL",
                "Reason": "",
            })
        return pd.DataFrame(rows, columns=[
            "Bar_ID", "Section", "Material", "fy_MPa", "Utilization",
            "Governing_Check", "Combined_Normal", "Axial", "Shear_Y",
            "Shear_Z", "Torsion", "Status", "Reason",
        ])

    # ------------------------------------------------------------------ #
    # P5: load combinations (first-class objects)
    # ------------------------------------------------------------------ #
    # VERIFIED PROBE RECIPE (live): combinations are created with
    # Cases.CreateCombination(num, name, I_CBT_*, nature, I_CAT_COMB=0).
    # The analize param MUST be 0 (I_CAT_COMB) — passing STATIC_LINEAR(1)
    # silently creates a broken case that solves to zero. The method
    # returns None (marshal quirk) — always fetch via Cases.Get(num) and
    # CastTo('IRobotCaseCombination'). Factors: CaseFactors.New(case_id,
    # factor); read-back via Get(i).CaseNumber/.Factor (i = 1..Count).
    # Calculate() evaluates combinations automatically and idempotently
    # (verified: 1.2D+1.6L returned exactly 1.2*M_dead + 1.6*M_live).


    def _iter_all_cases(self) -> List[Tuple[int, Any]]:
        """[P5] Returns [(case_number, case_object)] for every defined case."""
        out: List[Tuple[int, Any]] = []
        try:
            coll = self.structure.Cases.GetAll()
            n = int(coll.Count) if coll is not None else 0
        except Exception:
            n = 0
        for i in range(1, n + 1):   # collection Get() is 1-based (verified live)
            try:
                obj = coll.Get(i)
                num = int(obj.Number)
                out.append((num, self.structure.Cases.Get(num)))
            except Exception:
                continue
        return out

    # ------------------------------------------------------------------ #
    # [EUROCODE Phase A] Bracing / unbraced-length side-table
    # ------------------------------------------------------------------ #

    def _real_bar_ids(self) -> List[int]:
        """Real, existing bar numbers (T2: never trust a bare .Get()).

        Bare ``.Get(n)`` on Robot collections SILENTLY auto-creates proxies
        for nonexistent IDs on this build — validation always goes through
        a real enumeration.
        """
        out: List[int] = []
        try:
            coll = self.structure.Bars.GetAll()
            n = int(coll.Count) if coll is not None else 0
        except Exception:
            n = 0
        for i in range(1, n + 1):
            try:
                out.append(int(coll.Get(i).Number))
            except Exception:
                continue
        return out

    @com_thread_safe
    def set_bar_bracing(
        self,
        bar_id: int,
        lcr_y: Optional[float] = None,
        lcr_z: Optional[float] = None,
        lcr_lt: Optional[float] = None,
        brace_points: Optional[List[float]] = None,
    ) -> Dict[str, Any]:
        """[EUROCODE Phase A] Records engineer-specified unbraced lengths /
        bracing points for a bar in the session bracing registry.

        Robot has no bracing property — this is an explicit input layer.
        The bar is validated against real ids (T2) and its physical length
        is read so the resolved summary can tag defaults and suspicious
        K-factors. See tools.bracing_registry for the default-and-warn
        contract (an unspecified value resolves to the FULL bar length with
        a warning, which is a conservative assumption — NOT a verified
        bracing condition).
        """
        self._ensure_connected()
        bar_id = int(bar_id)
        if bar_id not in self._real_bar_ids():
            raise ValueError(
                f"bar {bar_id} does not exist in the model "
                f"(real bars: {sorted(self._real_bar_ids())[:10]}"
                f"{'...' if len(self._real_bar_ids()) > 10 else ''}).")
        length_m = self._bar_length(bar_id)
        self.bracing.set_bracing(
            bar_id, lcr_y=lcr_y, lcr_z=lcr_z, lcr_lt=lcr_lt,
            brace_points=brace_points, bar_length=length_m)
        return self.bracing.resolve(bar_id, length_m)

    @com_thread_safe
    def get_bar_bracing(self, bar_id: Optional[int] = None) -> Dict[str, Any]:
        """[EUROCODE Phase A] Resolved bracing summary for one bar (or all
        bars that have any entry, plus all real bars when none are set).

        Each row carries the resolved Lcr values with their source
        ("explicit" | "brace_points" | "defaulted") and any warnings, so
        results are traceable to what was specified vs assumed.
        """
        self._ensure_connected()
        real = self._real_bar_ids()
        if bar_id is not None:
            bar_id = int(bar_id)
            if bar_id not in real:
                raise ValueError(
                    f"bar {bar_id} does not exist in the model "
                    f"(real bars: {sorted(real)[:10]}).")
            return self.bracing.resolve(bar_id, self._bar_length(bar_id))
        rows = []
        for bid in sorted(real):
            rows.append(self.bracing.resolve(bid, self._bar_length(bid)))
        return {"bars": rows,
                "note": "lcr_*_source 'defaulted' = conservative full-length "
                        "assumption, not a verified bracing condition."}

    # ------------------------------------------------------------------ #
    # [EUROCODE Phase D] Simple-shear connection side-table + checks
    # ------------------------------------------------------------------ #

    @com_thread_safe
    def define_connection(self, bar_id: int, joint_end: str = "end",
                          **kwargs) -> Dict[str, Any]:
        """[EUROCODE Phase D] Stores a simple-shear connection definition
        (fin plate / double angle / end plate) in the session side-table.

        Robot has no connection-design server, so this is an explicit
        engineer-input layer. See tools.connection_check for the schema and
        the EN 1993-1-8 checks. Multi-column bolt layouts are rejected in
        v1 (block-shear geometry model is single-line only).
        """
        self._ensure_connected()
        bar_id = int(bar_id)
        if bar_id not in self._real_bar_ids():
            raise ValueError(
                f"bar {bar_id} does not exist in the model "
                f"(real bars: {sorted(self._real_bar_ids())[:10]}).")
        return {"bar_id": bar_id, "joint_end": str(joint_end or "end").lower(),
                "connection": self.connections.set_connection(
                    bar_id, joint_end, **kwargs)}

    @com_thread_safe
    def check_connection_capacity(self, bar_id: int, joint_end: str = "end",
                                  case_id: int = 1) -> Dict[str, Any]:
        """[EUROCODE Phase D] Checks the DEFINED connection at ``joint_end``
        against the solved end shear (EN 1993-1-8 simple shear). Bearing on
        the beam web uses the live section web thickness; the member grade
        for web bearing is read from the material name when it declares an
        EN grade (else that sub-check is skipped, honestly)."""
        self._ensure_connected()
        bar_id = int(bar_id)
        if bar_id not in self._real_bar_ids():
            raise ValueError(
                f"bar {bar_id} does not exist in the model "
                f"(real bars: {sorted(self._real_bar_ids())[:10]}).")
        joint_end = str(joint_end or "end").lower()
        conn = self.connections.get(bar_id, joint_end)
        base = {"bar_id": bar_id, "joint_end": joint_end}
        if conn is None:
            return {**base, "status": "NOT_CHECKABLE",
                    "reason": "no connection defined for this bar/joint_end "
                              "(call define_connection first)."}
        df = self.export_all_member_forces(case_id=int(case_id), divisions=8)
        sub = df[df["Bar_ID"] == bar_id] if df is not None else None
        if sub is None or sub.empty:
            return {**base, "status": "NOT_CHECKABLE",
                    "reason": "no force results for this case — solve first."}
        row = sub.iloc[0] if joint_end == "start" else sub.iloc[-1]
        fy_kn, fz_kn = float(row.get("FY_kN", 0.0)), float(row.get("FZ_kN", 0.0))
        v_ed_n = math.hypot(fy_kn, fz_kn) * 1e3

        member_fu, member_tw = None, None
        try:
            bar_obj = self.structure.Bars.Get(bar_id)
            sec_name = str(bar_obj.GetLabelName(RobotEnum.I_LT_BAR_SECTION))
            props = read_section_props(self, sec_name)
            if props:
                member_tw = props["tw_m"] * 1000.0
            _fy, mat_name, _r = self._bar_strength_mpa(bar_obj)
            member_fu = fu_for_grade(mat_name)
        except Exception:
            pass

        res = check_simple_shear_connection(conn, v_ed_n,
                                            member_fu_mpa=member_fu,
                                            member_web_t_mm=member_tw)
        return {**base, **res}

    def _as_combination(self, case_obj) -> Optional[Any]:
        """[P5] Returns the IRobotCaseCombination view of a case, or None.

        CAUTION (verified live): CastTo to IRobotCaseCombination succeeds
        via QI even on SIMPLE cases (the Robot coclass implements both
        interfaces), so it cannot discriminate. The reliable signal is
        IRobotCase.Type == I_CT_COMBINATION (1); fallback for objects
        without a readable Type is AnalizeType == I_CAT_COMB (0).
        """
        try:
            if int(case_obj.Type) != 1:      # 1 = I_CT_COMBINATION
                return None
        except Exception:
            try:
                if int(case_obj.AnalizeType) != RobotEnum.I_CAT_COMB:
                    return None
            except Exception:
                return None
        try:
            return CastTo(case_obj, "IRobotCaseCombination")
        except Exception:
            return None

    @com_thread_safe
    def define_combination(
        self,
        name: str,
        case_factors: Dict[int, float],
        combination_type: str = "ULS",
    ) -> dict:
        """
        [P5] Creates (or redefines) a load combination such as
        1.2*Dead + 1.6*Live. case_factors maps load-case ids to factors,
        e.g. {1: 1.2, 2: 1.6}. combination_type: 'ULS' | 'SLS' | 'ALS'.
        Verified live: Calculate() evaluates combinations automatically —
        no separate solve trigger is needed.
        """
        self._ensure_connected()
        if not name or not str(name).strip():
            raise ValueError("define_combination requires a non-empty name.")
        if not case_factors:
            raise ValueError("case_factors must map at least one "
                             "{case_id: factor}.")
        cbt = RobotEnum.CBT_NAMES.get(str(combination_type).upper())
        if cbt is None:
            raise ValueError(
                f"combination_type must be one of "
                f"{sorted(RobotEnum.CBT_NAMES)} — got '{combination_type}'.")

        cases = self.structure.Cases
        # Validate component cases: must exist and be simple cases.
        simple_case_names: Dict[int, str] = {}
        for cid, factor in case_factors.items():
            cid_i, factor_f = int(cid), float(factor)
            if not cases.Exist(cid_i):
                raise ValueError(
                    f"Case {cid_i} does not exist — create it with "
                    "create_load_case before combining.")
            obj = cases.Get(cid_i)
            if self._as_combination(obj) is not None:
                raise ValueError(
                    f"Case {cid_i} is itself a combination — nested "
                    "combinations are not supported.")
            try:
                simple_case_names[cid_i] = str(obj.Name)
            except Exception:
                simple_case_names[cid_i] = f"Case {cid_i}"

        # Redefine semantics: delete an existing combination with this name.
        for num, obj in self._iter_all_cases():
            cmb = self._as_combination(obj)
            if cmb is None:
                continue
            try:
                if str(cmb.Name).strip().lower() == str(name).strip().lower():
                    cases.Delete(num)
                    break
            except Exception:
                continue

        try:
            num = int(cases.FreeNumber)   # property, not a method (makepy)
        except Exception:
            num = 1 + max([n for n, _ in self._iter_all_cases()] or [0])
        try:
            cases.CreateCombination(num, str(name), cbt,
                                    RobotEnum.I_CN_PERMANENT,
                                    RobotEnum.I_CAT_COMB)
        except Exception as exc:
            raise RuntimeError(
                f"Robot refused to create combination '{name}': {exc}")

        combo = self._as_combination(cases.Get(num))
        if combo is None:
            raise RuntimeError(
                f"Combination '{name}' (case {num}) could not be resolved "
                "via IRobotCaseCombination.")
        cf = combo.CaseFactors
        for cid, factor in case_factors.items():
            try:
                cf.New(int(cid), float(factor))
            except Exception as exc:
                # Fallback: raw dispatch Invoke with an explicit 3rd arg
                # (the typed stub's 2-arg call is rejected by some builds).
                try:
                    cf._oleobj_.Invoke(1610743809, 0, 1,
                                       int(cid), float(factor), 0)
                except Exception:
                    raise RuntimeError(
                        f"Failed to add factor {factor} x case {cid} to "
                        f"combination '{name}': {exc}")

        readback = []
        try:
            for i in range(1, int(cf.Count) + 1):
                f = cf.Get(i)
                readback.append({"case": int(f.CaseNumber),
                                 "factor": round(float(f.Factor), 6)})
        except Exception:
            readback = [{"case": int(k), "factor": round(float(v), 6)}
                        for k, v in case_factors.items()]
        return {
            "combination": str(name), "case_id": num,
            "type": str(combination_type).upper(),
            "factors": readback,
            "component_cases": simple_case_names,
            "note": "Combinations are evaluated automatically by solve() "
                    "(verified live) — no separate trigger needed.",
        }

    @com_thread_safe
    def list_combinations(self) -> List[dict]:
        """[P5] Lists every defined combination with its factors."""
        self._ensure_connected()
        out: List[dict] = []
        for num, obj in self._iter_all_cases():
            cmb = self._as_combination(obj)
            if cmb is None:
                continue
            entry = {"case_id": num, "name": str(cmb.Name)}
            try:
                entry["type"] = {0: "ULS", 1: "SLS", 2: "ALS",
                                 3: "SPC"}.get(int(cmb.CombinationType),
                                               str(cmb.CombinationType))
            except Exception:
                entry["type"] = "?"
            factors = []
            try:
                cf = cmb.CaseFactors
                for i in range(1, int(cf.Count) + 1):
                    f = cf.Get(i)
                    factors.append({"case": int(f.CaseNumber),
                                    "factor": round(float(f.Factor), 6)})
            except Exception:
                pass
            entry["factors"] = factors
            out.append(entry)
        return out

    @com_thread_safe
    def solve_combination(self, name: Optional[str] = None) -> dict:
        """
        [P5] Runs the solver. VERIFIED LIVE: CalcEngine.Calculate()
        evaluates ALL cases AND combinations in one call — combinations
        need no separate trigger (1.2D+1.6L returned exactly
        1.2*M_dead + 1.6*M_live, idempotently on re-solve). This tool
        therefore solves, then reports the defined combinations.
        """
        self._ensure_connected()
        combos_before = self.list_combinations()
        if name is not None:
            match = [c for c in combos_before
                     if c["name"].strip().lower() == str(name).strip().lower()]
            if not match:
                raise ValueError(
                    f"No combination named '{name}'. Defined: "
                    f"{[c['name'] for c in combos_before] or '(none)'}.")
        self.solve()
        return {
            "status": "ok",
            "combinations": combos_before,
            "message": (
                "Solver run completed. Robot's Calculate() evaluates all "
                "combinations automatically (verified live); read combined "
                "results with export_member_forces / export_reactions using "
                "the combination's case_id, or get_governing_combination."),
        }

    @com_thread_safe
    def get_governing_combination(
        self,
        bar_id: int,
        component: str = "MY",
        divisions: int = 5,
    ) -> dict:
        """
        [P5] Finds which case/combination produces the max |component| on a
        bar. component: one of FX, FY, FZ, MX, MY, MZ. Simple cases are
        listed for reference; the governing combination is reported first.
        """
        self._ensure_connected()
        component = str(component).upper()
        if component not in ("FX", "FY", "FZ", "MX", "MY", "MZ"):
            raise ValueError("component must be one of FX/FY/FZ/MX/MY/MZ.")
        divisions = max(1, min(divisions, 50))
        bars = self.structure.Bars
        coll = bars.GetAll()
        count = int(coll.Count) if coll is not None else 0
        if bar_id not in self._enumerate_bar_ids(bars, count):
            raise ValueError(f"Bar {bar_id} does not exist.")

        force_srv = self.structure.Results.Bars.Forces
        ranking = []
        for num, obj in self._iter_all_cases():
            is_combo = self._as_combination(obj) is not None
            try:
                nm = str(obj.Name)
            except Exception:
                nm = f"Case {num}"
            worst = 0.0
            for ratio_pos in sorted(
                    {round(d / divisions, 6) for d in range(divisions + 1)}
                    | {0.5}):   # midspan matters (peak moment)
                try:
                    v = abs(float(getattr(
                        force_srv.Value(bar_id, num, ratio_pos),
                        component)))
                    worst = max(worst, v)
                except Exception:
                    continue
            ranking.append({"case_id": num, "name": nm,
                            "kind": "combination" if is_combo else "case",
                            f"max_abs_{component}": round(worst, 4)})
        ranking.sort(key=lambda r: r[f"max_abs_{component}"], reverse=True)
        combos = [r for r in ranking if r["kind"] == "combination"]
        governing = combos[0] if combos else (
            ranking[0] if ranking else None)
        return {
            "bar_id": bar_id, "component": component,
            "governing": governing, "ranking": ranking,
        }

    # ------------------------------------------------------------------ #
    # WP4: materials / panels (grillage approximation) / volumes
    # ------------------------------------------------------------------ #
    # Live findings from Robot SA 2027 probes: the RobotOM v27 type
    # library exposes NO panel/plate object server, and direct finite-
    # element creation (FiniteElems.Create) cannot marshal its node
    # parameter (array/selection/string all rejected). Panels are
    # therefore approximated as equivalent bar grillages — the standard
    # engineering substitute for plate/slab action — using the fully
    # verified bar pipeline. Solid volumes ARE natively supported via
    # Objects.CreateSolid (face-string syntax) and are implemented for
    # real. All of this is reported honestly in the tool descriptions.

    _PANEL_BAR_SECTIONS = [100, 120, 140, 160, 180, 200, 220, 240, 270,
                           300, 330, 360, 400, 450, 500]

    def _nearest_panel_section(self, thickness_m: float) -> str:
        """[WP4] Maps a plate thickness (m) onto the nearest IPE depth (mm),
        clamped to the loaded EURO catalog."""
        depth_mm = max(80, int(round(thickness_m * 1000.0)))
        best = min(self._PANEL_BAR_SECTIONS, key=lambda d: abs(d - depth_mm))
        return f"IPE {best}"

    @com_thread_safe
    def set_material(
        self,
        material_name: str = "STEEL",
        e_mpa: Optional[float] = None,
        nu: Optional[float] = None,
        fy_mpa: Optional[float] = None,
        apply_to_bars: bool = True,
    ) -> dict:
        """
        [WP4/P4] Creates/reuses a material label (type 8) with the given name.
        Named materials load from Robot's database (verified: 'STEEL' ->
        E=210 GPa, NU=0.3, RE=235 MPa = fy of S235); custom values can
        override E (MPa), NU and — new in P4 — the design strength RE
        (fy, MPa; Robot stores it in Pa). RE is what
        get_utilization_ratios uses as the check denominator, so custom
        materials SHOULD set fy_mpa to be code-checkable. Optionally
        applies the material to every bar in the model.
        """
        self._ensure_connected()
        name = str(material_name).upper()
        labels = self.structure.Labels
        lab = labels.Create(RobotEnum.I_LT_MATERIAL, name)
        data = lab.Data
        if e_mpa is None and nu is None and fy_mpa is None:
            try:
                data.LoadFromDBase(name)
            except Exception as exc:
                logger.warning("LoadFromDBase(%s) failed: %s", name, exc)
        if e_mpa is not None:
            data.E = float(e_mpa) * 1e6  # MPa -> Pa
        if nu is not None:
            data.NU = float(nu)
        if fy_mpa is not None:
            data.RE = float(fy_mpa) * 1e6  # MPa -> Pa (verified: STEEL.RE=235e6)
        labels.Store(lab)
        logger.info("Material label '%s' stored (E=%.3g Pa, RE=%.3g Pa).",
                    name, data.E, data.RE)

        applied = 0
        if apply_to_bars:
            bar_coll = self.structure.Bars.GetAll()
            count = int(bar_coll.Count) if bar_coll is not None else 0
            for bar_id in self._enumerate_bar_ids(self.structure.Bars, count):
                try:
                    self.structure.Bars.Get(bar_id).SetLabel(
                        RobotEnum.I_LT_MATERIAL, name)
                    applied += 1
                except Exception as exc:
                    logger.warning("Material apply failed on bar %s: %s",
                                   bar_id, exc)
        return {"material": name, "e_pa": data.E,
                "bars_reassigned": applied}

    @com_thread_safe
    def create_panel(
        self,
        panel_id: int,
        x: float = 0.0,
        y: float = 0.0,
        z: float = 0.0,
        width: float = 4.0,
        height: float = 3.0,
        normal: str = "Y",
        divisions_x: int = 4,
        divisions_z: int = 4,
        section: str = None,
        diagonals: bool = False,
    ) -> dict:
        """
        [WP4 HONEST] RobotOM v27 has no panel/plate object server, so a
        panel is built as an equivalent bar grillage: a rectangular grid of
        beams in the panel plane (plane perpendicular to `normal`), with
        `divisions_x` x `divisions_z` cells. This is the standard
        engineering substitute for plate/slab bending and is fully
        supported by the verified bar pipeline.

        `section` defaults to a scale-appropriate auto-size
        (``suggest_section`` on the panel's smaller plan dimension — the
        grillage bars bend in both directions) unless given explicitly.
        """
        self._ensure_connected()
        if not section:
            # [Part B] Scale-appropriate starting section for the grillage.
            span_m = min(float(width), float(height))
            section = suggest_section("beam", span_m, "IPE")
        dx = max(1, int(divisions_x))
        dz = max(1, int(divisions_z))
        normal = str(normal).upper()
        if normal not in ("X", "Y", "Z"):
            raise ValueError("normal must be 'X', 'Y' or 'Z'")

        # Build the grid of nodes in the panel plane.
        nx, nz = dx + 1, dz + 1
        next_node = self._first_free_node_number()
        grid: List[List[int]] = [[0] * nz for _ in range(nx)]
        for i in range(nx):
            for j in range(nz):
                if normal == "Y":      # horizontal slab, X x Z
                    fx, fy, fz = x + i * width / dx, y, z + j * height / dz
                elif normal == "X":    # wall in Y x Z
                    fx, fy, fz = x, y + i * width / dx, z + j * height / dz
                else:                  # wall in X x Y
                    fx, fy, fz = x + i * width / dx, y + j * height / dz, z
                nid = next_node + i * nz + j
                grid[i][j] = self.create_node(nid, fx, fy, fz)

        # Grillage bars: horizontal rows + vertical columns (+ diagonals).
        bar_ids: List[int] = []
        next_bar = self._first_free_bar_number()
        for j in range(dz + 1):
            for i in range(dx):
                bid = next_bar + len(bar_ids)
                self.create_bar(bid, grid[i][j], grid[i + 1][j], section)
                bar_ids.append(bid)
        for i in range(dx + 1):
            for j in range(dz):
                bid = next_bar + len(bar_ids)
                self.create_bar(bid, grid[i][j], grid[i][j + 1], section)
                bar_ids.append(bid)
        if diagonals:
            for i in range(dx):
                for j in range(dz):
                    bid = next_bar + len(bar_ids)
                    self.create_bar(bid, grid[i][j], grid[i + 1][j + 1],
                                    section)
                    bar_ids.append(bid)
                    bid = next_bar + len(bar_ids)
                    self.create_bar(bid, grid[i + 1][j], grid[i][j + 1],
                                    section)
                    bar_ids.append(bid)

        self._panel_meta[panel_id] = {
            "normal": normal, "dx": dx, "dz": dz,
            "width": float(width), "height": float(height),
            "grid": grid, "bar_ids": bar_ids,
        }
        logger.info("Panel %s grillage: %s nodes, %s bars.",
                    panel_id, nx * nz, len(bar_ids))
        return {"panel_id": panel_id, "nodes": nx * nz,
                "bars": len(bar_ids), "normal": normal,
                "bar_ids": bar_ids[:20]}

    @com_thread_safe
    def set_panel_thickness(self, panel_id: int, thickness_m: float) -> dict:
        """
        [WP4 HONEST] Re-sections every grillage bar of the panel to the
        nearest IPE depth for the requested plate thickness (the section
        depth approximates the slab stiffness).
        """
        self._ensure_connected()
        meta = self._panel_meta.get(panel_id)
        if meta is None:
            raise RuntimeError(f"Panel {panel_id} not found; create it first.")
        section = self._nearest_panel_section(float(thickness_m))
        changed = 0
        for bid in meta["bar_ids"]:
            try:
                self.modify_bar_section(bid, section)
                changed += 1
            except Exception as exc:
                logger.warning("set_panel_thickness bar %s: %s", bid, exc)
        return {"panel_id": panel_id, "section": section,
                "bars_resectioned": changed}

    @com_thread_safe
    def apply_panel_pressure(
        self,
        panel_id: int,
        case_id: int = 1,
        pressure_kpa: float = -1.0,
    ) -> dict:
        """
        [WP4 HONEST] Applies a uniform pressure (kPa) on the panel grillage
        as equivalent nodal loads at the grid nodes (tributary-area
        distribution: 1.0 interior / 0.5 edge / 0.25 corner cells). Sign
        follows the pressure value (negative = gravity/downward on slabs).
        """
        self._ensure_connected()
        meta = self._panel_meta.get(panel_id)
        if meta is None:
            raise RuntimeError(f"Panel {panel_id} not found; create it first.")
        p = float(pressure_kpa)  # kN/m^2
        dx, dz = meta["dx"], meta["dz"]
        cell_a = (meta["width"] / dx) * (meta["height"] / dz)
        normal = meta["normal"]
        grid = meta["grid"]
        total = 0.0
        count = 0
        for i, row in enumerate(grid):
            for j, nid in enumerate(row):
                w = 1.0
                if i in (0, dx):
                    w *= 0.5
                if j in (0, dz):
                    w *= 0.5
                f = p * cell_a * w
                total += abs(f)
                if abs(f) < 1e-9:
                    continue
                if normal == "Y":
                    self._apply_nodal_load_xyz(nid, case_id, 0.0, 0.0, f)
                elif normal == "X":
                    self._apply_nodal_load_xyz(nid, case_id, f, 0.0, 0.0)
                else:
                    self._apply_nodal_load_xyz(nid, case_id, 0.0, f, 0.0)
                count += 1
        return {"panel_id": panel_id, "pressure_kpa": p,
                "total_force_kN": round(total, 3), "nodes_loaded": count}

    @com_thread_safe
    def _apply_nodal_load_xyz(
        self, node_id: int, case_id: int,
        fx: float, fy: float, fz: float,
    ) -> None:
        """[WP4] Full 3-axis nodal force via the verified record pattern."""
        self._ensure_connected()
        case = CastTo(self.structure.Cases.Get(case_id), "IRobotSimpleCase")
        record = case.Records.Create(RobotEnum.I_LRT_NODE_FORCE)
        record.SetValue(0, fx)
        record.SetValue(1, fy)
        record.SetValue(2, fz)
        record.Objects.FromText(str(node_id))

    @com_thread_safe
    def create_solid(
        self,
        solid_id: int,
        node_ids: List[int],
        face_groups: List[List[int]],
    ) -> dict:
        """
        [WP4 VERIFIED] Creates a native 3D solid volume from existing nodes.
        `node_ids` must all exist; `face_groups` lists closed loops of node
        numbers for each bounding face. Verified live: Objects.CreateSolid
        with the semicolon-separated face string.
        """
        self._ensure_connected()
        faces = ";".join(
            " ".join(str(n) for n in face) for face in face_groups)
        try:
            obj = self.structure.Objects.CreateSolid(solid_id, faces)
        except Exception as exc:
            raise RuntimeError(
                f"CreateSolid({solid_id}) failed: {exc} — check that all "
                "nodes exist and each face is a closed, ordered loop."
            ) from exc
        # [WP4] Verified live: CreateSolid returns null in some sessions even
        # though the volume IS created — check existence via GetAll().
        exists, is_volume = False, False
        try:
            coll = self.structure.Objects.GetAll()
            if coll is not None:
                for i in range(1, int(coll.Count) + 1):
                    o = coll.Get(i)
                    if o is not None and int(getattr(o, "Number", -1)) == solid_id:
                        exists = True
                        is_volume = bool(getattr(o, "IsVolume", False))
                        break
        except Exception:
            pass
        volume = None
        try:
            volume = self.structure.Objects.CalcVol()
        except Exception:
            volume = None
        return {"solid_id": solid_id, "nodes": len(node_ids),
                "faces": len(face_groups), "created": exists,
                "is_volume": is_volume, "volume_m3": volume,
                "object": type(obj).__name__ if obj is not None
                else ("IRobotObjObject" if exists else None)}

    @com_thread_safe
    def create_solid_box(
        self,
        solid_id: int,
        origin_x: float = 0.0,
        origin_y: float = 0.0,
        origin_z: float = 0.0,
        size_x: float = 1.0,
        size_y: float = 1.0,
        size_z: float = 1.0,
    ) -> dict:
        """
        [WP4] Convenience: creates a rectangular solid (box) by generating
        its 8 corner nodes and calling the native CreateSolid.
        """
        self._ensure_connected()
        first = self._first_free_node_number()
        sx, sy, sz = size_x, size_y, size_z
        ox, oy, oz = origin_x, origin_y, origin_z
        corners = [
            (ox, oy, oz), (ox + sx, oy, oz), (ox + sx, oy, oz + sz),
            (ox, oy, oz + sz),          # bottom face CCW
            (ox, oy + sy, oz), (ox + sx, oy + sy, oz),
            (ox + sx, oy + sy, oz + sz), (ox, oy + sy, oz + sz),
        ]
        nids = []
        for k, (cx, cy, cz) in enumerate(corners):
            nid = first + k
            self.create_node(nid, cx, cy, cz)
            nids.append(nid)
        n1, n2, n3, n4 = nids[0], nids[1], nids[2], nids[3]
        n5, n6, n7, n8 = nids[4], nids[5], nids[6], nids[7]
        faces = [
            [n1, n2, n3, n4], [n5, n8, n7, n6],
            [n1, n5, n6, n2], [n4, n3, n7, n8],
            [n1, n4, n8, n5], [n2, n6, n7, n3],
        ]
        result = self.create_solid(solid_id, nids, faces)
        result["corner_nodes"] = nids
        return result


    def _max_node_number(self) -> int:
        try:
            nodes = self.structure.Nodes
            coll = nodes.GetAll()
            n = int(coll.Count) if coll is not None else 0
            ids = self._enumerate_node_ids(nodes, n)
            return max(ids) if ids else 0
        except Exception:
            return 0

    def _max_bar_number(self) -> int:
        try:
            bars = self.structure.Bars
            coll = bars.GetAll()
            n = int(coll.Count) if coll is not None else 0
            ids = self._enumerate_bar_ids(bars, n)
            return max(ids) if ids else 0
        except Exception:
            return 0

    def _first_free_node_number(self) -> int:
        return self._max_node_number() + 1

    def _first_free_bar_number(self) -> int:
        return self._max_bar_number() + 1

    # ------------------------------------------------------------------ #
    # WP7: modal analysis (frequencies + mode shapes)
    # ------------------------------------------------------------------ #
    # Live findings from Robot SA 2027 probes: modal cases CAN be created
    # (I_CAT_DYNAMIC_MODAL=11) and ModesCount set, and the results servers
    # live at Results.Advanced.Eigenvalues / Eigenvectors. HOWEVER, the
    # modal solver does NOT complete programmatically in this build:
    # CalcEngine.Calculate() never returns whenever a modal case exists
    # (it hangs after the GUI shows modes), and the results DB stays
    # empty. solve_modal therefore runs Calculate() on a bounded worker
    # thread, polls for real frequencies, and — if the solver never
    # completes — removes the modal case (so static analysis still works)
    # and reports the limitation honestly.

    @com_thread_safe
    def solve_modal(
        self,
        case_id: int = 1,
        n_modes: int = 10,
        timeout_s: int = 150,
    ) -> dict:
        """
        [WP7] Creates/reuses a modal case (I_CAT_DYNAMIC_MODAL), sets the
        number of modes, and checks whether modal results are already
        present (e.g. computed earlier in the Robot GUI). VERIFIED
        LIMITATION (multiple live probes): calling CalcEngine.Calculate()
        while a modal case exists NEVER returns in this RobotOM v27 build
        and leaves the engine unusable, so solve_modal does NOT trigger a
        programmatic solve. The modal case is removed afterwards so static
        analysis keeps working, and the user is told to run modal analysis
        in the Robot GUI, after which export_modal_frequencies /
        export_modal_mode_shapes read the stored results.
        """
        self._ensure_connected()
        try:
            case_obj = self.structure.Cases.Get(case_id)
        except Exception:
            case_obj = None
        if case_obj is None:
            self.create_load_case(
                case_id, f"MODAL{case_id}",
                analysis_type=RobotEnum.I_CAT_DYNAMIC_MODAL,
            )
            case_obj = self.structure.Cases.Get(case_id)
            if case_obj is None:
                raise RuntimeError(
                    f"Robot refused to create modal case {case_id}."
                )
        sc = CastTo(case_obj, "IRobotSimpleCase")
        sc.ModesCount = max(1, int(n_modes))
        try:
            ap = sc.GetAnalysisParams()
            ap.DisregardDensity = False  # masses from structural density
        except Exception:
            pass

        # Poll briefly for pre-existing modal results (never call
        # Calculate() with a modal case in this build — it hangs).
        ev = None
        try:
            ev = self.structure.Results.Advanced.Eigenvalues
        except Exception:
            ev = None
        ready = False
        t0 = time.time()
        while time.time() - t0 < min(10.0, float(timeout_s)):
            try:
                if ev is not None:
                    v = ev.Value(case_id, 1)
                    if v is not None and abs(float(v.Frequence)) > 1e-9:
                        ready = True
                        break
            except Exception:
                pass
            time.sleep(1)

        removed = False
        if not ready:
            try:
                self.structure.Cases.Delete(case_id)
                removed = True
            except Exception:
                removed = False
        return {
            "case_id": case_id,
            "modes_requested": int(n_modes),
            "results_available": ready,
            "solver_returned": ready,
            "elapsed_s": round(time.time() - t0, 1),
            "modal_case_removed": removed,
            "note": (
                "Modal results already present in the model."
                if ready else
                "Verified limitation of this RobotOM v27 build: the modal "
                "solver cannot be driven programmatically (Calculate() "
                "hangs and the results DB stays empty). The modal case was "
                "removed so static analysis still works. Run modal analysis "
                "in the Robot GUI and save the project — then "
                "export_modal_frequencies / export_modal_mode_shapes will "
                "read the stored frequencies and mode shapes."
            ),
        }


    @com_thread_safe
    def export_modal_frequencies(
        self, case_id: int = 1, n_modes: int = 10,
    ) -> pd.DataFrame:
        """
        [WP7] Reads natural frequencies via Results.Advanced.Eigenvalues.
        Columns: Mode, Frequency_Hz, Period_s, Pulsation_rad_s, Damping,
        AvPartCoeff. Returns an empty frame when no modal results exist.
        """
        self._ensure_connected()
        rows: List[Dict[str, Any]] = []
        try:
            ev = self.structure.Results.Advanced.Eigenvalues
        except Exception as exc:
            logger.warning("Eigenvalues server unavailable: %s", exc)
            return pd.DataFrame(columns=[
                "Mode", "Frequency_Hz", "Period_s", "Pulsation_rad_s",
                "Damping", "AvPartCoeff",
            ])
        for m in range(1, max(1, int(n_modes)) + 1):
            try:
                v = ev.Value(case_id, m)
                if abs(float(v.Frequence)) > 1e-9:
                    rows.append({
                        "Mode": m,
                        "Frequency_Hz": round(float(v.Frequence), 4),
                        "Period_s": round(float(v.Period), 4),
                        "Pulsation_rad_s": round(float(v.Pulsation), 4),
                        "Damping": round(float(v.Damping), 4),
                        "AvPartCoeff": round(float(v.AvPartCoeff), 4),
                    })
            except Exception as exc:
                logger.warning("Frequency read failed for mode %s: %s", m, exc)
        return pd.DataFrame(rows, columns=[
            "Mode", "Frequency_Hz", "Period_s", "Pulsation_rad_s",
            "Damping", "AvPartCoeff",
        ])

    @com_thread_safe
    def export_modal_mode_shapes(
        self, case_id: int = 1, mode_num: int = 1,
    ) -> pd.DataFrame:
        """
        [WP7] Reads a mode shape (eigenvector) for every node via
        Results.Advanced.Eigenvectors.Value(node, case, mode). Translations
        use the same 1e-3 scaling as nodal displacements (verified for the
        displacement server); rotations are in rad.
        """
        self._ensure_connected()
        rows: List[Dict[str, Any]] = []
        try:
            vec = self.structure.Results.Advanced.Eigenvectors
            nodes = self.structure.Nodes
            coll = nodes.GetAll()
            count = int(coll.Count) if coll is not None else 0
        except Exception as exc:
            logger.warning("Eigenvectors server unavailable: %s", exc)
            return pd.DataFrame(columns=[
                "Node_ID", "UX_m", "UY_m", "UZ_m",
                "RX_rad", "RY_rad", "RZ_rad",
            ])
        for node_id in self._enumerate_node_ids(nodes, count):
            try:
                v = vec.Value(node_id, case_id, mode_num)
                rows.append({
                    "Node_ID": node_id,
                    "UX_m": round(v.UX * 1000.0, 6),
                    "UY_m": round(v.UY * 1000.0, 6),
                    "UZ_m": round(v.UZ * 1000.0, 6),
                    "RX_rad": round(v.RX, 8),
                    "RY_rad": round(v.RY, 8),
                    "RZ_rad": round(v.RZ, 8),
                })
            except Exception as exc:
                logger.warning("Mode-shape read failed for node %s: %s",
                               node_id, exc)
        return pd.DataFrame(rows, columns=[
            "Node_ID", "UX_m", "UY_m", "UZ_m",
            "RX_rad", "RY_rad", "RZ_rad",
        ])

    def _enumerate_bar_ids(self, bars, bar_count: int) -> List[int]:
        """[FIX H2] Enumerates actual bar IDs from the Robot model instead
        of assuming sequential 1..N.

        [FIX R4] IRobotCollection.Get(i) returns the bar OBJECT, not its
        number (verified live) — extract `.Number` from each item."""
        bar_ids = []
        try:
            all_bars = bars.GetAll()
            if all_bars is not None:
                for i in range(1, bar_count + 1):
                    try:
                        item = all_bars.Get(i)
                        number = getattr(item, "Number", None)
                        if number is not None:
                            bar_ids.append(int(number))
                    except Exception:
                        continue
        except Exception:
            pass
        if not bar_ids:
            # Fallback to sequential if GetAll is unavailable
            bar_ids = list(range(1, bar_count + 1))
        return bar_ids

    def _enumerate_node_ids(self, nodes, node_count: int) -> List[int]:
        """[FIX H2] Enumerates actual node IDs from the Robot model instead
        of assuming sequential 1..N.

        [FIX R4] IRobotCollection.Get(i) returns the node OBJECT, not its
        number (verified live) — extract `.Number` from each item."""
        node_ids = []
        try:
            all_nodes = nodes.GetAll()
            if all_nodes is not None:
                for i in range(1, node_count + 1):
                    try:
                        item = all_nodes.Get(i)
                        number = getattr(item, "Number", None)
                        if number is not None:
                            node_ids.append(int(number))
                    except Exception:
                        continue
        except Exception:
            pass
        if not node_ids:
            # Fallback to sequential if GetAll is unavailable
            node_ids = list(range(1, node_count + 1))
        return node_ids

    @com_thread_safe
    def _bar_length(self, bar_id: int) -> float:
        """[FIX H1] Decorated with @com_thread_safe so COM fallback path
        is properly thread-safe.

        Computes Euclidean length of a bar from cached node coordinates,
        falling back to Robot's geometry API if unavailable locally.
        """
        if bar_id in self._bar_endpoints:
            n1, n2 = self._bar_endpoints[bar_id]
            if n1 in self._node_coords and n2 in self._node_coords:
                x1, y1, z1 = self._node_coords[n1]
                x2, y2, z2 = self._node_coords[n2]
                return ((x2 - x1) ** 2 + (y2 - y1) ** 2 + (z2 - z1) ** 2) ** 0.5

        try:
            bar = self.structure.Bars.Get(bar_id)
            return float(bar.Geometry.Length)
        except Exception:
            return 0.0

    _SECTION_UNIT_MASS_TABLE = {
        # kg/m for common catalog sections (approximate, EU/AISC hybrid table)
        "HEA100": 16.7, "HEA120": 19.9, "HEA140": 24.7, "HEA160": 30.4,
        "HEA180": 35.5, "HEA200": 42.3, "HEA220": 50.5, "HEA240": 60.3,
        "HEB100": 20.4, "HEB120": 26.7, "HEB140": 33.7, "HEB160": 42.6,
        "HEB180": 51.2, "HEB200": 61.3, "HEB220": 71.5, "HEB240": 83.2,
        "IPE100": 8.1, "IPE120": 10.4, "IPE140": 12.9, "IPE160": 15.8,
        "IPE180": 18.8, "IPE200": 22.4, "IPE220": 26.2, "IPE240": 30.7,
        "IPE270": 36.1, "IPE300": 42.2, "IPE330": 49.1, "IPE360": 57.1,
        "IPE400": 66.3, "IPE450": 77.6, "IPE500": 90.7,
        "UB203x133x25": 25.0, "UB305x165x40": 40.0, "UC203x203x46": 46.0,
    }

    def _lookup_unit_mass(self, section_name: str, density_kg_m3: float) -> float:
        """Looks up unit mass (kg/m) for a section name, first trying the
        static catalog table, then the live Robot section database.

        [FIX R6] The live path reads the real cross-sectional area via
        label.Data.GetValue(0) (area in m^2 — verified: IPE 300 returns
        0.0053812 m^2, giving 42.24 kg/m at 7850 kg/m^3, matching the
        catalog exactly)."""
        key = section_name.strip().upper().replace(" ", "")
        for table_key, mass in self._SECTION_UNIT_MASS_TABLE.items():
            if table_key.upper() == key:
                return mass

        try:
            labels = self.structure.Labels
            for cand in self._section_label_candidates(section_name):
                try:
                    label = labels.Get(RobotEnum.I_LT_BAR_SECTION, cand)
                except Exception:
                    continue
                if label is None:
                    continue
                try:
                    area_m2 = float(label.Data.GetValue(0))
                    if area_m2 > 0:
                        return area_m2 * density_kg_m3
                except Exception:
                    continue
        except Exception as exc:
            logger.warning(
                "Live section mass lookup failed for '%s': %s", section_name, exc
            )

        logger.warning(
            "Unit mass for section '%s' not found in catalog or Robot "
            "database; defaulting to 30.0 kg/m estimate.", section_name
        )
        return 30.0

    # ------------------------------------------------------------------ #
    # Milestone A: concentrated load, model clear
    # ------------------------------------------------------------------ #

    @com_thread_safe
    def apply_bar_concentrated(
        self,
        bar_id: int,
        case_id: int,
        fx_kn: float = 0.0,
        fy_kn: float = 0.0,
        fz_kn: float = 0.0,
        ratio: float = 0.5,
    ) -> None:
        """Applies a concentrated point force (FX, FY, FZ) at a relative
        position `ratio` (0..1) along a bar, in a given case.

        [MILESTONE A] Value indices from IRobotBarForceConcentrateRecordValues
        (verified live): FX=0, FY=1, FZ=2, and the load's location is the
        relative coordinate I_BFCRV_REL=13.
        """
        self._ensure_connected()
        ratio = max(0.0, min(float(ratio), 1.0))
        case = CastTo(self.structure.Cases.Get(case_id), "IRobotSimpleCase")
        record = case.Records.Create(RobotEnum.I_LRT_BAR_FORCE_CONCENTRATED)
        record.SetValue(RobotEnum.I_BFCRV_FX, float(fx_kn))
        record.SetValue(RobotEnum.I_BFCRV_FY, float(fy_kn))
        record.SetValue(RobotEnum.I_BFCRV_FZ, float(fz_kn))
        record.SetValue(RobotEnum.I_BFCRV_REL, ratio)
        record.Objects.FromText(str(bar_id))
        logger.info(
            "Applied concentrated load (FX=%.2f FY=%.2f FZ=%.2f) at ratio "
            "%.2f to bar %s in case %s.", fx_kn, fy_kn, fz_kn, ratio,
            bar_id, case_id,
        )

    # Simple-case natures for create_load_case (permanent / imposed).
    _NATURE_MAP = {
        "permanent": RobotEnum.I_CN_PERMANENT,
        "imposed": RobotEnum.I_CN_IMPOSED,
    }

    @staticmethod
    def eurocode_combination_factors(
        cases, combination_set: str = "ULS_SLS_basic",
    ) -> List[Dict[str, Any]]:
        """PURE: EN 1990 combination-factor plans for a set of load cases.

        ``cases`` is a list of (case_id, nature) with nature in
        {"permanent", "imposed"} (the create_load_case schema vocabulary;
        imposed == variable action). Returns a list of
        {name, case_factors: {case_id: factor}, combination_type} ready
        for define_combination():

          * ULS:      1.35 x every permanent + 1.5 x the leading variable
                      + 1.5*0.7 = 1.05 x each other variable (one ULS per
                      variable case as leading; plain 1.35G if none).
          * SLS char: 1.0 x every case (characteristic combination).

        ``combination_set``: "ULS_SLS_basic" (default) | "ULS_only" |
        "SLS_only". Raises ValueError on an empty case list or an unknown
        nature/set.
        """
        combo_set = str(combination_set or "").strip()
        if combo_set not in ("ULS_SLS_basic", "ULS_only", "SLS_only"):
            raise ValueError(
                f"combination_set must be one of "
                f"{{'ULS_SLS_basic','ULS_only','SLS_only'}} "
                f"(got '{combination_set}').")
        if not cases:
            raise ValueError(
                "generate_code_combinations needs at least one load case "
                "with a 'nature' (create_load_case first).")

        permanent = [(int(cid), str(nat)) for cid, nat in cases
                     if str(nat).lower() == "permanent"]
        variable = [(int(cid), str(nat)) for cid, nat in cases
                    if str(nat).lower() == "imposed"]
        unknown = sorted({str(nat) for _, nat in cases}
                         - {"permanent", "imposed"})
        if unknown:
            raise ValueError(
                f"Unknown load-case nature(s) {unknown}; supported: "
                "permanent / imposed.")

        plans: List[Dict[str, Any]] = []

        def _uls(q_lead):
            factors = {cid: 1.35 for cid, _ in permanent}
            factors.update({cid: 1.5 for cid, _ in variable if cid == q_lead})
            factors.update({cid: 1.05 for cid, _ in variable
                            if cid != q_lead})
            return factors

        if combo_set in ("ULS_SLS_basic", "ULS_only"):
            if variable:
                for cid, _ in variable:
                    plans.append({
                        "name": f"ULS_{cid}", "case_factors": _uls(cid),
                        "combination_type": "ULS",
                    })
            else:
                plans.append({
                    "name": "ULS", "case_factors": _uls(None),
                    "combination_type": "ULS",
                })

        if combo_set in ("ULS_SLS_basic", "SLS_only"):
            plans.append({
                "name": "SLS_char",
                "case_factors": {cid: 1.0 for cid, _ in cases},
                "combination_type": "SLS",
            })
        return plans

    @com_thread_safe
    def clear_structure(self, project_type: str = "3D") -> None:
        """[MILESTONE A] Resets the current project to a blank model of the
        given type ('3D' or '2D'), clearing all in-memory bookkeeping."""
        self._ensure_connected()
        code = (
            RobotEnum.I_PT_BAR_3D if project_type.lower() == "3d"
            else RobotEnum.I_PT_BAR_2D
        )
        self.project = self.robot_app.Project
        self._guarded_project_new(code)
        self._project_type = "3D" if project_type.lower() == "3d" else "2D"
        self.structure = self.project.Structure
        self._node_coords.clear()
        self._bar_endpoints.clear()
        self._section_assignments.clear()
        self.bracing.clear()
        self.connections.clear()
        logger.info(
                "clear_structure called (project_type=%s, pid=%s) - model reset "
                "to blank %s frame.",
                project_type, self.connected_pid, project_type)

    # --- [SPEC_AND_SUMMARY] ---
    # ------------------------------------------------------------------ #
    # Milestone A: model-spec builder + structure summary
    # ------------------------------------------------------------------ #

    @staticmethod
    def spec_integrity_issues(spec: Dict[str, Any]) -> List[str]:
        """[INTEGRITY] Pure, no-COM pre-flight check for a structure spec.
        Returns a list of human-readable problems (empty == spec is
        well-formed). Catches the 'silently built fewer bars' class of bug
        BEFORE any Robot call: duplicate node/bar ids (Robot's Create()
        overwrites silently), bars whose endpoints reference nodes that
        are not defined in the spec, and duplicate support/node entries."""
        issues: List[str] = []
        spec = spec or {}
        node_ids = [int(n["id"]) for n in spec.get("nodes", []) or []]
        if len(node_ids) != len(set(node_ids)):
            dup = sorted({nid for nid in node_ids if node_ids.count(nid) > 1})
            issues.append(
                f"duplicate node id(s) {dup} (Robot silently overwrites on "
                "re-Create - rename them so each is unique)")
        node_set = set(node_ids)
        bar_ids = [int(b["id"]) for b in spec.get("bars", []) or []]
        if len(bar_ids) != len(set(bar_ids)):
            dup = sorted({bid for bid in bar_ids if bar_ids.count(bid) > 1})
            issues.append(
                f"duplicate bar id(s) {dup} (would silently reduce the real "
                "bar count below what the spec asked for)")
        for b in spec.get("bars", []) or []:
            n1, n2 = int(b["n1"]), int(b["n2"])
            for n in (n1, n2):
                if n not in node_set:
                    issues.append(
                        f"bar {int(b['id'])} references node {n} which is not "
                        "defined in 'nodes'")
        if issues:
            return issues
        return []

    @com_thread_safe
    def build_structure_from_spec(self, spec: Dict[str, Any]) -> Dict[str, Any]:
        """Builds an entire model from a structured spec dict in a single
        call, so the agent can create large / complex structures without
        exhausting its per-turn tool-step or token budget.

        Supported spec keys:
          project  : "3D" | "2D"                                   (default 3D)
          nodes    : [{"id":int,"x":float,"y":float,"z":float}]
          bars     : [{"id":int,"n1":int,"n2":int,"section":str}]
          supports : [{"node":int,"type":"pinned"|"fixed"|"roller_x"|"roller_z"}]
          cases    : [{"id":int,"name":str,"nature":"permanent"|"imposed"}]
          loads    : [
                       {"kind":"bar_uniform","bar":int,"case":int,
                        "direction":"X|Y|Z","value":float},
                       {"kind":"bar_concentrated","bar":int,"case":int,
                        "fx":float,"fy":float,"fz":float,"ratio":float},
                       {"kind":"nodal","node":int,"case":int,
                        "fx":float,"fz":float,"my":float}
                     ]
        Returns get_structure_summary() plus a status field.
        """
        self._ensure_connected()
        spec = spec or {}

        # [INTEGRITY] Fail LOUDLY before touching Robot when the spec has
        # duplicate ids or dangling node references - silently building
        # "fewer bars than requested" (then failing later on bar 11+ with
        # 'Bar N not found') is exactly the class of bug this catches at
        # the source instead of several tool calls downstream.
        issues = self.spec_integrity_issues(spec)
        if issues:
            raise ValueError(
                "build_structure_from_spec REFUSED - invalid spec: "
                + "; ".join(issues)
                + (". Fix the spec (call get_model_geometry or "
                   "preview_structure_geometry to confirm real ids), or "
                   "build incrementally with create_node/create_bar in "
                   "smaller sub-specs."))

        if str(spec.get("project") or "3D").lower() == "2d":
            self.new_2d_frame()
        else:
            self.new_3d_frame()

        for n in spec.get("nodes", []) or []:
            self.create_node(
                int(n["id"]),
                float(n.get("x", 0.0)), float(n.get("y", 0.0)),
                float(n.get("z", 0.0)),
            )

        for b in spec.get("bars", []) or []:
            self.create_bar(
                int(b["id"]), int(b["n1"]), int(b["n2"]),
                str(b.get("section") or "IPE 200"),
            )

        # [INTEGRITY] The build MUST create exactly the bars the spec asked
        # for. Any mismatch is a hard error, surfaced HERE, not as a
        # confusing 'Bar N not found' several tool calls later.
        requested_bars = len(spec.get("bars", []) or [])
        actual_bars = len(self._bar_endpoints)
        if actual_bars != requested_bars:
            raise RuntimeError(
                f"build_structure_from_spec created {actual_bars} bars but "
                f"the spec requested {requested_bars}. This usually means a "
                "duplicate bar id or a Robot-side create failure. Call "
                "robot_session_status to check for a session split, and "
                "get_model_geometry to see what actually exists.")

        for s in spec.get("supports", []) or []:
            self.set_support(
                int(s["node"]), str(s.get("type") or "pinned"),
                spring_stiffness=s.get("spring_stiffness"),
            )

        for c in spec.get("cases", []) or []:
            nature = str(c.get("nature") or "permanent")
            self.create_load_case(
                int(c["id"]), str(c.get("name") or "Case"),
                nature=self._NATURE_MAP.get(nature, RobotEnum.I_CN_PERMANENT),
                analysis_type=RobotEnum.I_CAT_STATIC_LINEAR,
            )

        for ld in spec.get("loads", []) or []:
            kind = str(ld.get("kind") or "")
            case_id = int(ld["case"])
            if kind == "bar_uniform":
                self.apply_bar_load(
                    int(ld["bar"]), case_id,
                    float(ld.get("value", 0.0)), str(ld.get("direction", "Z")),
                )
            elif kind == "bar_concentrated":
                self.apply_bar_concentrated(
                    int(ld["bar"]), case_id,
                    fx_kn=float(ld.get("fx", 0.0)),
                    fy_kn=float(ld.get("fy", 0.0)),
                    fz_kn=float(ld.get("fz", 0.0)),
                    ratio=float(ld.get("ratio", 0.5)),
                )
            elif kind == "nodal":
                self.apply_nodal_load(
                    int(ld["node"]), case_id,
                    fx_kn=float(ld.get("fx", 0.0)),
                    fz_kn=float(ld.get("fz", 0.0)),
                    my_knm=float(ld.get("my", 0.0)),
                )
            else:
                logger.warning("Unknown load kind '%s' in spec; ignored.", kind)

        # [WP4] Optional 'materials' + 'panels' spec keys.
        for mt in spec.get("materials", []) or []:
            self.set_material(
                str(mt.get("name") or "STEEL"),
                e_mpa=mt.get("e_mpa"),
                nu=mt.get("nu"),
                apply_to_bars=bool(mt.get("apply_to_bars", True)),
            )
        for pnl in spec.get("panels", []) or []:
            self.create_panel(
                int(pnl["id"]),
                x=float(pnl.get("x", 0.0)),
                y=float(pnl.get("y", 0.0)),
                z=float(pnl.get("z", 0.0)),
                width=float(pnl.get("width", 4.0)),
                height=float(pnl.get("height", 3.0)),
                normal=str(pnl.get("normal", "Y")),
                divisions_x=int(pnl.get("divisions_x", 4)),
                divisions_z=int(pnl.get("divisions_z", 4)),
                section=str(pnl.get("section") or "IPE 100"),
                diagonals=bool(pnl.get("diagonals", False)),
            )

        summary = self.get_structure_summary()
        # [Part B] Scale-appropriateness safety net (pure, no COM): flag
        # egregious span/depth mismatches (e.g. a 1m beam on a fixed
        # "IPE 200"). Warnings only — never an exception. The agent can
        # inspect these and re-size the section if it wants to.
        summary["section_proportion_warnings"] = check_section_proportions(spec)
        section_notes = spec.get("__section_notes") or []
        if section_notes:
            summary["section_notes"] = section_notes
        summary["status"] = "ok"
        return summary

    @com_thread_safe
    def delete_bar(self, bar_id: int) -> str:
        """[WP2] Deletes an existing bar from the model."""
        self._ensure_connected()
        try:
            self.structure.Bars.Delete(bar_id)
        except Exception as exc:
            raise RuntimeError(
                f"Could not delete bar {bar_id}: {exc}"
            ) from exc
        self._section_assignments.pop(bar_id, None)
        self._bar_endpoints.pop(bar_id, None)
        self.bracing.remove(bar_id)
        self.connections.remove(bar_id)
        logger.info("Bar %s deleted.", bar_id)
        return f"Bar {bar_id} deleted."

    @com_thread_safe
    def delete_node(self, node_id: int) -> str:
        """
        [WP2] Deletes an existing node. Nodes with attached bars are refused
        by Robot — delete the connected bars first (use get_structure_summary
        to see the live model, including changes made manually in Robot).
        """
        self._ensure_connected()
        try:
            self.structure.Nodes.Delete(node_id)
        except Exception as exc:
            raise RuntimeError(
                f"Could not delete node {node_id} (it may still have bars "
                f"attached — delete those bars first): {exc}"
            ) from exc
        self._node_coords.pop(node_id, None)
        self._bar_endpoints = {
            b: ends for b, ends in self._bar_endpoints.items()
            if node_id not in ends
        }
        logger.info("Node %s deleted.", node_id)
        return f"Node {node_id} deleted."

    @com_thread_safe
    def save_project(self, file_path: str) -> str:
        """
        [WP3] Saves the current Robot project (.rtd) to an absolute
        user-specified path (parent folders are created if needed).
        """
        self._ensure_connected()
        path = os.path.abspath(file_path)
        if not path.lower().endswith(".rtd"):
            path += ".rtd"
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        try:
            self.project.SaveAs(path)
        except Exception as exc:
            raise RuntimeError(
                f"Robot could not save the project to '{path}': {exc}"
            ) from exc
        logger.info("Project saved to %s", path)
        return f"Project saved to '{path}'."

    # ------------------------------------------------------------------ #
    # Geometry read-back (in-memory, no COM) + self-weight loads
    # ------------------------------------------------------------------ #

    @com_thread_safe
    def get_model_geometry(self) -> Dict[str, Any]:
        """Returns the CURRENT in-memory geometry (no COM): project type,
        node coords and bar endpoints, exactly as the bridge has been
        building them. Used by the preview_structure_geometry tool so a
        wireframe can be drawn without contacting Robot."""
        return {
            "project": self._project_type,
            "nodes": {int(k): list(v) for k, v in self._node_coords.items()},
            "bars": {int(k): list(v) for k, v in self._bar_endpoints.items()},
        }

    @staticmethod
    def _self_weight_kn_m(unit_mass_kg_m: float, g: float = 9.81) -> float:
        """Gravity load per metre of a member from its unit mass (kg/m).
        Returns kN/m (positive downward, to be applied in global -Z)."""
        return float(unit_mass_kg_m) * float(g) / 1000.0

    @com_thread_safe
    def apply_self_weight(self, case_id: int, density: float = 7850.0) -> Dict[str, Any]:
        """Applies every bar's self-weight as EQUIVALENT NODAL loads in the
        given case (global -Z): each bar's weight (unit mass x length x g) is
        lumped 50/50 onto its two end nodes — the classic truss lumping.

        [2026-08-23 LIVE-FIX] Previously this wrote one bar-uniform load
        record per bar (I_LRT_BAR_UNIFORM, PZ). That path is verified exact
        on a single bar and on the planar 31-bar truss, but on a full 3D
        assembly (the 138-bar twin-arch) Robot's solver reported only
        125.69 kN of reactions for a verified 149.07 kN of records — a
        deterministic 15.7% shortfall that does NOT appear with nodal loads
        (same geometry + same total -> 149.07 kN reactions, 0.00% error,
        A/B verified live). Nodal lumping is the ONLY source of self-weight
        now: equilibrium is exact (sum(FZ) reactions == total computed),
        the tool's reported total is the number Robot actually applies, and
        no second/automatic self-weight mechanism is active in the case.
        Returns a per-bar summary dict plus the applied total.
        """
        self._ensure_connected()
        case = self.structure.Cases.Get(int(case_id))   # raises if missing

        per_bar: List[Dict[str, Any]] = []
        total_kn = 0.0
        node_loads: Dict[int, float] = {}
        for bar_id in sorted(self._bar_endpoints):
            length = self._bar_length(bar_id)
            if length <= 0.0:
                continue
            sec = self._section_assignments.get(bar_id) or ""
            unit_mass = self._lookup_unit_mass(sec, float(density)) \
                if sec else 0.0
            kn_m = self._self_weight_kn_m(unit_mass)
            weight = kn_m * length
            total_kn += weight
            n1, n2 = self._bar_endpoints[bar_id]
            node_loads[n1] = node_loads.get(n1, 0.0) + weight / 2.0
            node_loads[n2] = node_loads.get(n2, 0.0) + weight / 2.0
            per_bar.append({
                "bar_id": bar_id, "section": sec, "length_m": round(length, 3),
                "unit_mass_kg_m": round(unit_mass, 3),
                "load_kn_m": round(kn_m, 5),
                "weight_kn": round(weight, 4),
                "lumped_to": {"n1": int(n1), "n2": int(n2)},
            })
        # One nodal load per affected node (global -Z), exact by construction.
        for node_id in sorted(node_loads):
            w = node_loads[node_id]
            if abs(w) > 1e-9:
                self.apply_nodal_load(int(node_id), int(case_id),
                                      fx_kn=0.0, fz_kn=-w, my_knm=0.0)
        return {
            "case_id": int(case_id),
            "bars": len(per_bar),
            "applied_nodes": len([w for w in node_loads.values()
                                  if abs(w) > 1e-9]),
            "method": "nodal_lumped",
            "total_self_weight_kn": round(total_kn, 4),
            "density_kg_m3": float(density),
            "per_bar": per_bar,
        }


    # ------------------------------------------------------------------ #
    # Model spec export (reverse of build_structure_from_spec)
    # ------------------------------------------------------------------ #

    @com_thread_safe
    def export_structure_spec(self) -> Dict[str, Any]:
        """Reverse of build_structure_from_spec: reads the LIVE model and
        returns the same geometry JSON shape (project / nodes / bars /
        supports / cases / loads) so it can be passed verbatim to
        create_structure_from_spec or used as spec.geometry by the batch
        optimizer.

        Supports map back to the known type names via the AUTO_* label
        prefix (falling back to the fixity-flag pattern); elastic (spring)
        supports additionally carry spring_stiffness. Loads are read for
        simple static cases only (bar_uniform / bar_concentrated / nodal);
        combinations and exotic record types are skipped with a log note.
        """
        self._ensure_connected()
        spec: Dict[str, Any] = {
            "project": self._project_type,
            "nodes": [], "bars": [], "supports": [], "cases": [], "loads": [],
        }

        # ---- nodes ------------------------------------------------------ #
        try:
            node_coll = self.structure.Nodes.GetAll()
            if node_coll is not None:
                for i in range(1, int(node_coll.Count) + 1):
                    try:
                        n = node_coll.Get(i)
                        spec["nodes"].append({
                            "id": int(n.Number),
                            "x": float(n.X), "y": float(n.Y), "z": float(n.Z),
                        })
                    except Exception:
                        continue
        except Exception as exc:  # noqa: BLE001
            logger.warning("export_structure_spec: node read failed: %s", exc)

        # ---- bars ------------------------------------------------------- #
        try:
            bar_coll = self.structure.Bars.GetAll()
            if bar_coll is not None:
                for i in range(1, int(bar_coll.Count) + 1):
                    try:
                        b = bar_coll.Get(i)
                        sec = b.GetLabelName(RobotEnum.I_LT_BAR_SECTION) or ""
                        spec["bars"].append({
                            "id": int(b.Number),
                            "n1": int(b.StartNode), "n2": int(b.EndNode),
                            "section": sec,
                        })
                    except Exception:
                        continue
        except Exception as exc:  # noqa: BLE001
            logger.warning("export_structure_spec: bar read failed: %s", exc)

        # ---- supports --------------------------------------------------- #
        for node_id in [n["id"] for n in spec["nodes"]]:
            try:
                label_name = str(self.structure.Nodes.Get(node_id)
                                 .GetLabelName(RobotEnum.I_LT_SUPPORT) or "")
            except Exception:  # noqa: BLE001
                continue
            stype = self._support_type_from_label(label_name)
            if stype is None:
                continue  # node has no support label
            entry: Dict[str, Any] = {"node": int(node_id), "type": stype}
            if stype == "spring":
                stiffness = self._spring_stiffness_of(label_name)
                if stiffness:
                    entry["spring_stiffness"] = stiffness
            elif stype == "custom":
                entry["label"] = label_name
            spec["supports"].append(entry)

        # ---- cases + loads ---------------------------------------------- #
        for num, case in self._iter_all_cases():
            name = ""
            nature = "permanent"
            try:
                name = str(case.Name or "")
            except Exception:  # noqa: BLE001
                pass
            try:
                nat = int(case.Nature)
                nature = next(
                    (k for k, v in self._NATURE_MAP.items() if v == nat),
                    "permanent")
            except Exception:  # noqa: BLE001
                pass
            spec["cases"].append({"id": int(num), "name": name,
                                  "nature": nature})
            try:
                spec["loads"].extend(self._read_case_loads(case, int(num)))
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "export_structure_spec: load read failed for case %s: %s",
                    num, exc)
        return spec

    def _support_type_from_label(self, label_name: str) -> Optional[str]:
        """Maps a support label name back to a spec support_type (or None
        when the node carries no support). AUTO_* labels map by prefix;
        custom labels map by their fixity-flag pattern; anything unmatched
        is 'custom' (round-trip documented as a limitation)."""
        name = str(label_name or "").strip()
        if not name:
            return None
        if name.upper().startswith("AUTO_"):
            return name[5:].lower()
        try:
            support_label = self.structure.Labels.Get(
                RobotEnum.I_LT_SUPPORT, name)
            data = CastTo(support_label.Data, "IRobotNodeSupportData")
            flags = {dof: int(getattr(data, dof)) for dof in
                     ("UX", "UY", "UZ", "RX", "RY", "RZ")}
            for stype, pattern in self._SUPPORT_FLAG_SETS.items():
                if all(flags.get(d) == v for d, v in pattern.items()):
                    return stype
            try:
                if int(data.ElasticLinear) == 1:
                    return "spring"
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001
            pass
        return "custom"

    def _spring_stiffness_of(self, label_name: str) -> Dict[str, float]:
        """Reads an elastic (spring) support label's stiffness values back
        as {DOF: value} (translations in kN/m, rotations in kNm/rad)."""
        out: Dict[str, float] = {}
        try:
            support_label = self.structure.Labels.Get(
                RobotEnum.I_LT_SUPPORT, label_name)
            data = CastTo(support_label.Data, "IRobotNodeSupportData")
            if int(data.ElasticLinear) != 1:
                return out
            for dof, member in self._SPRING_VALUE_MEMBERS.items():
                try:
                    v = float(getattr(data, member))
                except Exception:  # noqa: BLE001
                    continue
                if abs(v) > 1e-12:
                    # [FIX 2026-08-23] RobotOM returns K*/H* in N/m / Nm/rad;
                    # the spec contract is kN/m / kNm/rad, so scale back.
                    out[dof] = round(v / 1000.0, 4)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not read spring stiffness '%s': %s",
                           label_name, exc)
        return out

    def _read_case_loads(self, case, case_id: int) -> List[Dict[str, Any]]:
        """Reads a simple case's load records into spec-style load dicts.
        Supports the three kinds the spec schema defines (bar_uniform /
        bar_concentrated / nodal); anything else is skipped with a note.

        [FIX 2026-08-23] Must CastTo IRobotSimpleCase to reach .Records -
        IRobotCase (what Cases.Get returns) has no Records attribute, so the
        old code logged "object has no attribute 'Records'" and silently
        returned zero loads (export_structure_spec always lost its loads)."""
        out: List[Dict[str, Any]] = []
        try:
            simple = CastTo(case, "IRobotSimpleCase")
            records = simple.Records
            n = int(records.Count)
        except Exception as exc:  # noqa: BLE001
            logger.warning("_read_case_loads: case %s has no readable "
                           "Records: %s", case_id, exc)
            return out

        for i in range(1, n + 1):
            try:
                record = records.Get(i)
                rtype = int(record.Type)
                objs = self._record_object_ids(record)
            except Exception as exc:  # noqa: BLE001
                logger.warning("_read_case_loads: record %s unreadable: %s",
                               i, exc)
                continue
            try:
                if rtype == RobotEnum.I_LRT_BAR_UNIFORM:
                    vals = [float(record.GetValue(j)) for j in (0, 1, 2)]
                    axis = next((a for a, v in zip(("X", "Y", "Z"), vals)
                                 if abs(v) > 1e-12), None)
                    if axis is not None:
                        for bar_id in objs:
                            out.append({
                                "kind": "bar_uniform", "bar": bar_id,
                                "case": case_id, "direction": axis,
                                "value": round(vals["XYZ".index(axis)], 5),
                            })
                elif rtype == RobotEnum.I_LRT_NODE_FORCE:
                    vals = [float(record.GetValue(j)) for j in range(6)]
                    for node_id in objs:
                        out.append({
                            "kind": "nodal", "node": node_id, "case": case_id,
                            "fx": round(vals[0], 5), "fz": round(vals[2], 5),
                            "my": round(vals[4], 5),
                        })
                elif rtype == RobotEnum.I_LRT_BAR_FORCE_CONCENTRATED:
                    fx = float(record.GetValue(RobotEnum.I_BFCRV_FX))
                    fy = float(record.GetValue(RobotEnum.I_BFCRV_FY))
                    fz = float(record.GetValue(RobotEnum.I_BFCRV_FZ))
                    try:
                        ratio = float(record.GetValue(RobotEnum.I_BFCRV_REL))
                    except Exception:  # noqa: BLE001
                        ratio = 0.5
                    for bar_id in objs:
                        out.append({
                            "kind": "bar_concentrated", "bar": bar_id,
                            "case": case_id, "fx": round(fx, 5),
                            "fy": round(fy, 5), "fz": round(fz, 5),
                            "ratio": round(ratio, 4),
                        })
                else:
                    logger.info(
                        "_read_case_loads: record type %s not in the spec "
                        "schema (skipped).", rtype)
            except Exception as exc:  # noqa: BLE001
                logger.warning("_read_case_loads: record %s value read "
                               "failed: %s", i, exc)
        return out

    def _record_object_ids(self, record) -> List[int]:
        """Bar/node ids a load record applies to, from its Objects range.
        Tries the Text property (inverse of FromText), then Count/Get.

        [FIX 2026-08-23] Live-verified: record.Objects.Get(k) returns the
        RAW int id directly (not an object with .Number), and Objects.Text
        does not exist on this build. The old code did rng.Get(k).Number,
        which raised AttributeError, was swallowed, and returned [] - so
        _read_case_loads silently produced ZERO loads every time."""
        try:
            text = str(record.Objects.Text or "")
            ids = [int(t) for t in re.findall(r"\d+", text)]
            if ids:
                return ids
        except Exception:  # noqa: BLE001
            pass
        try:
            rng = record.Objects
            n = int(rng.Count)
            ids = []
            for k in range(1, n + 1):
                item = rng.Get(k)
                if isinstance(item, int):
                    ids.append(item)
                    continue
                try:
                    ids.append(int(item.Number))
                except Exception:  # noqa: BLE001
                    try:
                        ids.append(int(item))
                    except Exception:  # noqa: BLE001
                        continue
            return ids
        except Exception:  # noqa: BLE001
            return []

    @com_thread_safe
    def get_structure_summary(self) -> Dict[str, Any]:
        """
        Compact summary of the CURRENT model. [WP2] Enumerates LIVE from
        Robot (nodes with coordinates, bars with their assigned section
        labels, case count) so manual edits/deletions made in the Robot
        window are reflected. Falls back to in-memory bookkeeping if live
        enumeration fails for any reason.
        """
        self._ensure_connected()

        nodes = 0
        xs: List[float] = []
        ys: List[float] = []
        zs: List[float] = []
        bars = 0
        sections: Dict[str, int] = {}
        live_ok = True

        try:
            node_coll = self.structure.Nodes.GetAll()
            if node_coll is not None:
                for i in range(1, int(node_coll.Count) + 1):
                    try:
                        n = node_coll.Get(i)
                        nodes += 1
                        xs.append(float(n.X))
                        ys.append(float(n.Y))
                        zs.append(float(n.Z))
                    except Exception:
                        continue
        except Exception:
            live_ok = False

        try:
            bar_coll = self.structure.Bars.GetAll()
            if bar_coll is not None:
                for i in range(1, int(bar_coll.Count) + 1):
                    try:
                        b = bar_coll.Get(i)
                        bars += 1
                        name = b.GetLabelName(RobotEnum.I_LT_BAR_SECTION)
                        if name:
                            sections[name] = sections.get(name, 0) + 1
                    except Exception:
                        continue
        except Exception:
            live_ok = False

        if not live_ok or nodes == 0:
            # Fallback: bookkeeping mirrors what the bridge created.
            nodes = len(self._node_coords)
            bars = len(self._bar_endpoints)
            xs = [c[0] for c in self._node_coords.values()] or [0.0]
            ys = [c[1] for c in self._node_coords.values()] or [0.0]
            zs = [c[2] for c in self._node_coords.values()] or [0.0]
            sections = {}
            for name in self._section_assignments.values():
                sections[name] = sections.get(name, 0) + 1

        case_count = 0
        try:
            coll = self.structure.Cases.GetAll()
            case_count = int(coll.Count) if coll is not None else 0
        except Exception:
            pass

        return {
            "nodes": nodes,
            "bars": bars,
            "cases": case_count,
            "bbox": {
                "x": [round(min(xs), 3), round(max(xs), 3)] if xs else [0, 0],
                "y": [round(min(ys), 3), round(max(ys), 3)] if ys else [0, 0],
                "z": [round(min(zs), 3), round(max(zs), 3)] if zs else [0, 0],
            },
            "sections": sections,
        }

    # --- [TEMPLATES] ---
    # ------------------------------------------------------------------ #
    # Milestone A: common-structure templates -> spec -> build
    # ------------------------------------------------------------------ #

    @staticmethod
    def grid_frame_spec(
        levels: int = 2,
        bays_x: int = 2,
        bays_y: int = 2,
        bay_width_x: float = 5.0,
        bay_width_y: float = 5.0,
        level_height: float = 3.5,
        column_section: str = None,
        beam_x_section: str = None,
        beam_y_section: str = None,
    ) -> Dict[str, Any]:
        """Spec for a 3D rectangular grid moment frame (columns + floor beams
        with pinned column bases).

        Sections default to scale-appropriate auto-sizes via
        ``suggest_section`` (columns slenderness-sized on the storey height,
        beams span/18 on their bay width) whenever not given explicitly;
        explicit sections always win.
        """
        levels = max(1, int(levels))
        bx = max(1, int(bays_x))
        by = max(1, int(bays_y))
        notes: List[str] = []
        column_section = column_section or suggest_section(
            "column", float(level_height), "HEB", notes=notes)
        beam_x_section = beam_x_section or suggest_section(
            "beam", float(bay_width_x), "IPE", notes=notes)
        beam_y_section = beam_y_section or suggest_section(
            "beam", float(bay_width_y), "IPE", notes=notes)
        xs = [i * bay_width_x for i in range(bx + 1)]
        ys = [j * bay_width_y for j in range(by + 1)]
        zs = [l * level_height for l in range(levels + 1)]

        def nid(ix, iy, lev):
            return ((ix) * (by + 1) + iy) * (levels + 1) + lev + 1

        nodes = []
        for ix in range(bx + 1):
            for iy in range(by + 1):
                for lev in range(levels + 1):
                    nodes.append({
                        "id": nid(ix, iy, lev),
                        "x": round(xs[ix], 4), "y": round(ys[iy], 4),
                        "z": round(zs[lev], 4),
                    })

        bars = []
        bid = 0

        def B(n1, n2, section):
            nonlocal bid
            bid += 1
            bars.append({"id": bid, "n1": n1, "n2": n2, "section": section})

        for ix in range(bx + 1):
            for iy in range(by + 1):
                for lev in range(levels):
                    B(nid(ix, iy, lev), nid(ix, iy, lev + 1), column_section)
        for lev in range(1, levels + 1):
            for iy in range(by + 1):
                for ix in range(bx):
                    B(nid(ix, iy, lev), nid(ix + 1, iy, lev), beam_x_section)
        for lev in range(1, levels + 1):
            for ix in range(bx + 1):
                for iy in range(by):
                    B(nid(ix, iy, lev), nid(ix, iy + 1, lev), beam_y_section)

        supports = [{"node": nid(ix, iy, 0), "type": "pinned"}
                    for ix in range(bx + 1) for iy in range(by + 1)]
        spec = {"project": "3D", "nodes": nodes, "bars": bars,
                "supports": supports, "__tpl": "rectangular_grid_frame"}
        if notes:
            spec["__section_notes"] = notes
        return spec

    @staticmethod
    def truss_spec(span: float = 12.0, height: float = 2.0, panels: int = 6,
                   top_section: str = None, bottom_section: str = None,
                   web_section: str = None) -> Dict[str, Any]:
        """Spec for a planar Pratt truss in the X-Z plane (y = 0), top and
        bottom chords joined by verticals and diagonals, pinned at both ends.

        A PRE-COMPOSED RECIPE over the composable primitives
        (generate_straight_chord + connect_web_pattern + apply_support_pattern)
        — kept byte-identical to the historical output by
        test_legacy_byte_identity so existing tests / run history are stable.

        Sections default to scale-appropriate auto-sizes via
        ``suggest_section`` (chords span/18, light angle web) whenever a
        section is not given explicitly; explicit sections always win and
        are never overridden.
        """
        n = max(2, int(panels))
        notes: List[str] = []
        top_section = top_section or suggest_section(
            "truss_chord", span, "IPE", notes=notes)
        bottom_section = bottom_section or suggest_section(
            "truss_chord", span, "IPE", notes=notes)
        web_section = web_section or suggest_section(
            "web", span, "L", notes=notes)

        # Flat chords via the composable primitives. ``xs`` is precomputed
        # so node x-coordinates match the historical ``round(i*dx, 6)``
        # layout exactly (the index k is reconstructed from t).
        xs = [round(i * span / n, 6) for i in range(n + 1)]

        def top_fn(t: float):
            k = min(n, max(0, int(round(t * n))))
            return (xs[k], 0.0, float(height))

        def bot_fn(t: float):
            k = min(n, max(0, int(round(t * n))))
            return (xs[k], 0.0, 0.0)

        top = nodes_along_curve(top_fn, n + 1, start_id=1)
        bot = nodes_along_curve(bot_fn, n + 1, start_id=n + 2)
        bars = connect_web_pattern(
            top, bot, "pratt", web_section=web_section,
            chord_a_section=top_section, chord_b_section=bottom_section,
            start_id=1)
        spec = {"project": "3D", "nodes": top + bot, "bars": bars,
                "supports": apply_support_pattern(
                    [bot[0]["id"], bot[-1]["id"]], "pinned"),
                "__tpl": "truss"}
        if notes:
            spec["__section_notes"] = notes
        return spec

    @staticmethod
    def braced_frame_spec(height: float = 6.0, width: float = 6.0,
                          column_section: str = None,
                          beam_section: str = None,
                          brace_section: str = None) -> Dict[str, Any]:
        """Spec for a single-bay braced frame (two columns, top beam, and a
        diagonal brace) in the X-Z plane, pinned bases.

        Sections default to scale-appropriate auto-sizes via
        ``suggest_section`` (columns slenderness-sized on the storey height,
        beam span/18 on the bay width, brace on the diagonal length) whenever
        not given explicitly; explicit sections always win.
        """
        notes: List[str] = []
        column_section = column_section or suggest_section(
            "column", float(height), "HEB", notes=notes)
        beam_section = beam_section or suggest_section(
            "beam", float(width), "IPE", notes=notes)
        brace_section = brace_section or suggest_section(
            "brace", float(math.hypot(width, height)), "IPE", notes=notes)
        nodes = [
            {"id": 1, "x": 0.0, "y": 0.0, "z": 0.0},
            {"id": 2, "x": width, "y": 0.0, "z": 0.0},
            {"id": 3, "x": 0.0, "y": 0.0, "z": height},
            {"id": 4, "x": width, "y": 0.0, "z": height},
        ]
        bars = [
            {"id": 1, "n1": 1, "n2": 3, "section": column_section},
            {"id": 2, "n1": 2, "n2": 4, "section": column_section},
            {"id": 3, "n1": 3, "n2": 4, "section": beam_section},
            {"id": 4, "n1": 1, "n2": 4, "section": brace_section},
        ]
        spec = {"project": "3D", "nodes": nodes, "bars": bars,
                "supports": [{"node": 1, "type": "pinned"},
                             {"node": 2, "type": "pinned"}],
                "__tpl": "braced_frame"}
        if notes:
            spec["__section_notes"] = notes
        return spec

    @staticmethod
    def cylindrical_tank_spec(
        radius: float = 2.5,
        height: float = 5.0,
        segments: int = 16,
        ring_levels: int = 2,
        section_vertical: str = None,
        section_ring: str = None,
    ) -> Dict[str, Any]:
        """
        [TANK-FIX] Spec for a FACETED CYLINDRICAL water-tank frame — a
        circular ring of `segments` nodes at each of `ring_levels` heights,
        connected by vertical columns and circumferential ring beams, with
        pinned base supports. This gives a true cylindrical geometry (not a
        square box). Ring levels count includes the base and top.

        The ring geometry is built with the shared ``radial_ring`` primitive
        (constant radius -> cylinder). Sections default to scale-appropriate
        auto-sizes via ``suggest_section`` (vertical members on the tank
        height, ring members on the tank diameter) whenever not given
        explicitly; explicit sections always win.
        """
        segs = max(6, int(segments))
        rings = max(2, int(ring_levels))
        r = float(radius)
        h = float(height)
        notes: List[str] = []
        section_vertical = section_vertical or suggest_section(
            "beam", h, "IPE", notes=notes)
        section_ring = section_ring or suggest_section(
            "beam", 2.0 * r, "IPE", notes=notes)

        def center_fn(ratio: float):
            # Reconstruct the historical ring index from the ratio so node
            # ids and z-coordinates stay byte-identical to the old code.
            k = int(round(ratio * (rings - 1)))
            return (0.0, 0.0, round(h * k / (rings - 1), 6))

        nodes = radial_ring(center_fn, lambda ratio: r, segs, rings,
                            start_id=1)

        def nid(ring: int, seg: int) -> int:
            return ring * segs + seg + 1

        bars = []
        bid = 0

        def B(n1, n2, section):
            nonlocal bid
            bid += 1
            bars.append({"id": bid, "n1": n1, "n2": n2, "section": section})

        # vertical columns between consecutive rings
        for ring in range(rings - 1):
            for seg in range(segs):
                B(nid(ring, seg), nid(ring + 1, seg), section_vertical)
        # circumferential ring beams (closing the polygon at each level)
        for ring in range(rings):
            for seg in range(segs):
                B(nid(ring, seg), nid(ring, (seg + 1) % segs), section_ring)

        supports = [{"node": nid(0, seg), "type": "pinned"} for seg in range(segs)]
        spec = {"project": "3D", "nodes": nodes, "bars": bars,
                "supports": supports, "__tpl": "cylindrical_tank"}
        if notes:
            spec["__section_notes"] = notes
        return spec

    def create_cylindrical_tank(self, **kwargs) -> Dict[str, Any]:
        return self.build_structure_from_spec(
            self.cylindrical_tank_spec(**kwargs))

    def create_rectangular_grid_frame(self, **kwargs) -> Dict[str, Any]:
        return self.build_structure_from_spec(self.grid_frame_spec(**kwargs))

    def create_truss(self, **kwargs) -> Dict[str, Any]:
        return self.build_structure_from_spec(self.truss_spec(**kwargs))

    def create_braced_frame(self, **kwargs) -> Dict[str, Any]:
        return self.build_structure_from_spec(self.braced_frame_spec(**kwargs))

    @staticmethod
    def arch_truss_spec(
        span: float = 30.0,
        rise: float = 5.0,
        panels: int = 10,
        top_section: str = None,
        bottom_section: str = None,
        web_section: str = None,
        arch_chord: str = "top",
    ) -> Dict[str, Any]:
        """Spec for a planar arch truss in the X-Z plane (y = 0), pinned at
        both bottom ends.

        The arched chord follows a circular arc (``circular_arc_fn``) rising
        from 0 to ``rise`` at mid-span; the other chord is straight.
        ``arch_chord`` selects which chord is arched:

          "top"    -> top chord arched, bottom chord straight on z=0
                       (classic bowstring / tied-arch truss)
          "bottom" -> bottom chord arched, top chord straight at z=rise
                       (arch bridge with a straight deck above the arch)

        The two chains are produced by the composable primitives
        (``generate_arc_chord`` for the arched chord,
        ``generate_straight_chord`` for the straight one, then
        ``connect_web_pattern``) — a PRE-COMPOSED RECIPE kept byte-identical
        to the historical output by ``test_legacy_byte_identity``. Sections
        default to scale-appropriate auto-sizes via ``suggest_section``
        (chords span/18, light angle web) whenever not given explicitly.
        """
        n = max(2, int(panels))
        rise = max(float(rise), 0.0)
        notes: List[str] = []
        top_section = top_section or suggest_section(
            "truss_chord", span, "IPE", notes=notes)
        bottom_section = bottom_section or suggest_section(
            "truss_chord", span, "IPE", notes=notes)
        web_section = web_section or suggest_section(
            "web", span, "L", notes=notes)

        if str(arch_chord).lower() == "bottom":
            # Arched bottom chord + straight top chord (deck above the arch).
            bot = generate_arc_chord(float(span), rise, n, elevation=0.0,
                                     arch="up", section=bottom_section,
                                     start_id=n + 2)
            top = generate_straight_chord(float(span), n, elevation=rise,
                                          section=top_section, start_id=1)
        else:
            # Straight bottom chord + arched top chord (bowstring).
            top = generate_arc_chord(float(span), rise, n, elevation=0.0,
                                     arch="up", section=top_section,
                                     start_id=1)
            bot = generate_straight_chord(float(span), n, elevation=0.0,
                                          section=bottom_section,
                                          start_id=n + 2)

        bars = connect_web_pattern(
            top, bot, "pratt", web_section=web_section,
            chord_a_section=top_section, chord_b_section=bottom_section,
            start_id=1)
        spec = {"project": "3D", "nodes": top["nodes"] + bot["nodes"],
                "bars": bars,
                "supports": apply_support_pattern(
                    [bot["first"], bot["last"]], "pinned"),
                "__tpl": "arch_truss"}
        if notes:
            spec["__section_notes"] = notes
        return spec

    def create_arch_truss(self, **kwargs) -> Dict[str, Any]:
        return self.build_structure_from_spec(self.arch_truss_spec(**kwargs))


# --------------------------------------------------------------------------
# Convenience factory used by the agent layer (app.py) so each Streamlit
# session gets its own bridge instance stored in st.session_state.
# --------------------------------------------------------------------------

def get_robot_bridge() -> RobotBridge:
    return RobotBridge()
