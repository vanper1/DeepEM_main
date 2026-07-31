from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(slots=True)
class HistoryUnit:
    unit_id: str
    messages: list[dict[str, Any]]
    source_message_ids: tuple[str, ...]

    @property
    def message_count(self) -> int:
        return len(dict.fromkeys(self.source_message_ids))


@dataclass(frozen=True, slots=True)
class ReducerSummaryState:
    content: str
    covered_unit_ids: tuple[str, ...]
    covered_source_message_ids: tuple[str, ...]
    source_fingerprint: str
    compression_mode: str
    summary_method: str
    state_schema_version: int = 1


@dataclass(frozen=True, slots=True)
class ContextReductionPolicy:
    tool_compaction_ratio: float = 0.65
    summary_ratio: float = 0.80
    aggressive_ratio: float = 0.90
    normal_tail_message_target: int = 8
    aggressive_tail_message_target: int = 4

    def stage_for_ratio(self, ratio: float) -> str:
        if ratio < self.tool_compaction_ratio:
            return "full"
        if ratio < self.summary_ratio:
            return "tool_compaction"
        if ratio < self.aggressive_ratio:
            return "normal_summary"
        return "aggressive"


def build_history_units(messages: Iterable[dict[str, Any]]) -> list[HistoryUnit]:
    units: list[HistoryUnit] = []
    current_messages: list[dict[str, Any]] = []
    current_ids: list[str] = []

    def flush() -> None:
        if not current_messages:
            return
        units.append(
            HistoryUnit(
                unit_id=current_ids[0],
                messages=list(current_messages),
                source_message_ids=tuple(current_ids),
            )
        )
        current_messages.clear()
        current_ids.clear()

    for source in messages:
        role = str(source.get("role") or "")
        if role == "user" and current_messages:
            flush()
        source_id = str(source.get("source_message_id") or "")
        prompt_message = {key: value for key, value in source.items() if key != "source_message_id"}
        current_messages.append(prompt_message)
        if source_id:
            current_ids.append(source_id)
    flush()
    return units


def flatten_history_units(units: Iterable[HistoryUnit]) -> list[dict[str, Any]]:
    return [message for unit in units for message in unit.messages]


def history_units_fingerprint(units: Iterable[HistoryUnit]) -> str:
    payload = [
        {
            "unit_id": unit.unit_id,
            "source_message_ids": list(unit.source_message_ids),
            "messages": [_stable_message_payload(message) for message in unit.messages],
        }
        for unit in units
    ]
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def is_ordered_coverage_prefix(cached_ids: tuple[str, ...], target_ids: tuple[str, ...]) -> bool:
    return len(cached_ids) <= len(target_ids) and target_ids[: len(cached_ids)] == cached_ids


def _stable_message_payload(message: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "role": str(message.get("role") or ""),
        "content": message.get("content") or "",
    }
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list):
        payload["tool_calls"] = [_stable_tool_call_payload(item) for item in tool_calls if isinstance(item, dict)]
    if payload["role"] == "tool":
        payload["tool_call_id"] = str(message.get("tool_call_id") or "")
        if message.get("name") is not None:
            payload["name"] = str(message.get("name") or "")
    return payload


def _stable_tool_call_payload(tool_call: dict[str, Any]) -> dict[str, Any]:
    function = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else {}
    return {
        "id": str(tool_call.get("id") or ""),
        "type": str(tool_call.get("type") or ""),
        "function": {
            "name": str(function.get("name") or ""),
            "arguments": function.get("arguments") or "",
        },
    }


def select_tail_units(
    units: Iterable[HistoryUnit],
    *,
    target_message_count: int,
) -> tuple[list[HistoryUnit], list[HistoryUnit]]:
    ordered = list(units)
    if target_message_count <= 0:
        return ordered, []
    kept_start = len(ordered)
    covered = 0
    while kept_start > 0 and covered < target_message_count:
        kept_start -= 1
        covered += ordered[kept_start].message_count
    return ordered[:kept_start], ordered[kept_start:]


def select_head_and_tail_units(
    units: Iterable[HistoryUnit],
    *,
    target_message_count: int,
) -> tuple[list[HistoryUnit], list[HistoryUnit]]:
    ordered = list(units)
    if not ordered:
        return [], []
    head = ordered[0]
    remaining_target = max(0, target_message_count - head.message_count)
    summarized, tail = select_tail_units(ordered[1:], target_message_count=remaining_target)
    return summarized, [head, *tail]
