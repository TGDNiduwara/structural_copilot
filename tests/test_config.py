"""Config sanity tests. Values must default per spec and honor env overrides."""

from __future__ import annotations

import importlib

import pytest

import config


@pytest.fixture(autouse=True)
def _reload_config_after():
    """Reset module state so env overrides never leak into other tests."""
    yield
    importlib.reload(config)


def test_max_agent_steps_default():
    assert config.MAX_AGENT_STEPS == 60


def test_max_agent_steps_env_override(monkeypatch):
    monkeypatch.setenv("STRUCTURAL_AGENT_MAX_AGENT_STEPS", "5")
    importlib.reload(config)
    assert config.MAX_AGENT_STEPS == 5


def test_max_tool_calls_per_step_default():
    assert config.MAX_TOOL_CALLS_PER_STEP == 10


def test_max_tool_calls_per_step_env_override(monkeypatch):
    monkeypatch.setenv("STRUCTURAL_AGENT_MAX_TOOL_CALLS_PER_STEP", "4")
    importlib.reload(config)
    assert config.MAX_TOOL_CALLS_PER_STEP == 4


def test_max_error_retries_default():
    assert config.MAX_ERROR_RETRIES == 3


def test_max_stuck_pattern_count_default():
    assert config.MAX_STUCK_PATTERN_COUNT == 3


def test_max_tool_result_chars_default():
    assert config.MAX_TOOL_RESULT_CHARS == 600


def test_max_http_retries_default():
    assert config.MAX_HTTP_RETRIES == 3
