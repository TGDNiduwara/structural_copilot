"""Daily token / cost budget tracker for LLM calls.

[FIX 11] Records prompt + completion token usage (and an optional cost in USD)
and reports whether the configured daily budgets have been exceeded, so the
agent loop can stop early instead of racking up unbounded spend.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from config import DAILY_COST_BUDGET_USD, DAILY_TOKEN_BUDGET


class TokenTracker:
    """In-memory accumulator (optionally persisted to a JSON file)."""

    def __init__(
        self,
        daily_token_budget: int = DAILY_TOKEN_BUDGET,
        daily_cost_budget_usd: float = DAILY_COST_BUDGET_USD,
        history_path: str | None = None,
    ):
        self.daily_token_budget = int(daily_token_budget)
        self.daily_cost_budget_usd = float(daily_cost_budget_usd)
        self._day = date.today().isoformat()
        self._prompt_tokens = 0
        self._completion_tokens = 0
        self._cost_usd = 0.0
        self.history_path = history_path
        if history_path:
            self._load()

    def _load(self) -> None:
        if not self.history_path:
            return
        try:
            with open(self.history_path, encoding="utf-8") as fh:
                data = json.load(fh)
            if data.get("day") == self._day:
                self._prompt_tokens = int(data.get("prompt_tokens", 0))
                self._completion_tokens = int(data.get("completion_tokens", 0))
                self._cost_usd = float(data.get("cost_usd", 0.0))
        except (OSError, ValueError, TypeError):
            pass  # fresh day / missing file is fine

    def _persist(self) -> None:
        if not self.history_path:
            return
        try:
            with open(self.history_path, "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "day": self._day,
                        "prompt_tokens": self._prompt_tokens,
                        "completion_tokens": self._completion_tokens,
                        "cost_usd": self._cost_usd,
                    },
                    fh,
                )
        except OSError:
            pass

    def add_usage(
        self,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cost_usd: float = 0.0,
    ) -> None:
        self._prompt_tokens += max(0, int(prompt_tokens or 0))
        self._completion_tokens += max(0, int(completion_tokens or 0))
        self._cost_usd += max(0.0, float(cost_usd or 0.0))
        self._persist()

    @property
    def total_tokens(self) -> int:
        return self._prompt_tokens + self._completion_tokens

    def is_over_budget(self) -> bool:
        if self.total_tokens >= self.daily_token_budget:
            return True
        return self._cost_usd >= self.daily_cost_budget_usd

    def get_usage_summary(self) -> dict[str, Any]:
        return {
            "day": self._day,
            "prompt_tokens": self._prompt_tokens,
            "completion_tokens": self._completion_tokens,
            "total_tokens": self.total_tokens,
            "estimated_cost_usd": round(self._cost_usd, 4),
            "token_budget": self.daily_token_budget,
            "cost_budget_usd": self.daily_cost_budget_usd,
            "over_budget": self.is_over_budget(),
        }
