from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping


class ChatMode(StrEnum):
    GENERAL = "general"
    WORKSPACE = "workspace"


GENERAL_HISTORY_LIMIT = 12
GENERAL_HISTORY_TOTAL_CHARS = 8000
GENERAL_HISTORY_PER_MESSAGE_CHARS = 2000

GENERAL_LLM_OVERRIDES: dict[str, Any] = {
    "enable_thinking": False,
    "preserve_thinking": False,
    "temperature": 0.3,
    "top_p": 0.8,
    "presence_penalty": 0.0,
}
GENERAL_LLM_REMOVED_OPTIONS = frozenset({"thinking_token_budget", "reasoning_effort"})
POLICY_VISIBLE_LLM_OPTIONS = frozenset(
    {
        "temperature",
        "top_p",
        "top_k",
        "min_p",
        "presence_penalty",
        "repetition_penalty",
        "enable_thinking",
        "preserve_thinking",
        "reasoning_effort",
        "thinking_token_budget",
    }
)


def effective_llm_options(chat_mode: ChatMode | str, requested: Mapping[str, Any] | None) -> dict[str, Any]:
    mode = ChatMode(chat_mode)
    options = dict(requested or {})
    if mode is ChatMode.WORKSPACE:
        return options
    for key in GENERAL_LLM_REMOVED_OPTIONS:
        options.pop(key, None)
    options.update(GENERAL_LLM_OVERRIDES)
    return options


def visible_llm_options(options: Mapping[str, Any] | None) -> dict[str, Any]:
    return {key: value for key, value in dict(options or {}).items() if key in POLICY_VISIBLE_LLM_OPTIONS}


@dataclass(frozen=True, slots=True)
class ExecutionPolicy:
    chat_mode: ChatMode
    workspace_injected: bool
    available_tool_count: int
    effective_llm_options: dict[str, Any]
    workspace_snapshot_injected: bool = False
    tool_transcript_included: bool = False
    retrieval_context_included: bool = False
    history_message_count: int = 0

    def to_event(self) -> dict[str, Any]:
        return {
            "chat_mode": self.chat_mode.value,
            "workspace_injected": self.workspace_injected,
            "workspace_snapshot_injected": self.workspace_snapshot_injected,
            "tool_transcript_included": self.tool_transcript_included,
            "retrieval_context_included": self.retrieval_context_included,
            "history_message_count": self.history_message_count,
            "available_tool_count": self.available_tool_count,
            "tool_count": self.available_tool_count,
            "effective_llm_options": dict(self.effective_llm_options),
        }


def build_execution_policy(
    *,
    chat_mode: ChatMode | str,
    available_tool_count: int,
    effective_options: Mapping[str, Any],
    history_message_count: int = 0,
) -> ExecutionPolicy:
    mode = ChatMode(chat_mode)
    is_workspace = mode is ChatMode.WORKSPACE
    return ExecutionPolicy(
        chat_mode=mode,
        workspace_injected=is_workspace,
        available_tool_count=available_tool_count,
        effective_llm_options=visible_llm_options(effective_options),
        workspace_snapshot_injected=is_workspace,
        tool_transcript_included=is_workspace,
        retrieval_context_included=is_workspace,
        history_message_count=history_message_count,
    )


def project_general_history(
    messages: list[Any] | tuple[Any, ...],
    *,
    limit: int = GENERAL_HISTORY_LIMIT,
    total_chars: int = GENERAL_HISTORY_TOTAL_CHARS,
    per_message_chars: int = GENERAL_HISTORY_PER_MESSAGE_CHARS,
) -> list[dict[str, str]]:
    projected: list[dict[str, str]] = []
    for message in messages:
        role = getattr(message, "role", None)
        role_value = getattr(role, "value", role)
        if role_value not in {"operator", "assistant", "user"}:
            continue
        metadata = dict(getattr(message, "metadata", None) or {})
        if metadata.get("synthetic_summary") or metadata.get("synthetic_context_summary") or metadata.get("is_synthetic"):
            continue
        content = str(getattr(message, "content", None) or "").strip()
        if content:
            projected.append(
                {
                    "role": "user" if role_value in {"operator", "user"} else "assistant",
                    "content": content[: max(0, per_message_chars)],
                }
            )
    if limit > 0:
        projected = projected[-limit:]
    while len(projected) > 1 and sum(len(item["content"]) for item in projected) > max(0, total_chars):
        projected.pop(0)
    if projected and total_chars >= 0:
        overflow = sum(len(item["content"]) for item in projected) - total_chars
        if overflow > 0:
            projected[-1]["content"] = projected[-1]["content"][: max(0, len(projected[-1]["content"]) - overflow)]
            if not projected[-1]["content"]:
                projected.pop()
    return projected
