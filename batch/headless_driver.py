"""
batch/headless_driver.py
========================
Headless batch driver for Robot Structural Analysis.

`HeadlessSession` wraps the verified low-level primitives in
tools.robot_tool.RobotBridge so batch candidates can be built, solved and
scored with no Streamlit session, no ToolExecutor, no LLM, and no chat loop.

FIRM BOUNDARY: `connect()` ALWAYS launches a fresh Robot instance
(``new_instance=True`` is hardcoded) and NEVER attaches to a running one via
GetActiveObject. A batch session must never touch an interactive user's live
Robot window. Because we always own the instance we launch, `close()` can
always Quit() — no attach/ownership bookkeeping is needed or carried.

Phase-0 verified context:
  * Every public RobotBridge method is standalone-safe (no Streamlit import
    chain in tools/; the @com_thread_safe decorator self-initializes COM per
    calling thread; RLock is re-entrant).
  * visible=False is UNTESTED in the codebase — this module probes it.
  * One RobotBridge must be created AND used on the same thread (COM
    apartment affinity). Batch runs are single-threaded by design.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from typing import Any

from tools.robot_tool import RobotBridge, RobotEnum
from tools.win_dialogs import (
    _click_button,
    _enum_windows,
    _is_dialog_like,
    _robot_pids,
    _window_text,
)

logger = logging.getLogger("structural_copilot.batch.headless_driver")

#: Analysis types HeadlessSession can run. Response-spectrum is confirmed to
#: not exist anywhere in the codebase, so it is deliberately NOT offered.
SUPPORTED_ANALYSIS_TYPES = ("static", "modal")


class MechanismError(RuntimeError):
    """Raised by solve_all() when validate_stability() finds a likely
    mechanism, so Calculate() is never reached (the solver's instability
    modal — which Interactive=0 suppresses but cannot ANSWER — is avoided
    entirely)."""


class SolverInstabilityError(RuntimeError):
    """A KNOWN solver dialog was auto-dismissed (e.g. the instability modal
    answered with its safe 'No' button). The solve is a clean FAILURE,
    not a success, and the session remains usable."""


class UnknownDialogError(RuntimeError):
    """An UNRECOGNIZED popup appeared during solve; the session's Robot
    process was force-terminated and the exact title logged as the seed
    for a new dialog pattern."""


#: In-plane DOF names per node for the 2D mechanism check (X, Z, rot-Y).
_DOF_NAMES_2D = ("UX", "UZ", "RY")


#: Substring that identifies Robot's main window (not a dialog).
_MAIN_WINDOW_MARKER = "robot structural analysis professional"

#: Known dialog title substrings -> safe action. Start with the confirmed
#: instability modal; add more as they are discovered later in the build.
DEFAULT_DIALOG_PATTERNS: dict[str, dict[str, str]] = {
    "instabilit": {"action": "click", "button_text": "No"},
    # Benign post-solve informational window (found live: appears after a
    # calculation that produced messages/warnings). It is NOT a failure -
    # clicking Close dismisses it and the solve result stands.
    "calculation messages": {"action": "click", "button_text": "Close", "benign": True},
    # Robot's "Do you want to save changes to Structure?" modal that appears
    # when Project.New()/close discards a project with results (Interactive=1),
    # or when one client's actions collide with another's session. For a batch
    # session the ONLY deterministic action is "No" (never save). This is the
    # SAME pattern the interactive path uses (tools.win_dialogs.
    # SAVE_PROMPT_PATTERNS); keeping it here makes dismiss behaviour
    # deterministic in BOTH paths, instead of the headless watcher treating
    # the identical dialog as UNKNOWN and force-killing its own robot (the
    # 2026-08-22 ~19:08 split-session root cause).
    "save changes to structure": {"action": "click", "button_text": "No"},
}


#: Generic markers that distinguish a modal prompt from benign tool/#: progress windows when no specific pattern matches.
_DIALOG_MARKERS = (
    "instabilit",
    "continue",
    "warning",
    "error",
    "question",
    "confirm",
    "do you want",
)


class HeadlessSession:
    """
    Owns one RobotBridge in headless mode.

    Usage::

        with HeadlessSession(visible=False) as s:
            s.build_from_spec(spec)
            s.solve_all(["static"])
            print(s.get_weight(), s.get_utilization_summary())
    """

    def __init__(self, visible: bool = False, solve_timeout_s: float = 90.0):
        self._visible = bool(visible)
        self.solve_timeout_s = float(solve_timeout_s)
        self.dialog_patterns: dict[str, dict[str, str]] = dict(DEFAULT_DIALOG_PATTERNS)
        self._bridge: RobotBridge | None = None
        # PIDs of robot.exe processes THIS session launched (for a
        # deterministic close: Quit() is asynchronous, so we poll and, only
        # if a process we own still lingers, taskkill it — never an
        # interactive instance's PID).
        self._owned_pids: set[int] = set()

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def connect(self) -> None:
        """Launches a FRESH Robot instance (never attaches to a running one).

        visible=False is intentionally supported and probed by
        test_headless_driver.py — if the solver misbehaves invisibly on this
        Robot build, the probe report will say so and this default can be
        revisited per that evidence (not assumed).
        """
        if self._bridge is not None:
            return
        bridge = RobotBridge()
        pids_before = _robot_pids()
        # FIRM BOUNDARY: new_instance=True hardcoded — batch must never
        # attach to an interactive session's Robot.
        bridge.connect(visible=self._visible, new_instance=True)
        # [PHASE-1 DIALOG FIX] Suppress Robot's modal prompts for headless
        # runs. RobotBridge.connect() sets Interactive=1 (correct for the
        # interactive app), which lets Robot pop blocking dialogs — e.g. a
        # "save changes?" prompt during Quit() — that stall a batch run
        # until clicked. Interactive=0 is Robot OM's documented switch to
        # run without user interference.
        try:
            bridge.robot_app.Interactive = 0
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not set Robot Interactive=0: %s", exc)
        self._bridge = bridge
        self._owned_pids = _robot_pids() - pids_before
        logger.info(
            "HeadlessSession connected (visible=%s, own instance, Interactive=0).", self._visible
        )

    def is_alive(self) -> bool:
        """True if the session's Robot process AND COM bridge are healthy.

        Two checks, both must pass:
          1. Process-level: at least one owned PID is still in tasklist.
          2. COM-level: the bridge reports its server responsive (probes
             .Project). The PID check alone is insufficient right after the
             DialogWatcher/timeout force-kill - tasklist can briefly still
             list a terminating PID, so the runner would wrongly reuse a dead
             session. bridge.is_alive() does the authoritative COM probe.
        """
        if self._bridge is None:
            return False
        if not (self._owned_pids & _robot_pids()):
            return False
        try:
            return bool(self._bridge.is_alive())
        except Exception:  # noqa: BLE001
            return False

    def reconnect(self) -> None:
        """Closes this session (force-terminating owned PIDs if they linger)
        and launches a fresh Robot instance.

        Used by the batch runner after a DialogWatcher force-kill
        (UnknownDialogError) or the solve() timeout: the old process is gone,
        so the session must be rebuilt from scratch rather than reused."""
        logger.warning(
            "HeadlessSession.reconnect() — closing dead session "
            "and launching a fresh Robot instance."
        )
        self.close()
        self.connect()

    def clear_structure(self, project_type: str = "3D") -> None:
        """Thin wrapper: resets the current project to a blank model of the
        given type ('2D' or '3D'), clearing all in-memory bookkeeping."""
        self.bridge.clear_structure(project_type)

    @property
    def bridge(self) -> RobotBridge:
        if self._bridge is None:
            raise RuntimeError("HeadlessSession is not connected — call connect() first.")
        return self._bridge

    def close(self) -> None:
        """Quits our own Robot instance and waits (bounded) for it to exit.

        Quit(0) is asynchronous on this Robot build — the process can outlive
        the call by a few seconds (observed in the Phase-1 5x-loop probe).
        To make "closed" deterministic for the batch runner, we poll for the
        PIDs we launched to disappear, then force-kill ONLY those PIDs if
        they linger. An interactive instance's process is never touched.
        """
        bridge = self._bridge
        self._bridge = None
        if bridge is None:
            return
        owned = self._owned_pids
        self._owned_pids = set()
        bridge.close()  # Quit(0) — asynchronous on this build
        # Quit(0) is asynchronous: robot.exe takes ~1-4 s to fully exit, and
        # tasklist may briefly NOT list a terminating process (a single
        # "gone" sample is a race). Require TWO consecutive clean samples
        # before declaring the process closed; force-kill only our PIDs if
        # they outlive the grace window.
        deadline = time.time() + 20.0
        gone = 0
        while time.time() < deadline and owned:
            if not (owned & _robot_pids()):
                gone += 1
                if gone >= 2:
                    owned = set()
                    break
            else:
                gone = 0
            time.sleep(0.75)
        for pid in owned:  # still alive after the grace window
            try:
                subprocess.run(
                    ["taskkill", "/F", "/PID", str(pid)], capture_output=True, timeout=15
                )
                logger.warning(
                    "Force-killed lingering robot.exe PID %s (owned by this session).", pid
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not force-kill robot.exe PID %s: %s", pid, exc)
        logger.info("HeadlessSession closed.")

    def __enter__(self) -> HeadlessSession:
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # Model building
    # ------------------------------------------------------------------ #

    def new_2d_frame(self) -> None:
        self.bridge.new_2d_frame()

    def build_from_spec(self, spec: dict[str, Any]) -> dict[str, Any]:
        """Thin wrapper around RobotBridge.build_structure_from_spec.

        Confirmed spec schema (Phase 0): project ("2D"/"3D"), nodes
        [{id,x,y,z}], bars [{id,n1,n2,section}], supports [{node,type}],
        cases [{id,name,nature}], loads [{kind,bar/node,case,direction,
        value}|{fx,fy,fz,ratio}|{fx,fz,my}], plus optional materials and
        panels. Gap for Phase 4's design space: bars have NO per-bar
        `material` key (materials are applied via the top-level `materials`
        list with apply_to_bars=True) — flagged in the Phase-1 report.
        """
        return self.bridge.build_structure_from_spec(spec)

    # ------------------------------------------------------------------ #
    # [STEP 2] Pre-solve stability validation (defense in depth)
    # ------------------------------------------------------------------ #

    @staticmethod
    def _support_flags_2d(support_name: str) -> tuple[int, int, int]:
        """(UX, UZ, RY) fixity flags for a support label name. Mirrors
        RobotBridge._SUPPORT_FLAG_SETS; unknown labels are treated as full
        fixity (conservative for mechanism detection)."""
        name = str(support_name or "").upper()
        for marker, flags in (("PINNED", (1, 1, 0)), ("ROLLER", (0, 1, 0)), ("FIXED", (1, 1, 1))):
            if marker in name:
                return flags
        return (1, 1, 1)

    def _section_a_i(self, section_name: str) -> tuple[float, float]:
        """(A, I) for a section label via the empirical GetValue map
        (0=A, 4/5=I — probed live in Phase 2). Falls back to unit values;
        exact magnitudes don't affect singularity detection."""
        try:
            data = self.bridge.structure.Labels.Get(
                RobotEnum.I_LT_BAR_SECTION, str(section_name)
            ).Data
            a = float(data.GetValue(0))
            i = min(float(data.GetValue(4)), float(data.GetValue(5)))
            if a > 0.0 and i > 0.0:
                return a, i
        except Exception:  # noqa: BLE001
            pass
        return 1.0, 1.0

    def validate_stability(self) -> dict[str, Any]:
        """
        [STEP 2] Detects likely kinematic mechanisms BEFORE Calculate()
        is ever called. DELEGATES to RobotBridge.validate_stability(),
        the single source of truth (batch/runner and the chat tool
        check_model_stability share the same 2D rank check).
        """
        return self.bridge.validate_stability()

    def _solve_with_timeout(self) -> None:
        """
        [STEP 3] Runs CalcEngine.Calculate() on the calling (COM-bound)
        thread with TWO independent safety nets:

        1. DialogWatcher: polls this session's own Robot PID for popup
           windows during Calculate(). A KNOWN dialog (e.g. the
           "Instability ... Do you want to continue?" modal, which
           Interactive=0 suppresses but cannot ANSWER) is auto-dismissed
           by clicking its safe button ("No") and the solve is reported as
           FAILED (SolverInstabilityError) - not success. An UNKNOWN popup
           force-terminates the owned processes (UnknownDialogError) and
           logs the exact title as the seed for a new pattern.
        2. Hard timeout: if the solver overruns (the watcher missed a
           dialog, or a genuinely long solve), the owned Robot processes
           are force-terminated and TimeoutError is raised.

        Calculate() must stay on the bridge's thread (COM apartment
        affinity); both watchers only do process/window-level work.
        """
        bridge = self.bridge
        owned = set(self._owned_pids)
        pids = sorted(owned) if owned else sorted(_robot_pids())
        killed = threading.Event()
        timed_out = threading.Event()
        dlg: dict[str, Any] = {"outcome": None, "title": None, "button": None}
        dlg_lock = threading.Lock()

        def _dialog_watcher():
            deadline = time.time() + self.solve_timeout_s + 10.0
            while time.time() < deadline and not killed.is_set():
                try:
                    for hwnd, title, cls in _enum_windows(pids):
                        if (cls or "").lower() == "robobatrobot97":
                            continue  # Robot main window (class, not title:
                            # the instability modal's own title is just the
                            # generic app name, so class is the safe signal)
                        text = _window_text(hwnd)
                        low = text.lower()
                        if not low:
                            continue
                        matched = None
                        for key, spec in self.dialog_patterns.items():
                            if key in low:
                                matched = spec
                                break
                        if matched is not None:
                            bt = str(matched.get("button_text", ""))
                            clicked = _click_button(hwnd, bt)
                            benign = bool(matched.get("benign", False))
                            with dlg_lock:
                                dlg["outcome"] = "dismissed_benign" if benign else "clicked"
                                dlg["title"] = title or text[:80]
                                dlg["button"] = bt
                            logger.warning(
                                "DialogWatcher %s %r (button=%r, clicked=%s)",
                                "auto-dismissed benign" if benign else "auto-dismissed",
                                dlg["title"],
                                bt,
                                clicked,
                            )
                            # Keep watching: Robot may raise more than one
                            # dialog per Calculate().
                            time.sleep(1.0)
                            continue
                        if not _is_dialog_like(low):
                            continue  # benign tool/progress window, ignore
                        with dlg_lock:
                            dlg["outcome"] = "killed_unknown"
                            dlg["title"] = title or text[:80]
                        logger.error(
                            "DialogWatcher: UNKNOWN dialog %r - force-terminating owned pids %s",
                            dlg["title"],
                            pids,
                        )
                        for pid in owned:
                            try:
                                subprocess.run(
                                    ["taskkill", "/F", "/PID", str(pid)],
                                    capture_output=True,
                                    timeout=15,
                                )
                            except Exception as exc:  # noqa: BLE001
                                logger.warning("Could not kill PID %s: %s", pid, exc)
                        return
                except Exception as exc:  # noqa: BLE001
                    logger.debug("DialogWatcher poll error: %s", exc)
                time.sleep(0.25)

        def _timeout_watcher():
            time.sleep(max(0.1, self.solve_timeout_s))
            if killed.is_set():
                return
            timed_out.set()
            logger.error(
                "Robot solve exceeded %.1fs - force-terminating owned pids %s",
                self.solve_timeout_s,
                pids,
            )
            for pid in owned:
                try:
                    subprocess.run(
                        ["taskkill", "/F", "/PID", str(pid)], capture_output=True, timeout=15
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Could not kill PID %s: %s", pid, exc)

        def _raise_classified():
            with dlg_lock:
                outcome, title, button = dlg["outcome"], dlg["title"], dlg["button"]
            if outcome == "clicked":
                raise SolverInstabilityError(
                    f"solver reported instability; dialog {title!r} auto-dismissed "
                    f"via button {button!r} - candidate FAILED"
                )
            if outcome == "dismissed_benign":
                # Informational post-solve dialog (e.g. Calculation Messages).
                # The solve is a SUCCESS; only log.
                logger.info("Benign dialog %r dismissed; solve stands.", title)
            if outcome == "killed_unknown":
                raise UnknownDialogError(
                    f"unknown dialog encountered: {title!r} - Robot process "
                    "force-terminated; add a dialog pattern for it"
                )
            if timed_out.is_set():
                raise TimeoutError(
                    f"Robot solve() exceeded {self.solve_timeout_s:.1f}s and "
                    "the session's Robot process was force-terminated."
                )

        td = threading.Thread(target=_dialog_watcher, daemon=True)
        tt = threading.Thread(target=_timeout_watcher, daemon=True)
        td.start()
        tt.start()
        try:
            bridge.solve()
            _raise_classified()
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, (SolverInstabilityError, UnknownDialogError, TimeoutError)):
                raise
            _raise_classified()
            raise
        finally:
            killed.set()

    def solve_all(self, analysis_types: list[str]) -> dict[str, Any]:
        """Runs the requested analyses and returns a per-type results dict.

        Supported: "static" (CalcEngine.Calculate, fully verified) and
        "modal" (delegates to the bounded solve_modal, which reports the
        documented RobotOM v27 limitation honestly). Anything else raises.
        """
        out: dict[str, Any] = {}
        for raw in analysis_types:
            at = str(raw).strip().lower()
            if at not in SUPPORTED_ANALYSIS_TYPES:
                raise ValueError(
                    f"analysis type '{at}' not supported by HeadlessSession; "
                    f"choose from {SUPPORTED_ANALYSIS_TYPES}."
                )
            if at == "static":
                stability = self.validate_stability()
                if not stability.get("ok", True):
                    raise MechanismError(stability["message"])
                t0 = time.time()
                self._solve_with_timeout()
                out["static"] = {
                    "status": "ok",
                    "elapsed_s": round(time.time() - t0, 1),
                }
            else:  # modal
                out["modal"] = self.bridge.solve_modal()
        return out

    # ------------------------------------------------------------------ #
    # Results
    # ------------------------------------------------------------------ #

    def get_weight(self) -> dict[str, Any]:
        """Total structure weight from the existing BOQ logic (kg)."""
        df = self.bridge.export_bill_of_materials()
        total = 0.0
        rows = 0
        if df is not None and not df.empty and "Total_Weight_kg" in df.columns:
            total = float(df["Total_Weight_kg"].astype(float).sum())
            rows = len(df)
        return {"weight_kg": round(total, 2), "boq_rows": rows}

    def get_utilization_summary(self, case_id: int = 1) -> dict[str, Any]:
        """Reuses get_utilization_ratios (analytical elastic check).

        Returns the governing utilization + check name and a per-bar table.
        NOTE: same discipline as the interactive tool — this is an elastic
        stress check, NOT full code compliance.
        """
        df = self.bridge.get_utilization_ratios(case_id=case_id)
        per_bar: list[dict[str, Any]] = []
        max_util: float | None = None
        gov_check: str | None = None
        if df is not None and not df.empty and "Utilization" in df.columns:
            valid = df[df["Utilization"].notna()]
            if not valid.empty:
                row = valid.loc[valid["Utilization"].idxmax()]
                max_util = round(float(row["Utilization"]), 4)
                gov_check = str(row.get("Governing_Check", "N/A"))
            for _, r in df.iterrows():
                per_bar.append(
                    {
                        "bar_id": int(r["Bar_ID"]),
                        "utilization": (
                            round(float(r["Utilization"]), 4)
                            if r.get("Utilization") is not None
                            else None
                        ),
                        "governing_check": str(r.get("Governing_Check", "N/A")),
                        "status": str(r.get("Status", "N/A")),
                    }
                )
        return {
            "max_utilization": max_util,
            "governing_check": gov_check,
            "per_bar": per_bar,
            "note": "Elastic stress check only — not full code compliance.",
        }
