"""Tests for agent.secret_manager (Step 15)."""

from __future__ import annotations

from agent.secret_manager import get_secret, resolve_llm_key


def test_get_secret_from_env(monkeypatch):
    monkeypatch.setenv("MY_TEST_KEY", "abc123")
    assert get_secret("MY_TEST_KEY") == "abc123"


def test_get_secret_missing():
    assert get_secret("NO_SUCH_KEY_XYZ") is None


def test_resolve_llm_key_fallback():
    assert resolve_llm_key("DeepSeek", "typed-key") == "typed-key"
