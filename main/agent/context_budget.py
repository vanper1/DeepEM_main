from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ContextBudgetOptions:
    token_limit: int = 30000
    reserved_output_tokens: int = 4096
    safety_margin_tokens: int = 512
    warning_ratio: float = 0.65
    danger_ratio: float = 0.80


class ContextBudgetEstimator:
    def __init__(self, options: ContextBudgetOptions | None = None) -> None:
        self.options = options or ContextBudgetOptions()

    def estimate(self, messages: list[dict[str, Any]], *, tools: list[Any] | None = None) -> dict[str, Any]:
        serialized = json.dumps({"messages": messages, "tools": tools or []}, ensure_ascii=False, default=str)
        estimated_chars = len(serialized)
        estimated_tokens = max(1, estimated_chars // 4)
        usable = max(1, self.options.token_limit - self.options.reserved_output_tokens - self.options.safety_margin_tokens)
        ratio = estimated_tokens / usable
        status = "danger" if ratio >= self.options.danger_ratio else "warning" if ratio >= self.options.warning_ratio else "ok"
        return {
            "status": status,
            "estimated_chars": estimated_chars,
            "estimated_tokens": estimated_tokens,
            "token_limit": self.options.token_limit,
            "usable_input_tokens": usable,
            "reserved_output_tokens": self.options.reserved_output_tokens,
            "safety_margin_tokens": self.options.safety_margin_tokens,
            "token_usage_ratio": ratio,
            "method": "json_chars_v1",
        }
