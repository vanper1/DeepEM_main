from __future__ import annotations

import json
from pathlib import Path
from typing import Any


VALID_SUITES = {"long_conversation", "document_qa", "context_pressure", "chat_mode_acceptance"}
VALID_EXECUTION_MODES = {"scripted_prefix", "sequential_agent"}
VALID_DOCUMENT_KEYS = {"pred", "api_docs"}
VALID_CHAT_MODES = {"general", "workspace"}
VALID_LLM_OPTIONS_PROFILES = {"heavy_probe", "server_default"}


def load_jsonl_cases(path: Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_no, raw in enumerate(handle, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSONL at {path}:{line_no}: {exc}") from exc
            if not isinstance(payload, dict):
                raise ValueError(f"Case at {path}:{line_no} must be an object")
            cases.append(payload)
    validate_cases(cases, source=str(path))
    return cases


def validate_cases(cases: list[dict[str, Any]], *, source: str = "dataset") -> None:
    seen: set[str] = set()
    for index, case in enumerate(cases, start=1):
        case_id = _required_text(case, "id", source, index)
        if case_id in seen:
            raise ValueError(f"Duplicate case id {case_id!r} in {source}")
        seen.add(case_id)
        suite = _required_text(case, "suite", source, index)
        if suite not in VALID_SUITES:
            raise ValueError(f"Invalid suite {suite!r} for {case_id}")
        schema_version = _schema_version(case, case_id)
        mode = _required_text(case, "execution_mode", source, index)
        if mode not in VALID_EXECUTION_MODES:
            raise ValueError(f"Invalid execution_mode {mode!r} for {case_id}")
        documents = case.get("documents") or []
        if not isinstance(documents, list) or any(item not in VALID_DOCUMENT_KEYS for item in documents):
            raise ValueError(f"Invalid documents for {case_id}: {documents!r}")
        prior_messages = case.get("prior_messages") or []
        if not isinstance(prior_messages, list):
            raise ValueError(f"prior_messages must be a list for {case_id}")
        if suite == "chat_mode_acceptance" and prior_messages:
            raise ValueError(f"prior_messages are not allowed for chat_mode_acceptance case {case_id}")
        for message in prior_messages:
            if not isinstance(message, dict) or not str(message.get("content") or "").strip():
                raise ValueError(f"Invalid prior message for {case_id}")
            if str(message.get("role") or "") not in {"operator", "user", "assistant", "system"}:
                raise ValueError(f"Invalid prior message role for {case_id}")
        turns = case.get("turns") or []
        if not isinstance(turns, list) or not turns:
            raise ValueError(f"Case {case_id} must contain turns")
        scored_turns = 0
        turn_ids: set[str] = set()
        for turn in turns:
            if not isinstance(turn, dict):
                raise ValueError(f"Invalid turn for {case_id}")
            turn_id = str(turn.get("turn_id") or "").strip()
            if not turn_id or turn_id in turn_ids:
                raise ValueError(f"Missing or duplicate turn_id for {case_id}")
            turn_ids.add(turn_id)
            if not str(turn.get("user_message") or "").strip():
                raise ValueError(f"Turn {case_id}/{turn_id} has no user_message")
            if suite == "chat_mode_acceptance":
                _validate_chat_mode_turn(case_id, turn_id, turn)
            if turn.get("score"):
                scored_turns += 1
                _validate_gold(case_id, turn_id, turn.get("gold"), strict_groups=schema_version >= 2)
                if suite == "chat_mode_acceptance":
                    _validate_chat_mode_gold(case_id, turn_id, turn)
        if scored_turns == 0:
            raise ValueError(f"Case {case_id} has no scored turn")
        if mode == "scripted_prefix" and len(turns) != 1:
            raise ValueError(f"scripted_prefix case {case_id} must have exactly one turn")
        _validate_pressure_target(
            case_id,
            suite,
            case.get("pressure_target"),
            turn_ids,
            require_measurement_turn=suite == "context_pressure" and schema_version >= 2,
        )
        _validate_seed_tool_results(case_id, case.get("seed_tool_results") or [])


def _validate_gold(case_id: str, turn_id: str, value: Any, *, strict_groups: bool = False) -> None:
    if not isinstance(value, dict):
        raise ValueError(f"Scored turn {case_id}/{turn_id} must contain gold")
    for name in (
        "must_include_any",
        "must_include_all",
        "forbidden_terms",
        "expected_tools",
        "forbidden_tools",
        "evidence",
        "required_anchor_fields",
    ):
        if not isinstance(value.get(name), list):
            raise ValueError(f"gold.{name} must be a list for {case_id}/{turn_id}")
    for group in value.get("must_include_any") or []:
        if not isinstance(group, list) or not group:
            raise ValueError(f"Invalid must_include_any group for {case_id}/{turn_id}")
        valid_items = all(isinstance(item, str) and item.strip() for item in group) if strict_groups else all(
            str(item).strip() for item in group
        )
        if not valid_items:
            raise ValueError(f"Invalid must_include_any group for {case_id}/{turn_id}")
    if not isinstance(value.get("answerable"), bool):
        raise ValueError(f"gold.answerable must be boolean for {case_id}/{turn_id}")


def _validate_chat_mode_turn(case_id: str, turn_id: str, turn: dict[str, Any]) -> None:
    mode = str(turn.get("chat_mode") or "").strip()
    if mode not in VALID_CHAT_MODES:
        raise ValueError(f"Invalid chat_mode for {case_id}/{turn_id}: {mode!r}")
    profile = str(turn.get("llm_options_profile") or "").strip()
    if profile not in VALID_LLM_OPTIONS_PROFILES:
        raise ValueError(f"Invalid llm_options_profile for {case_id}/{turn_id}: {profile!r}")


def _validate_chat_mode_gold(case_id: str, turn_id: str, turn: dict[str, Any]) -> None:
    gold = turn.get("gold")
    if not isinstance(gold, dict):
        raise ValueError(f"Scored turn {case_id}/{turn_id} must contain gold")
    max_tool_calls = gold.get("max_tool_calls")
    if not isinstance(max_tool_calls, int) or max_tool_calls < 0:
        raise ValueError(f"gold.max_tool_calls must be a non-negative integer for {case_id}/{turn_id}")
    policy = gold.get("expected_policy")
    if not isinstance(policy, dict):
        raise ValueError(f"gold.expected_policy must be an object for {case_id}/{turn_id}")
    mode = str(turn.get("chat_mode"))
    if policy.get("chat_mode") != mode:
        raise ValueError(f"gold.expected_policy.chat_mode must match turn.chat_mode for {case_id}/{turn_id}")
    if not isinstance(policy.get("workspace_injected"), bool):
        raise ValueError(f"gold.expected_policy.workspace_injected must be boolean for {case_id}/{turn_id}")
    exact_count = policy.get("available_tool_count")
    minimum_count = policy.get("min_available_tool_count")
    exact_valid = isinstance(exact_count, int) and exact_count >= 0
    minimum_valid = isinstance(minimum_count, int) and minimum_count >= 0
    if exact_valid == minimum_valid:
        raise ValueError(
            f"gold.expected_policy must define exactly one tool-count constraint for {case_id}/{turn_id}"
        )
    if not isinstance(policy.get("llm_options_equal"), dict):
        raise ValueError(f"gold.expected_policy.llm_options_equal must be an object for {case_id}/{turn_id}")
    absent = policy.get("llm_options_absent")
    if not isinstance(absent, list) or any(not isinstance(item, str) or not item for item in absent):
        raise ValueError(f"gold.expected_policy.llm_options_absent must be a string list for {case_id}/{turn_id}")


def _validate_seed_tool_results(case_id: str, items: Any) -> None:
    if not isinstance(items, list):
        raise ValueError(f"seed_tool_results must be a list for {case_id}")
    ids: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError(f"Invalid seed tool result for {case_id}")
        item_id = str(item.get("id") or "").strip()
        if not item_id or item_id in ids:
            raise ValueError(f"Missing or duplicate seed tool id for {case_id}")
        ids.add(item_id)
        if not str(item.get("tool_name") or "").strip() or not isinstance(item.get("result"), dict):
            raise ValueError(f"Invalid seed tool result {item_id} for {case_id}")


def _validate_pressure_target(
    case_id: str,
    suite: str,
    value: Any,
    turn_ids: set[str],
    *,
    require_measurement_turn: bool,
) -> None:
    if suite != "context_pressure":
        if value is not None:
            raise ValueError(f"pressure_target is only valid for context_pressure case {case_id}")
        return
    if not isinstance(value, dict):
        raise ValueError(f"context_pressure case {case_id} must contain pressure_target")
    lower = value.get("min_usage_ratio")
    upper = value.get("max_usage_ratio")
    if not isinstance(lower, (int, float)) or not isinstance(upper, (int, float)):
        raise ValueError(f"pressure_target must contain numeric min_usage_ratio and max_usage_ratio for {case_id}")
    if not 0 < float(lower) <= float(upper) <= 1:
        raise ValueError(f"pressure_target lower bound must be within (0, 1] for {case_id}")
    measurement_turn_id = value.get("measurement_turn_id")
    if measurement_turn_id is None:
        if require_measurement_turn:
            raise ValueError(f"pressure_target.measurement_turn_id is required for {case_id}")
        return
    if not isinstance(measurement_turn_id, str) or not measurement_turn_id.strip():
        raise ValueError(f"pressure_target.measurement_turn_id must be a non-empty string for {case_id}")
    if measurement_turn_id not in turn_ids:
        raise ValueError(f"pressure_target.measurement_turn_id {measurement_turn_id!r} is not a turn for {case_id}")


def _schema_version(case: dict[str, Any], case_id: str) -> int:
    value = case.get("schema_version", 1)
    if not isinstance(value, int) or value < 1:
        raise ValueError(f"schema_version must be a positive integer for {case_id}")
    return value


def _required_text(case: dict[str, Any], name: str, source: str, index: int) -> str:
    value = str(case.get(name) or "").strip()
    if not value:
        raise ValueError(f"Missing {name} at {source}:{index}")
    return value
