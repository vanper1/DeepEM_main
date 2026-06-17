from __future__ import annotations

import json
import os
from dataclasses import dataclass, field


@dataclass(slots=True)
class LLMSettings:
    model: str
    base_url: str
    api_key: str
    timeout_seconds: float = 60.0
    temperature: float = 0.2
    extra_headers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls, prefix: str = "DEEPEM_LLM_") -> "LLMSettings":
        model = os.getenv(f"{prefix}MODEL") or os.getenv("DEEPSEEK_MODEL") or "deepseek-chat"
        base_url = os.getenv(f"{prefix}BASE_URL") or os.getenv("DEEPSEEK_BASE_URL") or "https://api.deepseek.com/v1"
        api_key = os.getenv(f"{prefix}API_KEY") or os.getenv("DEEPSEEK_API_KEY")
        timeout_value = os.getenv(f"{prefix}TIMEOUT") or os.getenv("DEEPSEEK_TIMEOUT") or "60"
        temperature_value = os.getenv(f"{prefix}TEMPERATURE") or os.getenv("DEEPSEEK_TEMPERATURE") or "0.2"
        headers_value = os.getenv(f"{prefix}EXTRA_HEADERS", "{}")
        missing = [
            key
            for key, value in {
                f"{prefix}MODEL or DEEPSEEK_MODEL": model,
                f"{prefix}BASE_URL or DEEPSEEK_BASE_URL": base_url,
                f"{prefix}API_KEY or DEEPSEEK_API_KEY": api_key,
            }.items()
            if not value
        ]
        if missing:
            raise ValueError(f"Missing LLM configuration: {', '.join(missing)}")
        return cls(
            model=model,
            base_url=base_url.rstrip("/"),
            api_key=api_key,
            timeout_seconds=float(timeout_value),
            temperature=float(temperature_value),
            extra_headers=json.loads(headers_value),
        )
