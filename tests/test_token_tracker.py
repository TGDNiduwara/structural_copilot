"""Tests for agent.token_tracker.TokenTracker (Step 11)."""

from __future__ import annotations

from agent.token_tracker import TokenTracker


def test_usage_accumulates():
    t = TokenTracker(daily_token_budget=1000, daily_cost_budget_usd=10.0)
    t.add_usage(prompt_tokens=10, completion_tokens=20)
    t.add_usage(prompt_tokens=5, completion_tokens=5)
    assert t.total_tokens == 40
    assert not t.is_over_budget()


def test_token_budget_exceeded():
    t = TokenTracker(daily_token_budget=100, daily_cost_budget_usd=10.0)
    t.add_usage(prompt_tokens=60, completion_tokens=40)
    assert t.is_over_budget()


def test_cost_budget_exceeded():
    t = TokenTracker(daily_token_budget=100000, daily_cost_budget_usd=1.0)
    t.add_usage(prompt_tokens=1, completion_tokens=1, cost_usd=1.5)
    assert t.is_over_budget()


def test_summary_fields():
    t = TokenTracker(daily_token_budget=500, daily_cost_budget_usd=2.0)
    t.add_usage(prompt_tokens=3, completion_tokens=7)
    s = t.get_usage_summary()
    assert s["total_tokens"] == 10
    assert s["over_budget"] is False
    assert s["token_budget"] == 500


def test_persistence_roundtrip(tmp_path):
    path = str(tmp_path / "usage.json")
    t = TokenTracker(daily_token_budget=100, daily_cost_budget_usd=1.0, history_path=path)
    t.add_usage(prompt_tokens=11, completion_tokens=12)
    t2 = TokenTracker(daily_token_budget=100, daily_cost_budget_usd=1.0, history_path=path)
    assert t2.total_tokens == 23
