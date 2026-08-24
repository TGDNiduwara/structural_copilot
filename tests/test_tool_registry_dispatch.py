"""Offline (no Robot) dispatch tests covering the domain-split handlers."""

from __future__ import annotations

import json

import pytest

from agent.tool_registry import TOOL_SCHEMAS, ToolExecutor


@pytest.fixture(scope="module")
def executor() -> ToolExecutor:
    return ToolExecutor()


def _call(executor: ToolExecutor, name: str, args: dict):
    return json.loads(executor.dispatch(name, args))


def test_every_schema_has_handler():
    from agent import tool_registry as tr

    for schema in TOOL_SCHEMAS:
        assert schema["name"] in tr._HANDLERS, schema["name"]


def test_check_section_proportions(executor):
    r = _call(executor, "check_section_proportions", {"spec": {"section": "IPE 300"}})
    assert r["status"] == "ok"


def test_list_available_sections(executor):
    r = _call(executor, "list_available_sections", {})
    assert r["status"] == "ok"
    assert r["count"] > 0


def test_run_custom_script(executor):
    r = _call(executor, "run_custom_script", {"code": "result = {'a': 2 + 2}"})
    assert r["status"] == "ok"
    assert r.get("result") == {"a": 4}


def test_custom_tool_create_and_call(executor):
    _call(
        executor,
        "create_custom_tool",
        {"name": "probe_tool", "description": "probe", "code": "result = {'ok': True}"},
    )
    r = _call(executor, "call_custom_tool", {"tool_name": "probe_tool", "arguments": {}})
    assert "ok" in json.dumps(r)


def test_unknown_tool_raises(executor):
    from agent.tool_registry import ToolExecutionError

    with pytest.raises(ToolExecutionError):
        executor.dispatch("no_such_tool_xyz", {})


def test_validate_required_param(executor):
    from agent.tool_registry import ToolExecutionError

    with pytest.raises(ToolExecutionError):
        executor.dispatch("check_section_proportions", {})
