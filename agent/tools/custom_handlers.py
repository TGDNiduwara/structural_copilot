"""Result-store and custom-script tool handlers.

[FIX 06] Extracted verbatim from agent/tool_registry.py - dispatch binds
these functions onto ToolExecutor as `_tool_*` methods, so the public API
and behaviour are unchanged.
"""

from __future__ import annotations

import json
from typing import Any

import pandas as pd

from tools.custom_tools import ScriptRejected, run_sandboxed


def tool_store_result(self, key: str) -> dict:
    # Snapshot uses the executor's cached export DataFrames (populated by
    # the export_* tools) plus a live (cheap, bookkeeping-based) model
    # summary — no redundant re-query of solved results.
    try:
        summary = self.robot.get_structure_summary()
    except Exception:
        summary = {"nodes": 0, "bars": 0, "sections": {}}
    message = self.results.store(
        key=key,
        summary=summary,
        member_forces=self.member_forces_df,
        reactions=self.reactions_df,
        boq=self.boq_df,
        utilization=self.utilization_df,
        ltb_status=(self._eurocode_member_summary or {}).get("ltb_status"),
        connection_status=(self._eurocode_member_summary or {}).get("connection_status"),
    )
    if self.member_forces_df.empty and self.reactions_df.empty:
        message += (
            " Note: no exported results were cached — run "
            "export_member_forces / export_reactions (and solve "
            "first) before storing to capture result data."
        )
    elif self.utilization_df.empty:
        message += (
            " Note: no utilization data cached — run "
            "get_utilization_ratios before storing to capture "
            "pass/fail information."
        )
    return {"status": "ok", "message": message, "key": key}


def tool_retrieve_result(self, key: str) -> dict:
    return {"status": "ok", "message": self.results.retrieve(key)}


def tool_list_stored_results(self) -> dict:
    return {"status": "ok", "message": self.results.list_results()}


def tool_clear_stored_results(self) -> dict:
    return {"status": "ok", "message": self.results.clear()}


def tool_run_custom_script(self, code: str, purpose: str = "") -> dict:
    self._ensure_robot()  # scripts expect a live `robot`
    try:
        outcome = run_sandboxed(code, self.robot, timeout_s=120.0)
    except TimeoutError as exc:
        return {"status": "error", "message": str(exc)}
    except ScriptRejected as exc:
        return {"status": "error", "message": str(exc)}
    except RuntimeError as exc:
        # Script raised — return traceback so the LLM can self-correct.
        return {
            "status": "error",
            "message": str(exc),
            "hint": "Fix the script and call run_custom_script again.",
        }
    result = outcome["result"]
    if isinstance(result, pd.DataFrame):
        result = result.head(20).to_dict(orient="records")
    return {
        "status": "ok",
        "purpose": purpose,
        "result": result if result is not None else None,
        "stdout": outcome["stdout"][-40:],
    }


def tool_create_custom_tool(
    self,
    name: str,
    description: str,
    code: str,
    parameters: dict | None = None,
) -> dict:
    message = self.custom_tools.register(
        name=name,
        description=description,
        parameters=parameters or {"type": "object", "properties": {}},
        code=code,
    )
    ok = not message.startswith("Error")
    return {
        "status": "ok" if ok else "error",
        "message": message,
        "hint": (
            "Call it now with sample arguments to test it."
            if ok
            else "Adjust the name/code and retry."
        ),
    }


def tool_list_custom_tools(self) -> dict:
    return {"status": "ok", "message": self.custom_tools.list_tools()}


def tool_delete_custom_tool(self, name: str) -> dict:
    message = self.custom_tools.delete(name)
    ok = not message.startswith("Error")
    return {"status": "ok" if ok else "error", "message": message}


# [FIX 06] dispatch()'s session-custom-tool fallback helper (formerly _call_custom_tool).
def tool_call_custom_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
    """Executes a registered custom tool via dispatch()."""
    try:
        self._ensure_robot()
        outcome = self.custom_tools.call(tool_name, arguments, self.robot, timeout_s=120.0)
    except TimeoutError as exc:
        return json.dumps({"status": "error", "tool": tool_name, "message": str(exc)})
    except (RuntimeError, KeyError) as exc:
        return json.dumps(
            {
                "status": "error",
                "tool": tool_name,
                "message": str(exc),
                "hint": "Fix the custom tool (delete + re-register) and retry.",
            }
        )
    result = outcome["result"]
    if isinstance(result, pd.DataFrame):
        result = {"preview": result.head(20).to_dict(orient="records"), "rows": len(result)}
    payload = {"status": "ok", "tool": tool_name, "result": result}
    if outcome["stdout"]:
        payload["stdout_tail"] = outcome["stdout"][-15:]
    return json.dumps(payload, default=str)
