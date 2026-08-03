from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ContextCompressionOptions:
    keep_recent_messages: int = 8
    max_summary_chars: int = 6000


class ConversationContextCompressor:
    def __init__(self, options: ContextCompressionOptions | None = None, *, keep_recent_messages: int | None = None) -> None:
        self.options = options or ContextCompressionOptions()
        if keep_recent_messages is not None:
            self.options = ContextCompressionOptions(keep_recent_messages=keep_recent_messages)

    def build_summary_message(self, messages: list[Any]) -> dict[str, str] | None:
        older = list(messages)[: max(0, len(messages) - self.options.keep_recent_messages)]
        if not older:
            return None
        entries = []
        for item in older:
            role = getattr(item, "role", "message")
            role = getattr(role, "value", role)
            content = str(getattr(item, "content", "") or "").strip()
            if content:
                entries.append({"role": role, "content": content})
        if not entries:
            return None
        content = json.dumps({"summary_type": "conversation_context_summary", "messages": entries}, ensure_ascii=False)
        return {"role": "system", "content": content[: self.options.max_summary_chars]}
