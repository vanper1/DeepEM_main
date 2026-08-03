from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from deepem.protocol import ToolResult


@dataclass(frozen=True, slots=True)
class CompressionOptions:
    top_items: int = 3
    snippet_chars: int = 240
    max_depth: int = 3


class ToolResultCompressor:
    def __init__(self, options: CompressionOptions | None = None) -> None:
        self.options = options or CompressionOptions()
        self._omitted: list[dict[str, str]] = []

    def compress(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, Any],
        tool_result: ToolResult,
        tool_call_id: str,
        preserve_retrieved_detail: bool = False,
    ) -> dict[str, Any]:
        self._omitted = []
        data = dict(tool_result.data)
        if preserve_retrieved_detail and tool_name == "retrieve_tool_result_detail":
            compact_data = data
        else:
            compact_data = self._compact(data, "data", 0)
        result = {
            "tool_result_id": tool_call_id,
            "tool_name": tool_name,
            "status": tool_result.status,
            "data": compact_data,
            "error": tool_result.error,
            "emitted_event_ids": list(tool_result.emitted_event_ids),
            "compressed": True,
            "raw_result_stored": True,
            "can_retrieve_more": bool(self._omitted),
            "omitted_fields": list(self._omitted),
        }
        if tool_name == "retrieve_tool_result_detail":
            result["terminal"] = bool(data.get("found")) and not bool(data.get("truncated"))
        return result

    def _compact(self, value: Any, path: str, depth: int) -> Any:
        if depth >= self.options.max_depth:
            self._omit(path, "maximum nesting depth")
            return "<omitted>"
        if isinstance(value, str):
            if len(value) <= self.options.snippet_chars:
                return value
            self._omit(path, "string truncated")
            return value[: self.options.snippet_chars - 3] + "..."
        if isinstance(value, Mapping):
            return {str(key): self._compact(item, f"{path}.{key}", depth + 1) for key, item in value.items()}
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            items = list(value)
            if len(items) > self.options.top_items:
                self._omit(path, f"kept first {self.options.top_items} of {len(items)} items")
            return [self._compact(item, f"{path}[{index}]", depth + 1) for index, item in enumerate(items[: self.options.top_items])]
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        text = json.dumps(str(value), ensure_ascii=False)
        self._omit(path, f"unsupported type {type(value).__name__}")
        return text[: self.options.snippet_chars]

    def _omit(self, path: str, reason: str) -> None:
        item = {"path": path, "reason": reason}
        if item not in self._omitted:
            self._omitted.append(item)
