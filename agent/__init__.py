"""
agent package
=============
LLM orchestration layer: multi-provider chat client (llm_providers) and the
tool schema + dispatcher that binds LLM tool calls to the engineering
automation bridges (tool_registry).
"""

from .llm_providers import PROVIDERS, LLMProviderError, LLMResponse, ToolCall, call_llm
from .tool_registry import TOOL_SCHEMAS, ToolExecutionError, ToolExecutor

__all__ = [
    "call_llm",
    "PROVIDERS",
    "LLMResponse",
    "ToolCall",
    "LLMProviderError",
    "ToolExecutor",
    "TOOL_SCHEMAS",
    "ToolExecutionError",
]
