"""Shared helpers for the domain-split agent/tools modules.  No import from
agent.tool_registry here (would be circular) - this is the dependency root.
"""

from __future__ import annotations

import os
from typing import Any

from agent.tools.schemas import TOOL_SCHEMAS


class ToolExecutionError(RuntimeError):
    """Raised (and caught by the agent loop) when a tool call fails."""


# [FIX H5] Use module-relative path instead of os.getcwd()
_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_MODULE_DIR)
GENERATED_DIR = os.environ.get(
    "STRUCTURAL_AGENT_GENERATED_DIR",
    os.path.join(_PROJECT_ROOT, "generated"),
)

_generated_dir_created = False


def _ensure_generated_dir():
    """Lazily creates the generated output directory on first use."""
    global _generated_dir_created
    if not _generated_dir_created:
        os.makedirs(GENERATED_DIR, exist_ok=True)
        _generated_dir_created = True


ALLOWED_EXTENSIONS = {".xlsx", ".docx", ".pptx", ".png", ".pdf", ".rtd"}


def safe_output_path(file_name: str, base_dir: str = GENERATED_DIR) -> str:
    """
    Validates that file_name resolves to a path within base_dir after
    canonicalization, preventing path traversal attacks.
    Also validates the file extension.
    """
    _ensure_generated_dir()

    if not file_name or not file_name.strip():
        raise ValueError("file_name must not be empty.")

    # Strip any leading path separators
    file_name = file_name.lstrip(os.sep).lstrip("/")

    if os.path.isabs(file_name):
        raise ValueError(f"Absolute paths not allowed: {file_name}")

    # Check for path traversal patterns before resolution
    parts = file_name.replace("\\", "/").split("/")
    for part in parts:
        if part == ".." or part == ".":
            raise ValueError(f"Path traversal detected in file_name: {file_name}")

    # Resolve the full path and verify it stays within base_dir
    full_path = os.path.realpath(os.path.join(base_dir, file_name))
    base_real = os.path.realpath(base_dir)

    if not full_path.startswith(base_real + os.sep) and full_path != base_real:
        raise ValueError(f"Path traversal detected: {file_name} resolves to {full_path}")

    # Validate file extension
    ext = os.path.splitext(full_path)[1].lower()
    if ext and ext not in ALLOWED_EXTENSIONS:
        raise ValueError(
            f"Disallowed file extension '{ext}' in '{file_name}'. "
            f"Allowed: {sorted(ALLOWED_EXTENSIONS)}"
        )

    return full_path


_SCHEMA_LOOKUP: dict[str, dict[str, Any]] = {}
for _schema in TOOL_SCHEMAS:
    _SCHEMA_LOOKUP[_schema["name"]] = _schema


def _validate_tool_arguments(tool_name: str, arguments: dict[str, Any]) -> None:
    """
    [FIX M10] Validates tool arguments against the declared JSON schema.
    Raises ToolExecutionError for missing required params or type mismatches.
    Also detects JSON decode errors injected by llm_providers.py [FIX H3].
    """
    # [FIX H3] Detect JSON decode error marker from llm_providers
    if arguments.get("_json_decode_error"):
        raise ToolExecutionError(
            f"Tool call '{tool_name}' received malformed JSON arguments from the LLM. "
            f"Error: {arguments.get('_error_message', 'unknown')}. "
            f"Raw arguments: {arguments.get('_raw_arguments', '')[:300]}. "
            "Please regenerate the tool call with valid JSON."
        )

    schema = _SCHEMA_LOOKUP.get(tool_name)
    if schema is None:
        return  # Unknown tool — let dispatch handle it

    params_def = schema.get("parameters", {})
    required = params_def.get("required", [])
    properties = params_def.get("properties", {})

    # Check required parameters
    missing = [r for r in required if r not in arguments]
    if missing:
        raise ToolExecutionError(
            f"Tool '{tool_name}' is missing required parameter(s): {missing}. "
            f"Provided arguments: {list(arguments.keys())}. "
            f"Required: {required}."
        )

    # Basic type checking
    type_map: dict[str, Any] = {
        "integer": int,
        "number": (int, float),
        "string": str,
        "boolean": bool,
    }
    for key, value in arguments.items():
        if key.startswith("_"):
            continue  # Skip internal markers
        prop_def = properties.get(key)
        if prop_def is None:
            continue  # Extra param — will be ignored by handler
        expected_type = prop_def.get("type")
        if (
            expected_type
            and expected_type in type_map
            and not isinstance(value, type_map[expected_type])
        ):
            raise ToolExecutionError(
                f"Tool '{tool_name}' parameter '{key}' expected type "
                f"'{expected_type}' but got '{type(value).__name__}' "
                f"with value {value!r}."
            )
