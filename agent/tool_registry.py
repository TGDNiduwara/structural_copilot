"""
agent/tool_registry.py
=======================

[FIX 06] The tool handlers were split into domain modules under agent/tools/:
- schemas.py           -> TOOL_SCHEMAS (JSON schemas)
- _shared.py           -> ToolExecutionError, safe_output_path, _validate_tool_arguments,
                          GENERATED_DIR / ALLOWED_EXTENSIONS helpers
- robot_handlers.py    -> Robot model-building handlers
- template_handlers.py -> template / compose-structure handlers
- export_handlers.py   -> export handlers
- report_handlers.py   -> Word / PowerPoint report handlers
- check_handlers.py    -> engineering check handlers
- batch_handlers.py    -> optimization / surrogate-search handlers
- custom_handlers.py   -> result-store / custom-script handlers

This module keeps the ToolExecutor shell (session state, dispatch, Robot
connection helper, compose-engine helpers) and installs the domain handlers
from the modules above so the public API is unchanged.
"""

from __future__ import annotations

import json
import os
import traceback
from typing import Any

import pandas as pd
import structlog

from agent.tools import (  # noqa: E402
    batch_handlers,
    check_handlers,
    custom_handlers,
    export_handlers,
    report_handlers,
    robot_handlers,
    template_handlers,
)
from agent.tools._shared import (  # noqa: E402
    _PROJECT_ROOT,  # used by __init__ for the batch DB path
    ALLOWED_EXTENSIONS,  # noqa: F401 - public re-export (used by batch tests)
    GENERATED_DIR,
    ToolExecutionError,
    _ensure_generated_dir,
    _validate_tool_arguments,
    safe_output_path,  # noqa: F401 - public re-export (used by tests)
)

# [FIX 06] domain-split pieces
from agent.tools.schemas import TOOL_SCHEMAS  # noqa: E402
from batch.design_space import DesignSpace
from batch.runner import run_batch
from batch.storage import Storage
from batch.surrogate_search import (
    run_surrogate_search,
)

# [FIX 03] centralized config
from tools.custom_tools import (
    CustomToolRegistry,
)
from tools.diagram_tool import DiagramGenerator
from tools.excel_tool import ExcelReporter
from tools.pptx_tool import PowerPointReporter
from tools.result_store import ResultStore
from tools.robot_tool import RobotBridge
from tools.word_tool import WordReporter

logger = structlog.get_logger(
    "structural_copilot.tool_registry"
)  # [FIX 08]  # filtering at INFO via make_filtering_bound_logger

# --------------------------------------------------------------------------
# [FIX 06] Handler registry - dispatch resolves tool names through this map.
# --------------------------------------------------------------------------
_HANDLER_MODULES = (
    robot_handlers,
    template_handlers,
    export_handlers,
    report_handlers,
    check_handlers,
    batch_handlers,
    custom_handlers,
)


def _build_handler_registry():
    registry: dict[str, Any] = {}
    for _module in _HANDLER_MODULES:
        for _name, _fn in vars(_module).items():
            if _name.startswith("tool_") and callable(_fn):
                registry[_name[len("tool_") :]] = _fn
    return registry


_HANDLERS = _build_handler_registry()


class ToolExecutor:
    """
    Owns live instances of the four engineering tool bridges plus the
    in-memory result cache (member forces / reactions / BOQ DataFrames and
    generated file paths), and dispatches named tool calls to them.

    One ToolExecutor is created per Streamlit session and stashed in
    st.session_state so state (the open Robot model, cached DataFrames,
    generated artifact paths) survives across chat turns.
    """

    def __init__(self, robot_visible: bool = True):
        self.robot = RobotBridge()
        self.excel = ExcelReporter()
        self.diagrams = DiagramGenerator()
        self.word = WordReporter()
        self.pptx = PowerPointReporter()
        # [PHASE 2] Session-scoped variant/result snapshot store.
        self.results = ResultStore()
        # [WP1 meta-layer] LLM-authored custom tools (session-scoped).
        self.custom_tools = CustomToolRegistry()
        import tools.custom_tools as _ct

        _ct._BUILTIN_TOOL_NAMES = {s["name"] for s in TOOL_SCHEMAS}

        # [P7] Batch optimizer state: staged (validated but NOT started)
        # design-space configs, plus handles to live background runs.
        self._optimization_configs: dict[str, dict] = {}
        self._optimization_runs: dict[int, dict] = {}  # run_id -> thread info
        # [P7] Default SQLite DB for batch runs, shared by all bookend tools.
        self._batch_db_path = os.path.join(_PROJECT_ROOT, "batch", "runs.db")
        # [OBS] Lifecycle event log (plain list - no Streamlit import here).
        # app.py drains this into the sidebar Activity Log panel each turn so
        # connect/close/clear_structure events are visible in the running app.
        self.activity_log: list[str] = []

        # [COMPOSE] Session-scoped geometry-composition state (verified
        # primitives only; see _tool_compose_structure). Persists across
        # chat turns via the ToolExecutor held in st.session_state.
        self._compose_chains: dict[str, dict] = {}
        self._compose_bars: list[dict] = []
        self._compose_supports: list[dict] = []
        self._compose_next_id: int = 1

        self._robot_connected = False
        self._robot_visible = robot_visible

        self.member_forces_df: pd.DataFrame = pd.DataFrame()
        self.reactions_df: pd.DataFrame = pd.DataFrame()
        self.boq_df: pd.DataFrame = pd.DataFrame()
        # [WP6] Additional cached result exports.
        self.displacements_df: pd.DataFrame = pd.DataFrame()
        self.stresses_df: pd.DataFrame = pd.DataFrame()
        # [WP7] Modal frequency cache (for the Excel 'modal' sheet).
        self.modal_frequencies_df: pd.DataFrame = pd.DataFrame()
        # [P4] Utilization cache (included in store_result snapshots).
        self.utilization_df: pd.DataFrame = pd.DataFrame()

        self.generated_files: dict[str, str] = {}  # logical name -> abs path
        self.diagram_paths: dict[str, str] = {}

    def _log_activity(self, entry: str) -> None:
        """Appends a timestamped lifecycle event to the executor's activity log
        (drained into the UI by app.py)."""
        from datetime import datetime

        ts = datetime.now().strftime("%H:%M:%S")
        self.activity_log.append(f"[{ts}] {entry}")

    def drain_activity(self) -> list[str]:
        """Returns and clears the pending lifecycle events."""
        out = list(self.activity_log)
        self.activity_log = []
        return out

    def get_tool_schemas(self) -> list:
        """
        [WP1] Base tool schemas + any custom tools registered this session.
        The agent loop passes this to the LLM so custom tools appear in the
        tool list immediately after registration.
        """
        return TOOL_SCHEMAS + self.custom_tools.schemas()

    def set_robot_visible(self, visible: bool) -> None:
        """[FIX M5] Public setter for Robot visibility instead of direct _ access."""
        self._robot_visible = visible

    def dispatch(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """
        Executes a named tool with the given arguments. Returns a JSON string
        result on success. Raises ToolExecutionError (with a clear message
        suitable for feeding back to the LLM) on failure.
        """
        # [FIX M10] Validate arguments against schema before dispatching
        _validate_tool_arguments(tool_name, arguments)

        handler = _HANDLERS.get(tool_name)  # [FIX 06] registry lookup (was getattr on the class)
        if handler is None:
            # [WP1 meta-layer] fall back to session-registered custom tools.
            if self.custom_tools.has(tool_name):
                # [FIX 06] custom-tool fallback lives in custom_handlers.py
                return custom_handlers.tool_call_custom_tool(self, tool_name, arguments)
            raise ToolExecutionError(f"Unknown tool '{tool_name}'. No such handler is registered.")

        try:
            result = handler.__get__(self, type(self))(**arguments)
        except ToolExecutionError:
            raise  # Re-raise as-is
        except TypeError as exc:
            # Convert Python TypeError (wrong arguments) to structured error
            raise ToolExecutionError(
                f"Tool '{tool_name}' argument error: {exc}. "
                f"Provided arguments: {list(arguments.keys())}."
            ) from exc
        except Exception as exc:
            tb = traceback.format_exc(limit=4)
            logger.error("Unhandled error in tool '%s': %s\n%s", tool_name, exc, tb)
            raise ToolExecutionError(
                f"Tool '{tool_name}' failed with an unexpected error: {exc}"
            ) from exc

        return json.dumps(result, default=str)

    def _ensure_robot(self):
        """
        Connects to Robot on first use; reconnects if the connection was
        genuinely lost (COM transport error).

        [FIX R2] connect() failures are converted into ToolExecutionError so
        the LLM receives actionable guidance instead of the agent silently
        looping. The RobotBridge launch circuit-breaker [FIX R3] guarantees
        no endless robot.exe spawning.
        """
        try:
            if not self._robot_connected:
                self.robot.connect(visible=self._robot_visible)
                self._robot_connected = True
                self._log_activity(f"🔌 Robot connected (PID {self.robot.pid}) - first connection")
            elif not self.robot.is_alive():  # [FIX H8] Health check
                logger.warning("Robot connection lost; attempting reconnect...")
                self.robot.connect(visible=self._robot_visible)
                self._robot_connected = True
                self._log_activity(
                    f"🔌 Robot RECONNECTED (PID {self.robot.pid}) - health-check "
                    "reconnect after connection loss"
                )
        except ToolExecutionError:
            raise
        except Exception as exc:
            self._robot_connected = False
            raise ToolExecutionError(
                f"Could not establish a Robot Structural Analysis connection: {exc}. "
                "Verify Robot is installed, licensed, and not blocked by a "
                "splash/license dialog; then retry the tool call."
            ) from exc

    def _run_optimization_worker(
        self, ds: DesignSpace, run_id: int, holder: dict[str, Any]
    ) -> None:
        """Runs run_batch on the background thread for the pre-created run."""
        try:
            summary = run_batch(ds, run_id=run_id, db_path=self._batch_db_path)
            holder["run_id"] = summary["run_id"]
        except Exception as exc:  # noqa: BLE001
            logger.error("Batch optimizer worker failed: %s", exc)
            holder["error"] = str(exc)

    def _run_surrogate_worker(
        self, ds: DesignSpace, run_id: int, cfg: dict[str, Any], holder: dict[str, Any]
    ) -> None:
        """Runs run_surrogate_search on the background thread for the
        pre-created run (its own Robot instance, never the interactive
        session's)."""
        try:
            summary = run_surrogate_search(
                ds,
                run_id=run_id,
                budget=cfg["budget"],
                patience=cfg["patience"],
                acquisition=cfg["acquisition"],
                kappa=cfg["kappa"],
                db_path=self._batch_db_path,
            )
            holder["run_id"] = summary.get("run_id", run_id)
            holder["status"] = summary.get("status")
            if summary.get("status") == "grid_fallback":
                # The start-tool pre-check should have prevented this; mark
                # the pre-created run completed so it never dangles as
                # 'running' with zero results.
                try:
                    st = Storage(db_path=self._batch_db_path)
                    st.mark_run_status(run_id, "completed")
                    st.close()
                except Exception:  # noqa: BLE001
                    pass
        except Exception as exc:  # noqa: BLE001
            logger.error("Surrogate optimizer worker failed: %s", exc)
            holder["error"] = str(exc)

    def _save_robot_project_artifact(self, base_name: str) -> None:
        """
        [TANK-FIX] Saves the current Robot model (.rtd) into the generated
        artifacts directory so the user can download it alongside the
        reports/diagrams. Best-effort: never blocks artifact generation.
        """
        try:
            self._ensure_robot()
            stem = (os.path.splitext(os.path.basename(base_name))[0] or "robot_model").strip()
            _ensure_generated_dir()
            rtd_path = os.path.join(GENERATED_DIR, f"{stem}.rtd")
            self.robot.save_project(rtd_path)
            self.generated_files[f"{stem}.rtd"] = rtd_path
        except Exception as exc:
            logger.warning("Could not auto-save the Robot model artifact: %s", exc)

    def _compose_reset(self) -> dict:
        self._compose_chains = {}
        self._compose_bars = []
        self._compose_supports = []
        self._compose_next_id = 1
        return {"status": "ok", "message": "composition reset"}

    def _compose_apply_step(self, step: dict) -> dict:
        """Applies ONE compose step with IMMEDIATE per-op validation (never
        deferred to finish). Raises ToolExecutionError on any bad input."""
        from tools.geometry_primitives import (
            apply_support_pattern,
            connect_bracing,
            connect_web_pattern,
            generate_arc_chord,
            generate_straight_chord,
        )

        op = str(step.get("op") or "").lower()
        if not op:
            raise ToolExecutionError(
                "compose_structure step is missing 'op' (chord | web | bracing | copy | support)."
            )

        def _require(fields):
            for f in fields:
                if f not in step or step[f] in (None, ""):
                    raise ToolExecutionError(f"compose_structure op '{op}' requires '{f}'.")

        def _chain(name):
            if name not in self._compose_chains:
                raise ToolExecutionError(
                    f"compose_structure: unknown chain '{name}' - define it "
                    f"first with op='chord' (known: {sorted(self._compose_chains)})."
                )
            return self._compose_chains[name]

        if op == "chord":
            _require(["name"])
            name = str(step["name"])
            if name in self._compose_chains:
                raise ToolExecutionError(
                    f"compose_structure: chain name '{name}' already exists - pick a unique name."
                )
            kind = str(step.get("kind") or "straight").lower()
            if kind not in ("straight", "arc"):
                raise ToolExecutionError(
                    f"compose_structure op 'chord': kind must be 'straight' or 'arc', got {kind!r}."
                )
            _require(["span", "n_panels"])
            try:
                span = float(step["span"])
                n_panels = int(step["n_panels"])
            except (TypeError, ValueError) as exc:
                raise ToolExecutionError(
                    f"compose_structure op 'chord': span/n_panels must be "
                    f"numbers, got {step.get('span')!r}/{step.get('n_panels')!r}."
                ) from exc
            if span <= 0.0:
                raise ToolExecutionError("compose_structure op 'chord': span must be > 0.")
            if n_panels < 2:
                raise ToolExecutionError("compose_structure op 'chord': n_panels must be >= 2.")
            try:
                y_shift = float(step.get("plane") or 0.0)
            except (TypeError, ValueError):
                raise ToolExecutionError(
                    f"compose_structure op 'chord': plane must be a number, "
                    f"got {step.get('plane')!r}."
                ) from None
            section = str(step.get("section") or "IPE 200")
            if kind == "arc":
                try:
                    rise = float(step.get("rise") or 0.0)
                except (TypeError, ValueError):
                    raise ToolExecutionError(
                        f"compose_structure op 'chord' (arc): rise must be a "
                        f"number, got {step.get('rise')!r}."
                    ) from None
                chain = generate_arc_chord(
                    span,
                    rise,
                    n_panels,
                    elevation=float(step.get("elevation") or 0.0),
                    plane=y_shift,
                    arch=str(step.get("arch") or "up"),
                    section=section,
                    start_id=self._compose_next_id,
                )
            else:
                chain = generate_straight_chord(
                    span,
                    n_panels,
                    elevation=float(step.get("elevation") or 0.0),
                    plane=y_shift,
                    section=section,
                    start_id=self._compose_next_id,
                )
            self._compose_chains[name] = chain
            self._compose_next_id = max(
                self._compose_next_id,
                chain["last"] + 1,
                (chain["bars"][-1]["id"] + 1) if chain["bars"] else 1,
            )
            return {
                "status": "ok",
                "message": f"chain '{name}' added",
                "nodes": len(chain["nodes"]),
                "bars": len(chain["bars"]),
            }

        if op == "web":
            _require(["top", "bottom"])
            top = _chain(str(step["top"]))
            bottom = _chain(str(step["bottom"]))
            if len(top["ids"]) != len(bottom["ids"]):
                raise ToolExecutionError(
                    f"compose_structure op 'web': chains "
                    f"'{step['top']}' ({len(top['ids'])} nodes) and "
                    f"'{step['bottom']}' ({len(bottom['ids'])} nodes) have "
                    "different panel counts - regenerate with matching n_panels."
                )
            pattern = str(step.get("pattern") or "pratt").lower()
            if pattern not in ("pratt", "warren"):
                raise ToolExecutionError(
                    f"compose_structure op 'web': pattern must be 'pratt' or "
                    f"'warren', got {pattern!r}."
                )
            all_bars = connect_web_pattern(
                top,
                bottom,
                pattern,
                web_section=str(step.get("web_section") or "IPE 200"),
                start_id=self._compose_next_id,
            )
            # connect_web_pattern also emits the two chord runs; the chains
            # ALREADY carry their own chord bars, so keep only the web bars.
            n = len(top["ids"]) - 1
            web = all_bars[2 * n :]
            # [COMPOSE] A web member whose two endpoints are COINCIDENT is a
            # degenerate zero-length bar (e.g. the arch springs from z=0 at
            # the deck ends -> the end vertical arch_a[0]-deck_a[0] has length
            # 0). Robot keeps such bars but every downstream consumer
            # (apply_self_weight skips length<=0, the solver sees a singular
            # element) treats them as garbage. The two chains ARE connected at
            # those points through the adjacent diagonals, so dropping the
            # zero-length web bar is the correct, non-degenerate geometry.
            coords = {
                nd["id"]: (nd["x"], nd["y"], nd["z"]) for nd in top["nodes"] + bottom["nodes"]
            }
            dropped = [br["id"] for br in web if coords[br["n1"]] == coords[br["n2"]]]
            bars = [br for br in web if br["id"] not in set(dropped)]
            self._compose_bars.extend(bars)
            self._compose_next_id = max(
                self._compose_next_id,
                (all_bars[-1]["id"] + 1) if all_bars else self._compose_next_id,
            )
            msg = f"web {pattern} added between '{step['top']}' and '{step['bottom']}'"
            if dropped:
                msg += f" (dropped {len(dropped)} zero-length end bar(s): {dropped})"
            return {"status": "ok", "message": msg, "bars": len(bars)}

        if op == "bracing":
            _require(["plane_a", "plane_b"])
            pa = _chain(str(step["plane_a"]))
            pb = _chain(str(step["plane_b"]))
            if len(pa["ids"]) != len(pb["ids"]):
                raise ToolExecutionError(
                    f"compose_structure op 'bracing': planes "
                    f"'{step['plane_a']}' ({len(pa['ids'])} nodes) and "
                    f"'{step['plane_b']}' ({len(pb['ids'])} nodes) have "
                    "different panel counts - bracing needs matching n_panels."
                )
            pattern = str(step.get("pattern") or "cross").lower()
            if pattern not in ("cross", "transverse"):
                raise ToolExecutionError(
                    f"compose_structure op 'bracing': pattern must be "
                    f"'cross' or 'transverse', got {pattern!r}."
                )
            bars = connect_bracing(
                pa,
                pb,
                pattern,
                section=str(step.get("section") or "IPE 200"),
                start_id=self._compose_next_id,
            )
            self._compose_bars.extend(bars)
            self._compose_next_id = max(
                self._compose_next_id, (bars[-1]["id"] + 1) if bars else self._compose_next_id
            )
            return {
                "status": "ok",
                "message": f"bracing {pattern} added between "
                f"'{step['plane_a']}' and '{step['plane_b']}'",
                "bars": len(bars),
            }

        if op == "copy":
            _require(["source", "name", "y_shift"])
            try:
                y_shift = float(step["y_shift"])
            except (TypeError, ValueError):
                raise ToolExecutionError(
                    f"compose_structure op 'copy': y_shift must be a number, "
                    f"got {step.get('y_shift')!r}."
                ) from None  # [FIX 09] B904
            import math as _math

            if not _math.isfinite(y_shift):
                raise ToolExecutionError("compose_structure op 'copy': y_shift must be finite.")
            src = _chain(str(step["source"]))
            name = str(step["name"])
            if name in self._compose_chains:
                raise ToolExecutionError(
                    f"compose_structure op 'copy': chain name '{name}' already "
                    "exists - pick a unique name."
                )
            shift = int(self._compose_next_id) - src["first"]
            id_map = {}
            new_nodes = []
            for nd in src["nodes"]:
                new_id = int(nd["id"]) + shift
                id_map[int(nd["id"])] = new_id
                new_nodes.append(
                    {
                        "id": new_id,
                        "x": nd["x"],
                        "y": round(float(nd["y"]) + y_shift, 6),
                        "z": nd["z"],
                    }
                )
            new_bars = [
                {
                    "id": int(b["id"]) + shift,
                    "n1": id_map[int(b["n1"])],
                    "n2": id_map[int(b["n2"])],
                    "section": b["section"],
                }
                for b in src["bars"]
            ]
            self._compose_chains[name] = {
                "nodes": new_nodes,
                "bars": new_bars,
                "section": src["section"],
                "first": new_nodes[0]["id"],
                "last": new_nodes[-1]["id"],
                "ids": [n["id"] for n in new_nodes],
            }
            # [AUDIT 2026-08-23] advance past the copied chain's BAR ids too
            # (not just node ids): a bracing/web op after a copy used to start
            # at a next_id that collided with the copied chain's chord bars.
            # The twin-arch only dodged this because the following web op
            # discards its own leading chord bars; a bracing op does not.
            self._compose_next_id = max(
                self._compose_next_id,
                self._compose_chains[name]["last"] + 1,
                (new_bars[-1]["id"] + 1) if new_bars else 1,
            )
            return {
                "status": "ok",
                "message": f"chain '{name}' copied from '{step['source']}' with y_shift={y_shift}",
                "nodes": len(new_nodes),
                "bars": len(new_bars),
            }

        if op == "support":
            _require(["type"])
            stype = str(step["type"]).lower()
            if stype not in ("pinned", "fixed", "roller_x", "roller_z", "spring"):
                raise ToolExecutionError(
                    f"compose_structure op 'support': unknown support_type "
                    f"{stype!r} (pinned | fixed | roller_x | roller_z | spring)."
                )
            chain_ref = step.get("chain")
            explicit = step.get("nodes")
            if chain_ref:
                chain = _chain(str(chain_ref))
                if str(step.get("ends_only", "true")).lower() in ("true", "1", "yes"):
                    node_ids = [chain["first"], chain["last"]]
                else:
                    node_ids = chain["ids"]
            elif explicit:
                node_ids = []
                for n in explicit:
                    try:
                        node_ids.append(int(n))
                    except (TypeError, ValueError):
                        raise ToolExecutionError(
                            f"compose_structure op 'support': nodes must be ints, got {n!r}."
                        ) from None  # [FIX 09] B904
            else:
                raise ToolExecutionError(
                    "compose_structure op 'support': provide 'chain' (ends "
                    "supported) or 'nodes' (explicit ids)."
                )
            self._compose_supports.extend(apply_support_pattern(node_ids, stype))
            return {
                "status": "ok",
                "message": f"support '{stype}' applied to {len(node_ids)} node(s)",
            }

        raise ToolExecutionError(
            f"compose_structure: unknown op '{op}' (chord | web | bracing | copy | support)."
        )

    def _compose_finish(self) -> dict:
        """Merges every registered chain + accumulated bars + supports into
        ONE spec, runs the integrity pre-flight, and clears the registry."""
        nodes: list[dict] = []
        bars: list[dict] = list(self._compose_bars)
        for name in sorted(self._compose_chains):
            chain = self._compose_chains[name]
            nodes.extend(chain["nodes"])
            bars.extend(chain["bars"])
        spec = {
            "project": "3D",
            "nodes": nodes,
            "bars": bars,
            "supports": list(self._compose_supports),
            "__composed": True,
        }
        # [AUDIT] Robot's solver SILENTLY MERGES coincident-but-distinct nodes
        # during Calculate() (live-verified: 35 composed -> 25 solved nodes;
        # the merged-away ids are exactly the coincident pairs). That is the
        # root cause of the bar_uniform load shortfall and makes round-trips
        # lossy, so merge them HERE - the single chokepoint where compose
        # geometry is finalized - before the integrity pre-flight. The spec
        # returned is then identical to what Robot will actually analyze.
        from tools.geometry_primitives import merge_coincident_nodes

        spec = merge_coincident_nodes(spec)
        issues = self.robot.spec_integrity_issues(spec)
        counts = {
            "nodes": len(spec["nodes"]),
            "bars": len(spec["bars"]),
            "supports": len(spec.get("supports") or []),
        }
        message = (
            f"assembled {counts['nodes']} nodes / {counts['bars']} bars / "
            f"{counts['supports']} supports"
        )
        if issues:
            self._compose_reset()
            raise ToolExecutionError(
                f"compose_structure finish: spec integrity FAILED: {'; '.join(issues)}"
            )
        self._compose_reset()
        return {"status": "ok", "message": message, "geometry": spec, "counts": counts}


# --------------------------------------------------------------------------
# [FIX 06] Install domain handlers onto ToolExecutor (keeps the legacy
# direct-call API, e.g. ex._tool_create_node(...), working for existing
# callers/tests, while dispatch itself resolves through _HANDLERS).
# --------------------------------------------------------------------------
def _install_handlers(cls: type) -> None:
    for _tool_name, _fn in _HANDLERS.items():
        setattr(cls, "_tool_" + _tool_name, _fn)
    # _call_custom_tool is invoked directly from dispatch() as
    # custom_handlers.tool_call_custom_tool (kept out of the class to avoid
    # dynamic-attribute typing issues).


_install_handlers(ToolExecutor)
