from __future__ import annotations

import json
import re
from typing import Any

from main.agent.llm import LLMClient


CAUTION_MARKERS = ("未提供", "没有明确", "无法确认", "无法从", "not specified", "not provided", "unknown")
PSEUDO_TOOL_RE = re.compile(r"<\s*(?:tool_call|function\s*=|parameter\s*=)", re.IGNORECASE)


def score_checkpoint(checkpoint: dict[str, Any], *, judge: dict[str, Any] | None = None) -> dict[str, Any]:
    gold = dict(checkpoint.get("gold") or {})
    answer = str(checkpoint.get("answer") or "")
    answer_folded = answer.casefold()
    all_requirements = [str(item) for item in gold.get("must_include_all") or []]
    any_requirements = [[str(item) for item in group] for group in gold.get("must_include_any") or []]
    matched_all = [item for item in all_requirements if item.casefold() in answer_folded]
    matched_any = [group for group in any_requirements if any(item.casefold() in answer_folded for item in group)]
    required_total = len(all_requirements) + len(any_requirements)
    required_matched = len(matched_all) + len(matched_any)
    required_point_recall = required_matched / required_total if required_total else 1.0

    forbidden_hits = [
        str(item) for item in gold.get("forbidden_terms") or [] if str(item).casefold() in answer_folded
    ]
    formal_tools = [str(item.get("tool_name") or "") for item in checkpoint.get("tool_calls") or []]
    expected_tools = [str(item) for item in gold.get("expected_tools") or []]
    missing_expected_tools = [item for item in expected_tools if item not in formal_tools]
    forbidden_tool_hits = [str(item) for item in gold.get("forbidden_tools") or [] if str(item) in formal_tools]
    max_tool_calls = gold.get("max_tool_calls")
    tool_call_limit_exceeded = isinstance(max_tool_calls, int) and len(formal_tools) > max_tool_calls
    expected_tool_recall = (
        (len(expected_tools) - len(missing_expected_tools)) / len(expected_tools) if expected_tools else 1.0
    )
    pseudo_tool_call_recovered = bool(checkpoint.get("pseudo_tool_call_recovered"))
    visible_pseudo_tool_call = bool(PSEUDO_TOOL_RE.search(answer))
    pseudo_tool_call = bool(checkpoint.get("pseudo_tool_call")) or visible_pseudo_tool_call
    pseudo_tool_call_detected = bool(
        checkpoint.get("pseudo_tool_call_detected", pseudo_tool_call or visible_pseudo_tool_call)
    )
    answerable = bool(gold.get("answerable", True))
    abstention_ok = answerable or any(marker in answer_folded for marker in CAUTION_MARKERS)

    required_anchor_fields = [str(item) for item in gold.get("required_anchor_fields") or []]
    available_anchor_fields = set(str(item) for item in checkpoint.get("anchor_fields_present") or [])
    missing_anchor_fields = [item for item in required_anchor_fields if item not in available_anchor_fields]
    tool_anchor_recall = (
        (len(required_anchor_fields) - len(missing_anchor_fields)) / len(required_anchor_fields)
        if required_anchor_fields
        else 1.0
    )

    hard_failures: list[str] = []
    if forbidden_hits:
        hard_failures.append("forbidden_claim")
    if missing_expected_tools:
        hard_failures.append("missing_expected_tool")
    if forbidden_tool_hits:
        hard_failures.append("forbidden_tool")
    if tool_call_limit_exceeded:
        hard_failures.append("tool_call_limit_exceeded")
    if pseudo_tool_call:
        hard_failures.append("pseudo_tool_call")
    if not abstention_ok:
        hard_failures.append("unsupported_answer_to_unanswerable_question")
    if checkpoint.get("connection_error"):
        hard_failures.append("connection_error")
    if checkpoint.get("empty_assistant_response"):
        hard_failures.append("empty_assistant_response")
    if checkpoint.get("error"):
        hard_failures.append("runtime_error")
    if checkpoint.get("run_stop_reason") == "step_budget_exhausted":
        hard_failures.append("step_budget_exhausted")

    judge_score = calculate_judge_score(judge) if judge else None
    judge_pass = True if judge_score is None else judge_score >= 75 and int(judge.get("groundedness") or 0) >= 3
    passed = not hard_failures and required_point_recall >= 0.8 and judge_pass
    return {
        "passed": passed,
        "required_point_recall": round(required_point_recall, 6),
        "matched_all": matched_all,
        "matched_any_group_count": len(matched_any),
        "forbidden_hits": forbidden_hits,
        "formal_tools": formal_tools,
        "expected_tool_recall": round(expected_tool_recall, 6),
        "missing_expected_tools": missing_expected_tools,
        "forbidden_tool_hits": forbidden_tool_hits,
        "max_tool_calls": max_tool_calls,
        "tool_call_limit_exceeded": tool_call_limit_exceeded,
        "pseudo_tool_call": pseudo_tool_call,
        "pseudo_tool_call_detected": pseudo_tool_call_detected,
        "pseudo_tool_call_recovered": pseudo_tool_call_recovered,
        "abstention_ok": abstention_ok,
        "tool_anchor_recall": round(tool_anchor_recall, 6),
        "missing_anchor_fields": missing_anchor_fields,
        "judge_score_100": judge_score,
        "hard_failures": hard_failures,
    }


def evaluate_chat_mode_checkpoint(checkpoint: dict[str, Any], *, mode_protocol: str) -> dict[str, Any]:
    if mode_protocol not in {"legacy", "explicit"}:
        raise ValueError(f"Unsupported mode_protocol: {mode_protocol!r}")
    measurement_complete = bool(checkpoint.get("measurement_complete"))
    measurement_score = {
        "passed": measurement_complete,
        "hard_failures": [] if measurement_complete else ["measurement_incomplete"],
    }
    target_behavior_score = score_checkpoint(checkpoint)
    if mode_protocol == "legacy":
        policy_score = {"status": "not_applicable", "passed": None, "hard_failures": []}
    else:
        policy_score = _score_execution_policy(checkpoint)
    return {
        "measurement_score": measurement_score,
        "target_behavior_score": target_behavior_score,
        "policy_score": policy_score,
    }


def _score_execution_policy(checkpoint: dict[str, Any]) -> dict[str, Any]:
    observed = checkpoint.get("observed_execution_policy")
    if not isinstance(observed, dict):
        return {"status": "failed", "passed": False, "hard_failures": ["execution_policy_missing"]}
    gold = checkpoint.get("gold") if isinstance(checkpoint.get("gold"), dict) else {}
    expected = checkpoint.get("expected_policy")
    if not isinstance(expected, dict):
        expected = gold.get("expected_policy") if isinstance(gold.get("expected_policy"), dict) else {}
    failures: list[str] = []
    if observed.get("chat_mode") != expected.get("chat_mode"):
        failures.append("chat_mode_mismatch")
    if observed.get("workspace_injected") is not expected.get("workspace_injected"):
        failures.append("workspace_injection_mismatch")
    projection_fields = (
        "workspace_snapshot_injected",
        "tool_transcript_included",
        "retrieval_context_included",
        "history_message_count",
    )
    for field in projection_fields:
        if field in expected and observed.get(field) != expected.get(field):
            failures.append(f"{field}_mismatch")
    tool_count = observed.get("tool_count")
    exact_count = expected.get("available_tool_count")
    minimum_count = expected.get("min_available_tool_count")
    if isinstance(exact_count, int) and tool_count != exact_count:
        failures.append("available_tool_count_mismatch")
    elif isinstance(minimum_count, int) and (not isinstance(tool_count, int) or tool_count < minimum_count):
        failures.append("available_tool_count_mismatch")
    options = observed.get("effective_llm_options")
    options = options if isinstance(options, dict) else {}
    expected_options = expected.get("llm_options_equal")
    expected_options = expected_options if isinstance(expected_options, dict) else {}
    if any(options.get(key) != value for key, value in expected_options.items()):
        failures.append("effective_llm_option_mismatch")
    absent_options = expected.get("llm_options_absent") or []
    if any(key in options for key in absent_options):
        failures.append("forbidden_llm_option_present")
    return {"status": "passed" if not failures else "failed", "passed": not failures, "hard_failures": failures}


def calculate_judge_score(judge: dict[str, Any] | None) -> float | None:
    if not judge:
        return None
    try:
        correctness = _bounded_score(judge.get("correctness"))
        groundedness = _bounded_score(judge.get("groundedness"))
        completeness = _bounded_score(judge.get("completeness"))
        continuity = _bounded_score(judge.get("context_continuity"))
    except (TypeError, ValueError):
        return None
    score = (correctness * 0.35 + groundedness * 0.30 + completeness * 0.20 + continuity * 0.15) / 4 * 100
    return round(score, 3)


def judge_checkpoint(
    client: LLMClient,
    *,
    question: str,
    gold: dict[str, Any],
    answer: str,
    tool_calls: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, str | None]:
    system = (
        "You are an independent evaluator. Score only the supplied answer against the gold requirements and evidence. "
        "Do not infer which context strategy produced the answer. Return only JSON with integer scores 0-4 for "
        "correctness, completeness, groundedness, context_continuity; arrays unsupported_claims and missing_points; "
        "boolean irrelevant_context_pollution; and a short score_reason."
    )
    user_payload = {
        "question": question,
        "gold": gold,
        "answer": answer,
        "formal_tool_calls": tool_calls,
        "retrieved_evidence": evidence,
    }
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
    ]
    last_error: str | None = None
    for attempt in range(2):
        try:
            response = client.complete(
                messages=messages,
                tools=[],
                temperature=0.0,
                generation_options={"enable_thinking": False, "preserve_thinking": False},
            )
            payload = parse_judge_json(response.content)
            validate_judge_payload(payload)
            return payload, None
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt == 0:
                messages.append({"role": "user", "content": "The previous output was invalid. Return only the required JSON object."})
    return None, last_error


def parse_judge_json(text: str) -> dict[str, Any]:
    stripped = str(text or "").strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped, flags=re.IGNORECASE)
        stripped = re.sub(r"\s*```$", "", stripped)
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end < start:
        raise ValueError("judge response does not contain JSON")
    payload = json.loads(stripped[start : end + 1])
    if not isinstance(payload, dict):
        raise ValueError("judge response must be an object")
    return payload


def validate_judge_payload(payload: dict[str, Any]) -> None:
    for name in ("correctness", "completeness", "groundedness", "context_continuity"):
        _bounded_score(payload.get(name))
    for name in ("unsupported_claims", "missing_points"):
        if not isinstance(payload.get(name), list):
            raise ValueError(f"judge field {name} must be a list")
    if not isinstance(payload.get("irrelevant_context_pollution"), bool):
        raise ValueError("judge field irrelevant_context_pollution must be boolean")
    if not isinstance(payload.get("score_reason"), str):
        raise ValueError("judge field score_reason must be string")


def _bounded_score(value: Any) -> int:
    score = int(value)
    if not 0 <= score <= 4:
        raise ValueError("judge scores must be between 0 and 4")
    return score
