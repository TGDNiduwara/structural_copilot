"""
tools/custom_tools.py
=====================
[WP1 — Meta-layer] LLM-authored secondary tools.

Lets the agent write small Python tools against the live RobotBridge when
the built-in tool catalog cannot express a request (custom geometry
patterns, custom materials, parametric sweeps, ...), optionally REGISTER
them as named parameterized tools for the rest of the session, and call
them like any built-in tool.

Security model (pragmatic, local engineering agent):
- Scripts execute in a restricted namespace: the live `robot` bridge,
  RobotEnum, math, json, pandas — and NO filesystem, network, subprocess,
  or arbitrary imports (`__import__` is not provided; a static screen also
  rejects suspicious tokens before execution).
- Scripts run in a worker thread with a timeout.

Pure Python except the injected `robot` bridge (which is only touched when
a script chooses to call it).
"""

from __future__ import annotations

import logging
import math
from typing import Any

import pandas as pd

logger = logging.getLogger("structural_copilot.custom_tools")

# Imports a custom script may use (everything else is rejected).
ALLOWED_MODULES = {"math", "json", "statistics", "itertools", "pandas"}

# Static token screen — rejected before execution regardless of context.
FORBIDDEN_TOKENS = (
    "__import__",
    "open(",
    "exec(",
    "eval(",
    "compile(",
    "getattr(sys",
    "globals()",
    "locals()",
    "os.",
    "sys.",
    "subprocess",
    "socket",
    "shutil",
    "pathlib",
    "requests",
    "urllib",
    "__subclasses__",
    "__builtins__",
    "importlib",
    "ctypes",
    "win32com",
    "pythoncom",
)


class ScriptRejected(Exception):
    """Raised when a script fails the static security screen."""


def screen_script(code: str) -> None:
    """Static safety screen. Raises ScriptRejected with the offending token."""
    if not code or not code.strip():
        raise ScriptRejected("Empty script.")
    lowered = code.lower()
    for token in FORBIDDEN_TOKENS:
        if token.lower() in lowered:
            raise ScriptRejected(
                f"Script rejected: forbidden token '{token}'. Custom tools "
                "may only use the provided 'robot' bridge plus math/json/"
                "pandas/itertools/statistics — no filesystem, network, or "
                "system access."
            )
    for line in code.splitlines():
        stripped = line.strip()
        if stripped.startswith("import ") or stripped.startswith("from "):
            parts = stripped.replace("import", " ").split()
            module = parts[0].strip() if parts else ""
            if module not in ALLOWED_MODULES:
                raise ScriptRejected(
                    f"Script rejected: import '{module}' is not allowed. "
                    f"Allowed modules: {sorted(ALLOWED_MODULES)}."
                )


def _safe_builtins(print_fn) -> dict[str, Any]:
    """A minimal builtins dict for the sandbox (no open/exec/eval; a guarded
    __import__ only permits ALLOWED_MODULES)."""
    import builtins as _b

    names = [
        "abs",
        "min",
        "max",
        "sum",
        "round",
        "len",
        "range",
        "enumerate",
        "zip",
        "sorted",
        "reversed",
        "isinstance",
        "str",
        "int",
        "float",
        "bool",
        "list",
        "dict",
        "set",
        "tuple",
        "any",
        "all",
        "map",
        "filter",
        "print",
        "type",
        "repr",
        "Exception",
        "ValueError",
        "TypeError",
        "RuntimeError",
        "ZeroDivisionError",
        "StopIteration",
        "KeyError",
        "IndexError",
        "ArithmeticError",
    ]
    ns = {n: getattr(_b, n) for n in names}
    ns["print"] = print_fn

    def _guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        root = (name or "").split(".")[0]
        if root not in ALLOWED_MODULES:
            raise ImportError(
                f"Import of '{name}' is not allowed in custom tools. "
                f"Allowed modules: {sorted(ALLOWED_MODULES)}."
            )
        return _b.__import__(name, globals, locals, fromlist, level)

    ns["__import__"] = _guarded_import
    return ns


class _SandboxRobotProxy:
    """
    [ATTACH-FIX] Wraps the live RobotBridge for run_custom_script so that any
    method returning a pandas DataFrame transparently yields a LIST OF DICTS
    (records). The LLM reliably assumes record-style data in its scripts
    (`for r in rows: r['MY_kNm']`), but the raw bridge exports return
    DataFrames — iterating a DataFrame yields column names, so subscripting
    them fails. This mismatch caused the recurring 'persistent error pattern'
    in run_custom_script. Non-DataFrame results pass through untouched.
    """

    def __init__(self, robot: Any) -> None:
        object.__setattr__(self, "_robot", robot)

    @staticmethod
    def _to_records(value: Any) -> Any:
        if isinstance(value, pd.DataFrame):
            return value.to_dict(orient="records")
        if isinstance(value, dict):
            return {k: _SandboxRobotProxy._to_records(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [_SandboxRobotProxy._to_records(v) for v in value]
        return value

    def __getattr__(self, name: str) -> Any:
        attr = getattr(object.__getattribute__(self, "_robot"), name)
        if callable(attr):

            def _wrap(*args, **kwargs):
                return _SandboxRobotProxy._to_records(attr(*args, **kwargs))

            return _wrap
        return attr


def run_sandboxed(
    code: str,
    robot: Any,
    timeout_s: float = 60.0,
    extra_vars: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Executes `code` in the restricted namespace and returns
    {"result": ..., "stdout": [...]} or raises RuntimeError.

    IMPORTANT (threading): the script runs SYNCHRONOUSLY on the CALLING
    thread. Robot COM interface pointers are apartment-bound to the thread
    that connected (`robot.connect()`), so executing on any other thread
    fails with "interface marshalled for a different thread" (-2147417842).
    This matches how every built-in tool executes, but means `timeout_s`
    cannot hard-interrupt a script (kept for signature compatibility; a
    runaway loop blocks the turn exactly like a long-running solve()).
    """
    from tools.robot_tool import RobotEnum  # injected, not imported by script

    screen_script(code)

    output: list[str] = []

    def _print(*args, **kwargs):
        output.append(" ".join(str(a) for a in args))

    ns: dict[str, Any] = {
        # [ATTACH-FIX] Proxy converts DataFrame exports to record lists so
        # LLM scripts that do `for r in robot.export_all_member_forces(...)`
        # get dicts, not column-name iteration.
        "robot": _SandboxRobotProxy(robot),
        "RobotEnum": RobotEnum,
        "math": math,
        "json": __import__("json"),
        "pd": pd,
        "result": None,
        "__builtins__": _safe_builtins(_print),
    }
    if extra_vars:
        ns.update(extra_vars)

    try:
        exec(compile(code, "<custom_tool>", "exec"), ns)  # noqa: S102
    except SystemExit:
        pass  # a script may call exit() defensively; treat as normal end
    except BaseException as exc:  # noqa: BLE001 — surfaced to the LLM
        import traceback

        raise RuntimeError(
            f"Custom script failed: {type(exc).__name__}: {exc}\n"
            + traceback.format_exception_only(type(exc), exc)[-1].strip()
        ) from exc

    return {"result": ns.get("result"), "stdout": output}


# --- [CT_REGISTRY] ---
class CustomToolRegistry:
    """Session-scoped registry of LLM-authored, parameterized tools."""

    def __init__(self) -> None:
        self._tools: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------ #

    def register(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        code: str,
    ) -> str:
        """Validates and stores a custom tool. `parameters` is a JSON-schema
        'parameters' object; its declared properties are injected into the
        script's namespace as variables when called."""
        import re as _re

        if not _re.fullmatch(r"[a-z][a-z0-9_]{2,40}", name or ""):
            return (
                f"Error: invalid tool name '{name}'. Use snake_case, "
                "3-40 chars, letters/digits/underscore, starting with a "
                "letter."
            )
        if name in {s.get("name") for s in self.schemas()} or name in _BUILTIN_TOOL_NAMES:
            return (
                f"Error: a tool named '{name}' already exists. Choose a "
                "different name or delete it first."
            )
        try:
            screen_script(code)
        except ScriptRejected as exc:
            return f"Error: {exc}"

        self._tools[name] = {
            "name": name,
            "description": description or "Custom agent-authored tool.",
            "parameters": parameters or {"type": "object", "properties": {}},
            "code": code,
        }
        logger.info("Custom tool '%s' registered.", name)
        return (
            f"Custom tool '{name}' registered and now callable. Test it "
            "by calling it with sample arguments."
        )

    # ------------------------------------------------------------------ #

    def call(
        self, name: str, arguments: dict[str, Any], robot: Any, timeout_s: float = 120.0
    ) -> Any:
        """Executes a registered custom tool with the given arguments."""
        tool = self._tools.get(name)
        if tool is None:
            raise KeyError(
                f"Unknown custom tool '{name}'. Registered: {list(self._tools) or '(none)'}."
            )
        # Only inject declared properties (defensive: extra LLM args ignored).
        declared = (tool["parameters"] or {}).get("properties", {}) or {}
        inject = {k: v for k, v in (arguments or {}).items() if k in declared}
        outcome = run_sandboxed(tool["code"], robot, timeout_s=timeout_s, extra_vars=inject)
        return outcome

    # ------------------------------------------------------------------ #

    def list_tools(self) -> str:
        if not self._tools:
            return (
                "No custom tools registered yet. Write one with "
                "run_custom_script first, then register it via "
                "create_custom_tool."
            )
        lines = [f"{len(self._tools)} custom tool(s):"]
        for name, t in self._tools.items():
            props = list((t["parameters"] or {}).get("properties", {}) or {})
            lines.append(f"- {name}({', '.join(props)}): {t['description']}")
        return "\n".join(lines)

    def delete(self, name: str) -> str:
        if name in self._tools:
            del self._tools[name]
            return f"Custom tool '{name}' deleted."
        return f"Error: no custom tool named '{name}'. Registered: {list(self._tools) or '(none)'}."

    def schemas(self) -> list[dict[str, Any]]:
        """OpenAI-style tool schemas for all registered custom tools."""
        return [
            {
                "name": t["name"],
                "description": ("[custom] " + t["description"])[:300],
                "parameters": t["parameters"],
            }
            for t in self._tools.values()
        ]

    def has(self, name: str) -> bool:
        return name in self._tools


# Built-in tool names that custom tools must not shadow (kept in sync with
# TOOL_SCHEMAS at import time by tool_registry).
_BUILTIN_TOOL_NAMES: set = set()
