from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from eval.context_management.http_frontend_eval_client import FrontendEvalClient
from eval.context_management.run_runtime_eval import (
    DEFAULT_DOCUMENT_DATASET,
    DEFAULT_LONG_DATASET,
    DEFAULT_PRESSURE_DATASET,
    configure_tokenizer_network,
    load_project_env,
    select_cases,
)
from eval.context_management.runtime_eval_core import (
    DEFAULT_API_DOCS_PATH,
    DEFAULT_PRED_PATH,
    PSEUDO_TOOL_RE,
    _aggregate_turn_metrics,
    _anchor_fields,
    _prompt_step_metrics,
    _resolve_document_source,
    evaluate_pressure_target,
)
from eval.context_management.runtime_eval_scoring import evaluate_chat_mode_checkpoint, score_checkpoint
from eval.context_management.runtime_eval_schema import load_jsonl_cases
from eval.context_management.score_runtime_eval import load_results, write_reports


DEFAULT_OUTPUT_DIR = Path("eval/context_management/results/http_runtime_v1")
DEFAULT_CHAT_MODE_DATASET = Path("eval/context_management/chat_mode_acceptance_v1.jsonl")
HEAVY_PROBE_LLM_OPTIONS = {
    "temperature": 1.0,
    "top_p": 0.95,
    "top_k": 20,
    "min_p": 0.0,
    "presence_penalty": 1.5,
    "repetition_penalty": 1.0,
    "enable_thinking": True,
    "preserve_thinking": False,
    "reasoning_effort": "medium",
    "thinking_token_budget": 16384,
}
DEFAULT_LLM_OPTIONS = HEAVY_PROBE_LLM_OPTIONS
DOCUMENT_NAMES = {"pred": "PReD.pdf", "api_docs": "API_DOCS.md"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run context evaluation through DeepEM's frontend HTTP/SSE path.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--suite",
        choices=["all", "long_conversation", "document_qa", "context_pressure", "chat_mode_acceptance"],
        default="all",
    )
    parser.add_argument("--case-id", default=None, help="Comma-separated case ids")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--save-prompts", action="store_true")
    parser.add_argument("--pred-path", type=Path, default=None)
    parser.add_argument("--api-docs-path", type=Path, default=None)
    parser.add_argument(
        "--pressure-dataset",
        type=Path,
        default=DEFAULT_PRESSURE_DATASET,
        help="Path to the context-pressure JSONL dataset",
    )
    parser.add_argument("--request-timeout", type=float, default=900.0)
    parser.add_argument("--debug-log-dir", type=Path, default=None)
    parser.add_argument("--chat-mode-dataset", type=Path, default=DEFAULT_CHAT_MODE_DATASET)
    parser.add_argument("--mode-protocol", choices=["auto", "legacy", "explicit"], default="auto")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--tokenizer-offline", action="store_true")
    return parser


def resolve_mode_protocol(requested: str, *, chat_mode_supported: bool) -> str:
    if requested == "auto":
        return "explicit" if chat_mode_supported else "legacy"
    if requested == "explicit" and not chat_mode_supported:
        raise ValueError("Server OpenAPI does not expose chat_mode")
    if requested not in {"legacy", "explicit"}:
        raise ValueError(f"Unsupported mode protocol: {requested!r}")
    return requested


def turn_llm_options(turn: dict[str, Any]) -> dict[str, Any] | None:
    profile = str(turn.get("llm_options_profile") or "server_default")
    if profile == "server_default":
        return None
    if profile == "heavy_probe":
        return dict(HEAVY_PROBE_LLM_OPTIONS)
    raise ValueError(f"Unsupported llm_options_profile: {profile!r}")


def expected_policy_for_turn(turn: dict[str, Any]) -> dict[str, Any]:
    gold = turn.get("gold") if isinstance(turn.get("gold"), dict) else {}
    expected = gold.get("expected_policy") if isinstance(gold.get("expected_policy"), dict) else None
    if expected is not None:
        return deepcopy(expected)
    mode = str(turn.get("chat_mode") or "")
    if mode == "general":
        return {
            "chat_mode": "general",
            "workspace_injected": False,
            "available_tool_count": 0,
            "llm_options_equal": {
                "enable_thinking": False,
                "preserve_thinking": False,
                "temperature": 0.3,
                "top_p": 0.8,
                "presence_penalty": 0.0,
            },
            "llm_options_absent": ["thinking_token_budget", "reasoning_effort"],
        }
    if mode == "workspace":
        return {
            "chat_mode": "workspace",
            "workspace_injected": True,
            "min_available_tool_count": 1,
            "llm_options_equal": {"enable_thinking": True, "thinking_token_budget": 4096},
            "llm_options_absent": [],
        }
    raise ValueError(f"Unsupported chat_mode: {mode!r}")


def iter_case_repeats(cases: list[dict[str, Any]], *, repeat: int):
    if repeat <= 0:
        raise ValueError("--repeat must be greater than zero")
    for case in cases:
        for repeat_index in range(repeat):
            yield case, repeat_index, f"{case['id']}:semantic:{repeat_index}"


def select_http_cases(args: argparse.Namespace, case_ids: set[str] | None) -> list[dict[str, Any]]:
    if args.suite == "chat_mode_acceptance":
        cases = load_jsonl_cases(args.chat_mode_dataset)
        if case_ids:
            known = {str(case["id"]) for case in cases}
            missing = sorted(case_ids - known)
            if missing:
                raise ValueError(f"Unknown case ids: {', '.join(missing)}")
            cases = [case for case in cases if str(case["id"]) in case_ids]
        return cases
    return select_cases(suite=args.suite, case_ids=case_ids, pressure_dataset=args.pressure_dataset)


def main() -> int:
    args = build_parser().parse_args()
    load_project_env()
    configure_tokenizer_network(offline=args.tokenizer_offline)
    if args.request_timeout <= 0:
        raise ValueError("--request-timeout must be greater than zero")
    if args.repeat <= 0:
        raise ValueError("--repeat must be greater than zero")
    case_ids = {item.strip().lower() for item in (args.case_id or "").split(",") if item.strip()} or None
    cases = select_http_cases(args, case_ids)
    documents = _document_sources(args.pred_path, args.api_docs_path)
    debug_dir = (args.debug_log_dir or Path(os.getenv("DEEPEM_DEBUG_LOG_DIR") or "deepem_debug_logs")).resolve()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_path = output_dir / "raw_results.jsonl"
    if raw_path.exists() and not args.resume:
        raw_path.write_text("", encoding="utf-8")
    results = load_results(raw_path) if args.resume else []
    completed = {str(item.get("experiment_key")) for item in results}
    experiment_id = (
        str(results[0].get("experiment_id") or "")
        if results
        else datetime.now(timezone.utc).strftime("http-runtime-v1-%Y%m%dT%H%M%SZ")
    )
    if not experiment_id:
        experiment_id = datetime.now(timezone.utc).strftime("http-runtime-v1-%Y%m%dT%H%M%SZ")
    client = FrontendEvalClient(args.base_url, timeout_seconds=args.request_timeout)
    chat_mode_dataset_hash = _sha256(args.chat_mode_dataset) if args.suite == "chat_mode_acceptance" else None
    chat_mode_supported = client.detect_chat_mode_capability() if args.suite == "chat_mode_acceptance" else False
    mode_protocol = (
        resolve_mode_protocol(args.mode_protocol, chat_mode_supported=chat_mode_supported)
        if args.suite == "chat_mode_acceptance"
        else "legacy"
    )
    manifest = _manifest(
        experiment_id,
        args,
        cases,
        documents,
        debug_dir,
        mode_protocol=mode_protocol,
        chat_mode_supported=chat_mode_supported,
    )
    (output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    with raw_path.open("a", encoding="utf-8") as handle:
        for case, repeat_index, key in iter_case_repeats(cases, repeat=args.repeat):
            if key in completed:
                print(f"skip {key}")
                continue
            print(f"run {key}", flush=True)
            try:
                result = run_http_case(
                    client,
                    case=case,
                    experiment_id=experiment_id,
                    documents=documents,
                    debug_dir=debug_dir,
                    save_prompts=args.save_prompts,
                    mode_protocol=mode_protocol,
                    repeat_index=repeat_index,
                )
            except Exception as exc:
                result = {
                    "experiment_key": key,
                    "case_id": case["id"],
                    "suite": case["suite"],
                    "category": case.get("category"),
                    "execution_mode": "sequential_http",
                    "strategy": "semantic",
                    "repeat_index": repeat_index,
                    "passed": False,
                    "checkpoints": [],
                    "fatal_error": f"{type(exc).__name__}: {exc}",
                }
            result.update(
                {
                    "experiment_id": experiment_id,
                    "dataset_version": (
                        args.chat_mode_dataset.stem
                        if args.suite == "chat_mode_acceptance"
                        else "runtime_context_v1"
                    ),
                    "dataset_hash": chat_mode_dataset_hash,
                    "judge_model": None,
                }
            )
            handle.write(json.dumps(result, ensure_ascii=False) + "\n")
            handle.flush()
            results.append(result)
            completed.add(key)

    summary = write_reports(results, output_dir)
    print(f"results={len(results)} checkpoints={summary['checkpoint_count']} failed={summary['failed_checkpoint_count']} output={output_dir}")
    return 0


def run_http_case(
    client: FrontendEvalClient,
    *,
    case: dict[str, Any],
    experiment_id: str,
    documents: dict[str, Path],
    debug_dir: Path,
    save_prompts: bool = False,
    mode_protocol: str = "legacy",
    repeat_index: int = 0,
) -> dict[str, Any]:
    case_started = time.perf_counter()
    started_at = datetime.now(timezone.utc)
    session = client.create_session(title=f"[eval {experiment_id}] {case['id']}")
    session_id = str(session["id"])
    named_files = [(DOCUMENT_NAMES[key], documents[key]) for key in case.get("documents") or []]
    uploaded = client.upload_files(session_id=session_id, files=named_files) if named_files else []
    attachment_ids = [str(item.get("asset_id")) for item in uploaded if item.get("asset_id")]
    all_turns: list[dict[str, Any]] = []
    checkpoints: list[dict[str, Any]] = []

    prefix_turns = [
        {
            "turn_id": f"prefix_{index}",
            "user_message": str(message.get("content") or ""),
            "score": False,
            "gold": {},
            "source_role": str(message.get("role") or ""),
        }
        for index, message in enumerate(case.get("prior_messages") or [], start=1)
        if str(message.get("role") or "") in {"operator", "user"}
    ]
    executable_turns = [*prefix_turns, *(case.get("turns") or [])]
    is_chat_mode_suite = str(case.get("suite") or "") == "chat_mode_acceptance"
    for turn_index, turn in enumerate(executable_turns):
        requested_chat_mode = str(turn.get("chat_mode") or "workspace")
        llm_options = turn_llm_options(turn) if is_chat_mode_suite else dict(DEFAULT_LLM_OPTIONS)
        turn_result = client.execute_turn(
            session_id=session_id,
            content=str(turn.get("user_message") or ""),
            attachment_ids=attachment_ids if turn_index == 0 else [],
            llm_options=llm_options,
            chat_mode=requested_chat_mode,
            send_chat_mode=is_chat_mode_suite and mode_protocol == "explicit",
        )
        history = client.get_history(session_id)
        checkpoint = _build_checkpoint(
            case=case,
            turn=turn,
            session_id=session_id,
            turn_result=turn_result,
            history_items=list(history.get("items") or []),
            debug_dir=debug_dir,
            save_prompts=save_prompts,
            mode_protocol=mode_protocol if is_chat_mode_suite else None,
            requested_chat_mode=requested_chat_mode if is_chat_mode_suite else None,
        )
        all_turns.append(checkpoint)
        if is_chat_mode_suite:
            checkpoint.update(evaluate_chat_mode_checkpoint(checkpoint, mode_protocol=mode_protocol))
            checkpoint["policy_evaluation_status"] = checkpoint["policy_score"]["status"]
        if turn.get("score"):
            if is_chat_mode_suite:
                checkpoint["rule_score"] = checkpoint["target_behavior_score"]
            else:
                checkpoint["rule_score"] = score_checkpoint(checkpoint)
            checkpoints.append(deepcopy(checkpoint))
        if turn_result.get("error"):
            break

    finished_at = datetime.now(timezone.utc)
    if is_chat_mode_suite and mode_protocol == "legacy":
        passed = bool(checkpoints) and all(bool(item.get("measurement_score", {}).get("passed")) for item in all_turns)
    elif is_chat_mode_suite:
        passed = (
            bool(checkpoints)
            and all(bool(item.get("measurement_score", {}).get("passed")) for item in all_turns)
            and all(bool(item.get("policy_score", {}).get("passed")) for item in all_turns)
            and all(bool(item.get("target_behavior_score", {}).get("passed")) for item in checkpoints)
        )
    else:
        passed = bool(checkpoints) and all(bool(item.get("rule_score", {}).get("passed")) for item in checkpoints)
    pressure_result = evaluate_pressure_target(case, all_turns)
    if pressure_result and pressure_result["measurement_available"] and not pressure_result["within_target_band"]:
        passed = False
    return {
        "experiment_key": f"{case['id']}:semantic:{repeat_index}",
        "case_id": str(case["id"]),
        "suite": str(case["suite"]),
        "category": str(case.get("category") or ""),
        "difficulty": str(case.get("difficulty") or ""),
        "execution_mode": "sequential_http",
        "source_execution_mode": str(case.get("execution_mode") or ""),
        "critical_repeat": bool(case.get("critical_repeat")),
        "strategy": "semantic",
        "repeat_index": repeat_index,
        "mode_protocol": mode_protocol if is_chat_mode_suite else None,
        "evaluation_role": ("baseline" if mode_protocol == "legacy" else "candidate") if is_chat_mode_suite else None,
        "session_id": session_id,
        "uploaded_assets": uploaded,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "latency_ms": round((time.perf_counter() - case_started) * 1000, 3),
        "case_end_to_end_ms": round((time.perf_counter() - case_started) * 1000, 3),
        "turn_count": len(all_turns),
        "prefix_turn_count": len(prefix_turns),
        "checkpoint_count": len(checkpoints),
        "all_turns": all_turns,
        "checkpoints": checkpoints,
        "passed": passed,
        "pressure_result": pressure_result,
    }


def _build_checkpoint(
    *,
    case: dict[str, Any],
    turn: dict[str, Any],
    session_id: str,
    turn_result: dict[str, Any],
    history_items: list[dict[str, Any]],
    debug_dir: Path,
    save_prompts: bool,
    mode_protocol: str | None = None,
    requested_chat_mode: str | None = None,
) -> dict[str, Any]:
    message_event = next((item for item in turn_result["events"] if item["name"] == "message"), None)
    operator_id = str((message_event or {}).get("data", {}).get("message_id") or "") or None
    assistant = _assistant_after(history_items, operator_id)
    run_id = str((assistant or {}).get("run_id") or turn_result.get("run_id") or "") or None
    answer = str((assistant or {}).get("content") or turn_result.get("answer") or "")
    tool_calls = _merge_tool_calls(
        _tool_calls_from_steps(list((assistant or {}).get("run_steps") or [])),
        _tool_calls_from_events(turn_result["events"]),
    )
    records = _read_debug_records(debug_dir, run_id)
    prompt_steps = []
    for record in records:
        if record.get("stage") != "llm_prompt":
            continue
        payload = dict(record.get("payload") or {})
        payload["assembled_messages"] = payload.get("messages") or []
        prompt_steps.append(_prompt_step_metrics(payload, save_prompts=save_prompts))
    summaries = [item.get("context_summary") for item in prompt_steps if item.get("context_summary")]
    metrics = _aggregate_turn_metrics(prompt_steps, tool_calls)
    semantic_latencies = [
        float((item.get("context_compression") or {}).get("semantic_latency_ms"))
        for item in prompt_steps
        if (item.get("context_compression") or {}).get("semantic_latency_ms") is not None
    ]
    metrics.update(
        {
            "semantic_latency_ms_total": round(sum(semantic_latencies), 3) if semantic_latencies else None,
            "semantic_latency_ms_max": round(max(semantic_latencies), 3) if semantic_latencies else None,
        }
    )
    stop_reason = _run_stop_reason(records)
    llm_steps = _llm_steps(records)
    visible_pseudo_tool_call = bool(PSEUDO_TOOL_RE.search(answer))
    transcript_pseudo_tool_call = any(
        PSEUDO_TOOL_RE.search(f"{record.get('payload', {}).get('reasoning') or ''}\n{record.get('payload', {}).get('content') or ''}")
        for record in records
        if record.get("stage") == "llm_output"
    )
    pseudo_tool_call_detected = visible_pseudo_tool_call or transcript_pseudo_tool_call
    empty_response_repair_succeeded = any(
        record.get("stage") == "empty_response_repair_succeeded" for record in records
    )
    pseudo_tool_call_recovered = (
        transcript_pseudo_tool_call and empty_response_repair_succeeded and not visible_pseudo_tool_call
    )
    pseudo_tool_call = visible_pseudo_tool_call or (
        transcript_pseudo_tool_call and not pseudo_tool_call_recovered
    )
    error = turn_result.get("error")
    estimated_input_tokens = _estimated_input_tokens(prompt_steps)
    measurement_complete = bool(
        error is None
        and answer.strip()
        and turn_result.get("done")
        and turn_result.get("time_to_first_visible_token_ms") is not None
        and turn_result.get("turn_end_to_end_ms") is not None
        and estimated_input_tokens is not None
    )
    return {
        "case_id": str(case["id"]),
        "turn_id": str(turn.get("turn_id") or ""),
        "session_id": session_id,
        "operator_message_id": operator_id,
        "assistant_message_id": (assistant or {}).get("id"),
        "run_id": run_id,
        "question": str(turn.get("user_message") or ""),
        "gold": deepcopy(turn.get("gold") or {}),
        "expected_policy": expected_policy_for_turn(turn) if mode_protocol is not None else None,
        "requested_chat_mode": requested_chat_mode,
        "mode_protocol": mode_protocol,
        "observed_execution_policy": turn_result.get("observed_execution_policy"),
        "measurement_complete": measurement_complete,
        "estimated_input_tokens": estimated_input_tokens,
        "answer": answer,
        "error": error,
        "connection_error": "connection error" in str(error or "").casefold(),
        "empty_assistant_response": not answer.strip() and error is None,
        "run_stop_reason": stop_reason,
        "events": turn_result["events"],
        "tool_calls": tool_calls,
        "prompt_steps": prompt_steps,
        "llm_steps": llm_steps,
        "context_summaries": summaries,
        "summary_merge_count": len(summaries),
        "anchor_fields_present": sorted(_anchor_fields(summaries)),
        "retrieved_evidence": _retrieved_evidence(tool_calls),
        "pseudo_tool_call": pseudo_tool_call,
        "pseudo_tool_call_detected": pseudo_tool_call_detected,
        "pseudo_tool_call_recovered": pseudo_tool_call_recovered,
        "metrics": metrics,
        "http_request_started_at": datetime.fromtimestamp(turn_result["http_request_started_at"], timezone.utc).isoformat(),
        "time_to_stream_open_ms": turn_result.get("time_to_stream_open_ms"),
        "time_to_first_activity_ms": turn_result.get("time_to_first_activity_ms"),
        "time_to_first_visible_token_ms": turn_result.get("time_to_first_visible_token_ms"),
        "time_to_final_answer_token_ms": turn_result.get("time_to_final_answer_token_ms"),
        "turn_end_to_end_ms": turn_result.get("turn_end_to_end_ms"),
        "latency_ms": turn_result.get("turn_end_to_end_ms"),
    }


def _estimated_input_tokens(prompt_steps: list[dict[str, Any]]) -> int | None:
    values = [
        int(total["estimated_tokens"])
        for item in prompt_steps
        for composition in [item.get("prompt_composition")]
        if isinstance(composition, dict)
        for total in [composition.get("total")]
        if isinstance(total, dict) and total.get("estimated_tokens") is not None
    ]
    return max(values) if values else None


def _assistant_after(items: list[dict[str, Any]], operator_id: str | None) -> dict[str, Any] | None:
    if not operator_id:
        return None
    for index, item in enumerate(items):
        if str(item.get("id") or "") != operator_id:
            continue
        return next((candidate for candidate in items[index + 1 :] if str(candidate.get("role")) == "assistant"), None)
    return None


def _tool_calls_from_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    calls: dict[str, dict[str, Any]] = {}
    for step in steps:
        call_id = str(step.get("tool_call_id") or "")
        if not call_id:
            continue
        if step.get("type") == "tool_call":
            calls[call_id] = {
                "tool_result_id": call_id,
                "tool_name": step.get("tool_name"),
                "arguments": step.get("arguments") or {},
                "status": step.get("status"),
            }
        elif step.get("type") == "tool_result":
            item = calls.setdefault(call_id, {"tool_result_id": call_id, "tool_name": step.get("tool_name")})
            data = step.get("data") if isinstance(step.get("data"), dict) else {}
            if not item.get("tool_name") and data.get("found") is not None and data.get("path"):
                # Detail results retain the source tool name in data.tool_name; it is not this invocation's name.
                item["tool_name"] = "retrieve_tool_result_detail"
            item.update({"result_status": step.get("status"), "found": data.get("found"), "path": data.get("path"), "data": data, "compression": step.get("compression")})
    return list(calls.values())


def _tool_calls_from_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for index, event in enumerate(events):
        if event.get("name") != "tool_call":
            continue
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        tool_name = str(data.get("tool_name") or "")
        if not tool_name:
            continue
        calls.append(
            {
                "tool_result_id": str(data.get("tool_call_id") or data.get("tool_result_id") or f"sse_tool_call_{index}"),
                "tool_name": tool_name,
                "arguments": data.get("arguments") if isinstance(data.get("arguments"), dict) else {},
                "status": data.get("status"),
            }
        )
    return calls


def _merge_tool_calls(step_calls: list[dict[str, Any]], event_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    step_signatures = {_tool_call_signature(item) for item in step_calls}
    return [*step_calls, *(item for item in event_calls if _tool_call_signature(item) not in step_signatures)]


def _tool_call_signature(tool_call: dict[str, Any]) -> str:
    arguments = tool_call.get("arguments") if isinstance(tool_call.get("arguments"), dict) else {}
    return f"{tool_call.get('tool_name') or ''}:{json.dumps(arguments, sort_keys=True, ensure_ascii=False, default=str)}"


def _retrieved_evidence(tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for call in tool_calls:
        data = call.get("data") if isinstance(call.get("data"), dict) else {}
        for result in data.get("results") or []:
            if isinstance(result, dict):
                evidence.append({key: result.get(key) for key in ("file_name", "page", "chunk_text", "score") if key in result})
    return evidence


def _read_debug_records(debug_dir: Path, run_id: str | None) -> list[dict[str, Any]]:
    if not run_id:
        return []
    path = debug_dir / f"{run_id}.jsonl"
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            records.append(item)
    return records


def _run_stop_reason(records: list[dict[str, Any]]) -> str | None:
    for record in reversed(records):
        if record.get("stage") not in {"run_completed", "run_failed", "run_aborted"}:
            continue
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        run = payload.get("run") if isinstance(payload.get("run"), dict) else {}
        return str(payload.get("stop_reason") or run.get("stop_reason") or payload.get("error") or payload.get("reason") or "") or None
    return None


def _llm_steps(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    for index, record in enumerate((item for item in records if item.get("stage") == "llm_output"), start=1):
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        content = str(payload.get("content") or "")
        reasoning = str(payload.get("reasoning") or "")
        steps.append(
            {
                "step_index": payload.get("step_index") or index,
                "latency_ms": None,
                "finish_reason": None,
                "content_chars": len(content),
                "reasoning_chars": len(reasoning),
                "raw_content_chars": None,
                "raw_reasoning_chars": None,
                "tool_call_count": len(payload.get("tool_calls") or []),
                "input_tokens": None,
                "output_tokens": None,
                "reasoning_tokens": None,
                "total_tokens": None,
            }
        )
    return steps


def _document_sources(pred_path: Path | None, api_docs_path: Path | None) -> dict[str, Path]:
    return {
        "pred": _resolve_document_source("PReD.pdf", pred_path or DEFAULT_PRED_PATH),
        "api_docs": _resolve_document_source("API_DOCS.md", api_docs_path or DEFAULT_API_DOCS_PATH),
    }


def _manifest(
    experiment_id: str,
    args: argparse.Namespace,
    cases: list[dict[str, Any]],
    documents: dict[str, Path],
    debug_dir: Path,
    *,
    mode_protocol: str = "legacy",
    chat_mode_supported: bool = False,
) -> dict[str, Any]:
    is_chat_mode_suite = args.suite == "chat_mode_acceptance"
    return {
        "experiment_id": experiment_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "command": " ".join(sys.argv),
        "base_url": args.base_url,
        "suite": args.suite,
        "mode_protocol": mode_protocol if is_chat_mode_suite else None,
        "chat_mode_supported": chat_mode_supported if is_chat_mode_suite else None,
        "evaluation_role": ("baseline" if mode_protocol == "legacy" else "candidate") if is_chat_mode_suite else None,
        "dataset_version": args.chat_mode_dataset.stem if is_chat_mode_suite else "runtime_context_v1",
        "case_ids": [case["id"] for case in cases],
        "context_mode": "semantic",
        "judge_enabled": False,
        "execution_path": "frontend_http_sse",
        "shared_test_state": True,
        "debug_log_dir": str(debug_dir),
        "document_source_paths": {key: str(path) for key, path in documents.items()},
        "document_source_hashes": {key: _sha256(path) for key, path in documents.items()},
        "dataset_hashes": {
            "long_conversation": _sha256(DEFAULT_LONG_DATASET),
            "document_qa": _sha256(DEFAULT_DOCUMENT_DATASET),
            "context_pressure": _sha256(getattr(args, "pressure_dataset", DEFAULT_PRESSURE_DATASET)),
            **(
                {"chat_mode_acceptance": _sha256(args.chat_mode_dataset)}
                if is_chat_mode_suite
                else {}
            ),
        },
        "git_commit": _git_value(["rev-parse", "HEAD"]),
        "git_dirty": bool(_git_value(["status", "--porcelain"])),
        "resume": bool(args.resume),
        "save_prompts": bool(args.save_prompts),
        "repeat": int(args.repeat),
        "llm_options": DEFAULT_LLM_OPTIONS,
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_value(arguments: list[str]) -> str:
    try:
        return subprocess.run(["git", *arguments], cwd=PROJECT_ROOT, capture_output=True, text=True, check=False).stdout.strip()
    except OSError:
        return ""


if __name__ == "__main__":
    raise SystemExit(main())
