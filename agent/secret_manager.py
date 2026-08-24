"""Centralized secret lookup for API keys and credentials.

[FIX 15] Resolves a secret from (in order):
  1. os.environ (set via .env / shell / deployment platform)
  2. Streamlit secrets (streamlit secrets.toml)
  3. a .env file in the project root
Never stores secrets in session_state, and never falls back to them.
"""

from __future__ import annotations

import os
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_ENV_FILE = _PROJECT_ROOT / ".env"


def _load_env_file(path: Path = _ENV_FILE) -> dict[str, str]:
    """Parse KEY=VALUE lines (no external dependency, mirrors app.load_env_file)."""
    result: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            result[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        pass  # no .env is fine
    return result


def get_secret(key: str) -> str | None:
    """Return the secret value for `key`, or None if not configured anywhere."""
    if not key:
        return None
    val = os.environ.get(key)
    if val:
        return val
    try:
        import streamlit as st  # lazy: streamlit may be absent in non-UI contexts

        if hasattr(st, "secrets") and key in getattr(st, "secrets", {}):
            return str(st.secrets[key])
    except Exception:
        pass
    return _load_env_file().get(key)


def resolve_llm_key(provider: str, fallback_key: str = "") -> str:
    """Resolve the provider API key via its canonical env var name.

    A key typed directly into the UI (fallback_key) takes precedence so custom
    entries keep working; otherwise the canonical secret is resolved from
    os.environ / streamlit secrets / .env.
    """
    if fallback_key:
        return fallback_key
    from agent.llm_providers import API_KEY_ENV_VARS  # lazy (avoids import cycle)

    env_key = API_KEY_ENV_VARS.get(provider, "")
    return get_secret(env_key) or ""
