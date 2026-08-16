"""
agent/llm_providers.py
=======================
Unified chat-completion + tool-calling client that speaks to five backends
with a single interface:

    - OpenAI            (api.openai.com,        OpenAI-style function calling)
    - OpenRouter         (openrouter.ai,         OpenAI-compatible endpoint)
    - Google AI Studio   (generativelanguage.googleapis.com, Gemini function calling)
    - Z.AI (GLM) Standard
    - Z.AI (GLM) Coding Plan

The rest of the application only ever deals with the normalized
`LLMResponse` / `ToolCall` dataclasses below, so `app.py` and the tool
executor never need to know which backend answered.
"""

from __future__ import annotations

import base64
import json
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import requests

logger = logging.getLogger("structural_copilot.llm_providers")
logger.setLevel(logging.INFO)


# --------------------------------------------------------------------------
# Normalized data structures
# --------------------------------------------------------------------------

@dataclass
class ToolCall:
    id: str
    name: str
    arguments: Dict[str, Any]


@dataclass
class LLMResponse:
    content: str
    tool_calls: List[ToolCall] = field(default_factory=list)
    raw: Optional[dict] = None
    finish_reason: str = "stop"


class LLMProviderError(RuntimeError):
    """Raised for any transport / auth / malformed-response error talking to a provider."""


# --------------------------------------------------------------------------
# Provider constants
# --------------------------------------------------------------------------

PROVIDERS = {
    "OpenAI": {
        "base_url": "https://api.openai.com/v1/chat/completions",
        "default_model": "gpt-4o",
        "style": "openai",
    },
    "OpenRouter": {
        "base_url": "https://openrouter.ai/api/v1/chat/completions",
        "default_model": "openai/gpt-4o",
        "style": "openai",
    },
    "Google AI Studio": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/models",
        "default_model": "gemini-1.5-pro",
        "style": "gemini",
    },
    "Z.AI (GLM) - Standard API": {
        "base_url": "https://api.z.ai/api/paas/v4/chat/completions",
        "default_model": "glm-5.2",
        "style": "openai",
    },
    "Z.AI (GLM) - Coding Plan": {
        "base_url": "https://api.z.ai/api/coding/paas/v4/chat/completions",
        "default_model": "glm-5.2",
        "style": "openai",
    },
    "DeepSeek": {
        "base_url": "https://api.deepseek.com/chat/completions",
        "default_model": "deepseek-chat",
        "style": "openai",
    },
    "Custom (OpenAI-compatible)": {
        # Leave base_url empty: it is supplied interactively in the sidebar so
        # you can point the agent at ANY OpenAI-compatible endpoint (Ollama,
        # LM Studio, vLLM, Together, Groq, Azure OpenAI, a local proxy, etc.)
        # with any model name.
        "base_url": "",
        "default_model": "",
        "style": "openai",
    },
}

# [.env] Map each provider to the env var holding its API key. The sidebar
# pre-fills the matching key when that provider is selected (still editable).
# Custom endpoints have no fixed key env var - the user enters it in the UI.
API_KEY_ENV_VARS = {
    "OpenAI": "OPENAI_API_KEY",
    "OpenRouter": "OPENROUTER_API_KEY",
    "Google AI Studio": "GEMINI_API_KEY",
    "Z.AI (GLM) - Standard API": "ZAI_STANDARD_API_KEY",
    "Z.AI (GLM) - Coding Plan": "ZAI_CODING_API_KEY",
    "DeepSeek": "DEEPSEEK_API_KEY",
    "Custom (OpenAI-compatible)": "",
}


# --------------------------------------------------------------------------
# [FIX H4] Retry logic for transient HTTP errors
# --------------------------------------------------------------------------

MAX_HTTP_RETRIES = 3
RETRYABLE_STATUS_CODES = {429, 502, 503, 504}


def _request_with_retry(
    method_func,
    url: str,
    headers: Dict[str, str],
    payload: Dict[str, Any],
    timeout_s: int,
    provider_name: str = "",
) -> requests.Response:
    """
    Executes an HTTP request with exponential backoff + jitter for transient
    errors (429, 502, 503, 504). Respects Retry-After header for 429.
    """
    last_resp = None
    for attempt in range(MAX_HTTP_RETRIES + 1):
        try:
            resp = method_func(url, headers=headers, json=payload, timeout=timeout_s)
        except requests.RequestException as exc:
            # Connection-level errors: retry once, then give up
            if attempt < MAX_HTTP_RETRIES and attempt < 1:
                delay = (2 ** attempt) + random.uniform(0.5, 1.5)
                logger.warning(
                    "%s connection error (attempt %d/%d), retrying in %.1fs: %s",
                    provider_name, attempt + 1, MAX_HTTP_RETRIES + 1, delay, exc,
                )
                time.sleep(delay)
                continue
            raise LLMProviderError(f"{provider_name} request failed: {exc}") from exc

        if resp.status_code < 400 or resp.status_code not in RETRYABLE_STATUS_CODES:
            return resp

        # Transient server error — retry with backoff
        if attempt < MAX_HTTP_RETRIES:
            delay = (2 ** attempt) + random.uniform(0.5, 1.5)
            # Respect Retry-After for rate limits
            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                if retry_after:
                    try:
                        delay = max(delay, float(retry_after))
                    except ValueError:
                        pass
            logger.warning(
                "%s returned HTTP %d (attempt %d/%d), retrying in %.1fs",
                provider_name, resp.status_code, attempt + 1,
                MAX_HTTP_RETRIES + 1, delay,
            )
            time.sleep(delay)
            last_resp = resp
            continue

        # Exhausted retries
        return resp

    # Should not reach here, but just in case
    return last_resp  # type: ignore[return-value]


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------

def _build_image_content(
    messages: List[Dict[str, Any]],
    attachments: Optional[List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    """
    [ATTACH] Converts the LAST user message into an OpenAI-style multimodal
    content list (text + image_url data-URL parts) when image attachments are
    present. Returns the original list untouched when there is nothing to
    attach. Non-OpenAI providers build their own part formats.
    """
    if not attachments:
        return messages
    msgs = [dict(m) for m in messages]
    for i in range(len(msgs) - 1, -1, -1):
        if msgs[i].get("role") == "user":
            content = msgs[i].get("content") or ""
            if not isinstance(content, str):
                break
            parts: List[Dict[str, Any]] = [{"type": "text", "text": content}]
            for a in attachments:
                mime = a.get("mime") or "image/png"
                b64 = base64.b64encode(a["bytes"]).decode("ascii")
                parts.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{b64}"},
                })
            msgs[i]["content"] = parts
            break
    return msgs


def _apply_gemini_attachments(
    contents: List[dict],
    attachments: Optional[List[Dict[str, Any]]],
) -> List[dict]:
    """
    [ATTACH] Appends inline_data (base64) image parts to the last user turn
    for Gemini's generateContent format.
    """
    if not attachments:
        return contents
    for i in range(len(contents) - 1, -1, -1):
        if contents[i].get("role") == "user":
            parts = contents[i].setdefault("parts", [])
            for a in attachments:
                parts.append({
                    "inline_data": {
                        "mime_type": a.get("mime") or "image/png",
                        "data": base64.b64encode(a["bytes"]).decode("ascii"),
                    }
                })
            break
    return contents


def call_llm(
    provider: str,
    model: str,
    api_key: str,
    messages: List[Dict[str, Any]],
    tool_schemas: List[Dict[str, Any]],
    temperature: float = 0.2,
    max_tokens: int = 4000,
    timeout_s: int = 90,
    base_url: Optional[str] = None,
    attachments: Optional[List[Dict[str, Any]]] = None,
) -> LLMResponse:
    """
    Dispatches a chat-completion + tool-calling request to the selected
    provider and returns a normalized LLMResponse.

    `base_url` optionally overrides the provider's registered endpoint
    (used by the "Custom (OpenAI-compatible)" provider so the user can point
    the agent at any OpenAI-style chat/completions service / local server).
    """
    if provider not in PROVIDERS:
        raise LLMProviderError(f"Unknown provider '{provider}'.")

    style = PROVIDERS[provider]["style"]

    if style == "openai":
        return _call_openai_compatible(
            provider=provider,
            model=model,
            api_key=api_key,
            messages=messages,
            tool_schemas=tool_schemas,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout_s=timeout_s,
            base_url=base_url,
            attachments=attachments,
        )
    elif style == "gemini":
        return _call_gemini(
            model=model,
            api_key=api_key,
            messages=messages,
            tool_schemas=tool_schemas,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout_s=timeout_s,
            attachments=attachments,
        )
    else:  # pragma: no cover
        raise LLMProviderError(f"Unsupported provider style '{style}'.")


# --------------------------------------------------------------------------
# OpenAI / OpenRouter (OpenAI-compatible /chat/completions)
# --------------------------------------------------------------------------

def _call_openai_compatible(
    provider: str,
    model: str,
    api_key: str,
    messages: List[Dict[str, Any]],
    tool_schemas: List[Dict[str, Any]],
    temperature: float,
    max_tokens: int,
    timeout_s: int,
    base_url: Optional[str] = None,
    attachments: Optional[List[Dict[str, Any]]] = None,
) -> LLMResponse:
    url = base_url or PROVIDERS[provider]["base_url"]

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if provider == "OpenRouter":
        headers["HTTP-Referer"] = "https://structural-copilot.local"
        headers["X-Title"] = "Structural Multi-App Agent"

    # [ATTACH] Multimodal user message when image attachments are present.
    messages = _build_image_content(messages, attachments)

    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if tool_schemas:
        payload["tools"] = [{"type": "function", "function": schema} for schema in tool_schemas]
        payload["tool_choice"] = "auto"

    # [FIX H4] Use retry wrapper instead of bare requests.post
    resp = _request_with_retry(
        requests.post, url, headers, payload, timeout_s, provider_name=provider,
    )

    if resp.status_code >= 400:
        hint = ""
        if attachments:
            hint = (" The attached image may not be supported by this model/"
                    "endpoint — try a vision-capable model or describe the "
                    "sketch in text.")
        raise LLMProviderError(f"{provider} returned HTTP {resp.status_code}: {resp.text[:500]}{hint}")

    # [FIX M7] Handle JSON decode errors gracefully
    try:
        data = resp.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise LLMProviderError(
            f"{provider} returned non-JSON response (HTTP {resp.status_code}): "
            f"{resp.text[:500]}"
        ) from exc

    try:
        choice = data["choices"][0]
        message = choice["message"]
    except (KeyError, IndexError) as exc:
        raise LLMProviderError(f"{provider} returned an unexpected payload shape: {data}") from exc

    content = message.get("content") or ""
    finish_reason = choice.get("finish_reason", "stop")

    tool_calls: List[ToolCall] = []
    for tc in message.get("tool_calls") or []:
        try:
            args = json.loads(tc["function"]["arguments"] or "{}")
        except json.JSONDecodeError as exc:
            # [FIX H3] Log the error and include error marker instead of silent empty dict
            logger.error(
                "Malformed JSON in tool call '%s' arguments: %s — raw: %r",
                tc["function"]["name"], exc, tc["function"]["arguments"][:200],
            )
            args = {
                "_json_decode_error": True,
                "_error_message": str(exc),
                "_raw_arguments": tc["function"]["arguments"][:500],
            }
        tool_calls.append(
            ToolCall(id=tc["id"], name=tc["function"]["name"], arguments=args)
        )

    return LLMResponse(content=content, tool_calls=tool_calls, raw=data, finish_reason=finish_reason)


# --------------------------------------------------------------------------
# Google AI Studio (Gemini generateContent + functionCall)
# --------------------------------------------------------------------------

def _openai_messages_to_gemini(messages: List[Dict[str, Any]]) -> tuple[Optional[str], List[dict]]:
    """
    Converts an OpenAI-style message list into Gemini's `contents` array plus
    an extracted system instruction string.
    """
    system_instruction = None
    contents: List[dict] = []

    for msg in messages:
        role = msg["role"]

        if role == "system":
            system_instruction = (system_instruction or "") + msg["content"] + "\n"
            continue

        if role == "tool":
            # Gemini expects tool results as a "user" turn containing a functionResponse part
            contents.append(
                {
                    "role": "user",
                    "parts": [
                        {
                            "functionResponse": {
                                "name": msg.get("name", "tool_result"),
                                "response": {"result": msg["content"]},
                            }
                        }
                    ],
                }
            )
            continue

        gemini_role = "model" if role == "assistant" else "user"

        parts = []
        if msg.get("content"):
            parts.append({"text": msg["content"]})

        for tc in msg.get("tool_calls") or []:
            try:
                tc_args = json.loads(tc["function"]["arguments"] or "{}")
            except json.JSONDecodeError:
                tc_args = {}
            parts.append(
                {
                    "functionCall": {
                        "name": tc["function"]["name"],
                        "args": tc_args,
                    }
                }
            )

        if not parts:
            parts = [{"text": ""}]

        contents.append({"role": gemini_role, "parts": parts})

    return system_instruction, contents


def _openai_schema_to_gemini_tools(tool_schemas: List[Dict[str, Any]]) -> List[dict]:
    if not tool_schemas:
        return []
    function_declarations = []
    for schema in tool_schemas:
        function_declarations.append(
            {
                "name": schema["name"],
                "description": schema.get("description", ""),
                "parameters": schema.get("parameters", {"type": "object", "properties": {}}),
            }
        )
    return [{"functionDeclarations": function_declarations}]


def _call_gemini(
    model: str,
    api_key: str,
    messages: List[Dict[str, Any]],
    tool_schemas: List[Dict[str, Any]],
    temperature: float,
    max_tokens: int,
    timeout_s: int,
    attachments: Optional[List[Dict[str, Any]]] = None,
) -> LLMResponse:
    base_url = PROVIDERS["Google AI Studio"]["base_url"]
    # [FIX L2] URL-encode the model name to handle special characters safely
    url = f"{base_url}/{quote(model, safe='')}:generateContent?key={api_key}"

    system_instruction, contents = _openai_messages_to_gemini(messages)
    # [ATTACH] Gemini inline_data parts for image attachments.
    contents = _apply_gemini_attachments(contents, attachments)
    tools = _openai_schema_to_gemini_tools(tool_schemas)

    payload: Dict[str, Any] = {
        "contents": contents,
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": max_tokens,
        },
    }
    if tools:
        payload["tools"] = tools
    if system_instruction:
        payload["systemInstruction"] = {"parts": [{"text": system_instruction}]}

    # [FIX H4] Use retry wrapper
    resp = _request_with_retry(
        requests.post, url,
        headers={"Content-Type": "application/json"},
        payload=payload,
        timeout_s=timeout_s,
        provider_name="Google AI Studio",
    )

    if resp.status_code >= 400:
        raise LLMProviderError(
            f"Google AI Studio returned HTTP {resp.status_code}: {resp.text[:500]}"
        )

    # [FIX M7] Handle JSON decode errors gracefully
    try:
        data = resp.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise LLMProviderError(
            f"Google AI Studio returned non-JSON response (HTTP {resp.status_code}): "
            f"{resp.text[:500]}"
        ) from exc

    try:
        candidate = data["candidates"][0]
        parts = candidate["content"]["parts"]
    except (KeyError, IndexError) as exc:
        raise LLMProviderError(f"Google AI Studio returned an unexpected payload: {data}") from exc

    content_text_chunks: List[str] = []
    tool_calls: List[ToolCall] = []

    for i, part in enumerate(parts):
        if "text" in part:
            content_text_chunks.append(part["text"])
        elif "functionCall" in part:
            fc = part["functionCall"]
            tool_calls.append(
                ToolCall(id=f"gemini_call_{i}", name=fc["name"], arguments=fc.get("args", {}))
            )

    finish_reason = candidate.get("finishReason", "STOP").lower()

    return LLMResponse(
        content="".join(content_text_chunks),
        tool_calls=tool_calls,
        raw=data,
        finish_reason=finish_reason,
    )
