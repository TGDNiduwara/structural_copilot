"""Centralized configuration. Values overridable via STRUCTURAL_AGENT_* env vars."""

from __future__ import annotations

import os
from typing import TypeVar, cast

T = TypeVar("T", int, float, str)


def _env(key: str, default: T) -> T:
    val = os.environ.get(f"STRUCTURAL_AGENT_{key}")
    if val is None:
        return default
    if isinstance(default, int):
        return cast(T, int(val))
    if isinstance(default, float):
        return cast(T, float(val))
    return cast(T, val)


# Agent Loop
MAX_AGENT_STEPS: int = _env("MAX_AGENT_STEPS", 60)
MAX_TOOL_CALLS_PER_STEP: int = _env("MAX_TOOL_CALLS_PER_STEP", 10)
MAX_ERROR_RETRIES: int = _env("MAX_ERROR_RETRIES", 3)
MAX_STUCK_PATTERN_COUNT: int = _env("MAX_STUCK_PATTERN_COUNT", 3)
MAX_TOOL_RESULT_CHARS: int = _env("MAX_TOOL_RESULT_CHARS", 600)

# LLM Client
MAX_HTTP_RETRIES: int = _env("MAX_HTTP_RETRIES", 3)
LLM_TIMEOUT_S: int = _env("LLM_TIMEOUT_S", 90)
LLM_TEMPERATURE: float = _env("LLM_TEMPERATURE", 0.2)
LLM_MAX_TOKENS: int = _env("LLM_MAX_TOKENS", 4000)

# Batch
DEFAULT_MAX_CANDIDATES: int = _env("DEFAULT_MAX_CANDIDATES", 50_000)
SOLVE_TIMEOUT_S: float = _env("SOLVE_TIMEOUT_S", 90.0)

# Artifacts
GENERATED_DIR: str = os.environ.get("STRUCTURAL_AGENT_GENERATED_DIR", "generated")

# Budget
DAILY_TOKEN_BUDGET: int = _env("DAILY_TOKEN_BUDGET", 100_000)
DAILY_COST_BUDGET_USD: float = _env("DAILY_COST_BUDGET_USD", 10.0)
