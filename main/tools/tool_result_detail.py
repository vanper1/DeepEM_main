from __future__ import annotations

import json
import re
from dataclasses import asdict, is_dataclass
from typing import Any

from deepem.protocol import ToolResult
from deepem.tools.base import ToolContext, ToolDefinition, ToolExecutionResult


_PATH_PART_RE = re.compile(r"([A-Za-z_][\w-]*)(?:\[(\d+)\])?")
_DEFAULT_MAX_CHARS = 4000
_MIN_MAX_CHARS = 200
_MAX_MAX_CHARS = 12000


def build_retrieve_tool_result_detail_tool() -> ToolDefinition:
    return ToolDefinition(
        name="retrieve_tool_result_detail",
        description=(
            "按 tool_result_id 和字段路径取回已保存的原始工具结果字段。"
            "当 compact tool result 提示 can_retrieve_more 或 omitted_fields 中包含所需字段时调用。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "tool_result_id": {"type": "string"},
                "path": {"type": "string"},
                "max_chars": {"type": "integer"},
            },
            "required": ["tool_result_id", "path"],
            "additionalProperties": False,
        },
        handler=_retrieve_tool_result_detail,
    )


def _retrieve_tool_result_detail(args: dict[str, object], context: ToolContext) -> ToolExecutionResult:
    tool_result_id = str(args.get("tool_result_id") or "").strip()
    path = str(args.get("path") or "").strip()
    max_chars = _coerce_max_chars(args.get("max_chars"))

    if not tool_result_id or not path:
        return _result(tool_result_id=tool_result_id, path=path, found=False, error="invalid_arguments", max_chars=max_chars)
    if context.tool_call_repo is None:
        return _result(tool_result_id=tool_result_id, path=path, found=False, error="tool_call_repo_unavailable", max_chars=max_chars)

    try:
        tool_call = context.tool_call_repo.get(tool_result_id)
    except KeyError:
        return _result(tool_result_id=tool_result_id, path=path, found=False, error="tool_result_not_found", max_chars=max_chars)

    if tool_call.task_id != context.task.id or tool_call.conversation_id != context.run.conversation_id:
        return _result(tool_result_id=tool_result_id, path=path, found=False, error="forbidden", max_chars=max_chars)
    if tool_call.result is None:
        return _result(tool_result_id=tool_result_id, tool_name=tool_call.tool_name, path=path, found=False, error="result_not_available", max_chars=max_chars)

    root = _tool_result_root(tool_call.result)
    try:
        value = _resolve_path(root, path)
    except (KeyError, IndexError, TypeError, ValueError):
        return _result(tool_result_id=tool_result_id, tool_name=tool_call.tool_name, path=path, found=False, error="path_not_found", max_chars=max_chars)

    value_text = _stringify_value(value)
    clipped, truncated = _clip(value_text, max_chars)
    data = {
        "tool_result_id": tool_result_id,
        "tool_name": tool_call.tool_name,
        "path": path,
        "found": True,
        "value_type": type(value).__name__,
        "value": clipped,
        "truncated": truncated,
        "original_chars": len(value_text),
        "returned_chars": len(clipped),
        "max_chars": max_chars,
    }
    return ToolExecutionResult(result=ToolResult(status="success", data=data))


def _result(
    *,
    tool_result_id: str,
    path: str,
    found: bool,
    error: str,
    max_chars: int,
    tool_name: str | None = None,
) -> ToolExecutionResult:
    return ToolExecutionResult(
        result=ToolResult(
            status="success",
            data={
                "tool_result_id": tool_result_id,
                "tool_name": tool_name,
                "path": path,
                "found": found,
                "error": error,
                "max_chars": max_chars,
            },
        )
    )


def _coerce_max_chars(value: object) -> int:
    try:
        number = int(value) if value is not None else _DEFAULT_MAX_CHARS
    except Exception:
        number = _DEFAULT_MAX_CHARS
    return max(_MIN_MAX_CHARS, min(_MAX_MAX_CHARS, number))


def _tool_result_root(result: ToolResult) -> dict[str, Any]:
    return {
        "status": result.status,
        "data": result.data,
        "error": result.error,
        "metadata": result.metadata,
        "attachments": [_plain_value(item) for item in result.attachments],
        "emitted_event_ids": result.emitted_event_ids,
    }


def _resolve_path(root: Any, path: str) -> Any:
    current = root
    for raw_part in path.split("."):
        if not raw_part:
            raise ValueError(path)
        match = _PATH_PART_RE.fullmatch(raw_part)
        if not match:
            raise ValueError(path)
        key, index_text = match.groups()
        if not isinstance(current, dict) or key not in current:
            raise KeyError(key)
        current = current[key]
        if index_text is not None:
            if not isinstance(current, list):
                raise TypeError(key)
            current = current[int(index_text)]
    return current


def _stringify_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(_plain_value(value), ensure_ascii=False, sort_keys=True, default=str)


def _plain_value(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, dict):
        return {str(key): _plain_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain_value(item) for item in value]
    return value


def _clip(text: str, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    return text[: max(0, max_chars - 3)] + "...", True
