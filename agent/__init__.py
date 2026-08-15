"""
agent package
=============
LLM orchestration layer: multi-provider chat client (llm_providers) and the
tool schema + dispatcher that binds LLM tool calls to the engineering
automation bridges (tool_registry).
"""

from .llm_providers import call_llm, PROVIDERS, LLMResponse, ToolCall, LLMProviderError
from .tool_registry import ToolExecutor, TOOL_SCHEMAS, ToolExecutionError

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
