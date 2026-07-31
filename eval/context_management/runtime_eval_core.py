from __future__ import annotations

import hashlib
import hashlib
import json
import os
import re
import tempfile
import time
import uuid
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, dataclass, is_dataclass, replace
from pathlib import Path
from typing import Any, Iterator

from main.agent.llm import LLMClient
from main.agent.profiles import TASK_CHAT_AGENT
from main.agent.prompt_builder import PromptBuilder
from main.assets import AssetManager
from main.devices.registry import DeviceRegistry
from main.document_index import ElasticsearchDocumentIndex, InMemoryDocumentIndex
from main.protocol import (
    ChatMessage,
    ChatRole,
    Conversation,
    EvidenceRef,
    Run,
    RunStatus,
    RunTriggerKind,
    StateSnapshot,
    Task,
    TaskStatus,
    TaskType,
    ToolCall,
    ToolCallStatus,
    ToolResult,
    new_id,
    utc_now,
)
from main.runtime.context import RuntimeContext
from main.runtime.engine import RunEngine
from main.runtime.projector import EventProjector
from main.state.memory import (
    InMemoryCaseRepo,
    InMemoryChatMessageRepo,
    InMemoryConversationRepo,
    InMemoryEventRepo,
    InMemoryKnowledgeBase,
    InMemoryPartRepo,
    InMemoryRunRepo,
    InMemoryStateRepo,
    InMemoryTaskRepo,
    InMemoryToolCallRepo,
)
from main.tools.builtins import register_builtin_tools
from main.tools.registry import ToolRegistry
from main.upload_processing import UploadProcessor

from eval.context_management.runtime_eval_scoring import PSEUDO_TOOL_RE, judge_checkpoint, score_checkpoint


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PRED_PATH = PROJECT_ROOT / "main/data/runtime_assets/chat_assets/files/asset_25645b890280.pdf"
DEFAULT_API_DOCS_PATH = PROJECT_ROOT / "main/data/runtime_assets/chat_assets/files/asset_01847021fe6b.md"
SEMANTIC_ENV = "DEEPEM_CONTEXT_SEMANTIC_COMPRESSION"


@dataclass(slots=True)
class PreparedDocuments:
    asset_manager: AssetManager
    document_index: Any
    asset_ids: dict[str, str]
    source_hashes: dict[str, str]
    source_paths: dict[str, str]
    _temp_dir: tempfile.TemporaryDirectory[str]
    _delete_index_on_close: bool = False

    def __enter__(self) -> PreparedDocuments:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def close(self) -> None:
        try:
            if self._delete_index_on_close:
                self.document_index.client.indices.delete(
                    index=self.document_index.index_name,
                    ignore_unavailable=True,
                )
        finally:
            self._temp_dir.cleanup()


class CollectingDebugLogger:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def safe_log(self, *, run_id: str, stage: str, payload: dict[str, Any]) -> None:
        self.records.append({"run_id": run_id, "stage": stage, "payload": _plain(payload), "logged_at": time.time()})


@dataclass(slots=True)
class EvalRuntime:
    context: RuntimeContext
    engine: RunEngine
    task: Task
    conversation: Conversation
    debug_logger: CollectingDebugLogger


def _build_eval_document_index_from_env(*, index_suffix: str | None = None) -> Any:
    if os.getenv("DEEPEM_USE_MEMORY_DOCUMENT_INDEX") == "1":
        return InMemoryDocumentIndex()
    url = (os.getenv("DEEPEM_ES_URL") or "http://127.0.0.1:9200").strip()
    frontend_index = (os.getenv("DEEPEM_ES_INDEX") or "deepem_knowledge").strip()
    configured_eval_index = (os.getenv("DEEPEM_EVAL_ES_INDEX") or "").strip()
    eval_index_base = configured_eval_index or f"{frontend_index}_context_eval"
    if eval_index_base == frontend_index:
        raise ValueError("DEEPEM_EVAL_ES_INDEX must differ from DEEPEM_ES_INDEX")
    suffix = (index_suffix or uuid.uuid4().hex[:12]).strip()
    if not suffix:
        raise ValueError("evaluation index suffix must not be empty")
    index_name = f"{eval_index_base}_{suffix}"
    username = (os.getenv("DEEPEM_ES_USERNAME") or "").strip() or None
    password = (os.getenv("DEEPEM_ES_PASSWORD") or "").strip() or None
    return ElasticsearchDocumentIndex(
        url=url,
        index_name=index_name,
        username=username,
        password=password,
    )


def prepare_documents(
    llm_client: LLMClient,
    *,
    pred_path: Path | None = None,
    api_docs_path: Path | None = None,
) -> PreparedDocuments:
    sources = {
        "pred": _resolve_document_source("PReD.pdf", pred_path or DEFAULT_PRED_PATH),
        "api_docs": _resolve_document_source("API_DOCS.md", api_docs_path or DEFAULT_API_DOCS_PATH),
    }
    temp_dir = tempfile.TemporaryDirectory(prefix="deepem-context-eval-")
    asset_manager = AssetManager(Path(temp_dir.name) / "chat_assets")
    document_index = _build_eval_document_index_from_env()
    delete_index_on_close = isinstance(document_index, ElasticsearchDocumentIndex)
    processor = UploadProcessor(asset_manager=asset_manager, document_index=document_index, llm_client=llm_client)
    asset_ids: dict[str, str] = {}
    source_hashes: dict[str, str] = {}
    source_paths: dict[str, str] = {}
    canonical_names = {"pred": "PReD.pdf", "api_docs": "API_DOCS.md"}
    try:
        for key, source in sources.items():
            content = source.read_bytes()
            canonical_name = canonical_names[key]
            if source.suffix.lower() != Path(canonical_name).suffix.lower():
                canonical_name = source.name
            result = processor.process_upload(
                file_name=canonical_name,
                content=content,
                conversation_id="context-eval-documents",
            )
            document_index.index_documents(processor.build_index_payloads(result))
            asset_ids[key] = result.asset.asset_id
            source_hashes[key] = hashlib.sha256(content).hexdigest()
            source_paths[key] = str(source)
    except Exception:
        try:
            if delete_index_on_close:
                try:
                    document_index.client.indices.delete(
                        index=document_index.index_name,
                        ignore_unavailable=True,
                    )
                except Exception:
                    pass
        finally:
            temp_dir.cleanup()
        raise
    return PreparedDocuments(
        asset_manager=asset_manager,
        document_index=document_index,
        asset_ids=asset_ids,
        source_hashes=source_hashes,
        source_paths=source_paths,
        _temp_dir=temp_dir,
        _delete_index_on_close=delete_index_on_close,
    )


def build_eval_runtime(
    case: dict[str, Any],
    *,
    strategy: str,
    repeat_index: int,
    llm_client: LLMClient,
    documents: PreparedDocuments,
    max_agent_steps: int | None = None,
) -> EvalRuntime:
    case_id = str(case["id"])
    suffix = f"{_slug(case_id)}_{strategy}_{repeat_index}"
    now = utc_now()
    task = Task(
        id=f"task_eval_{suffix}",
        task_type=TaskType.PLACE_DETECTION,
        target={"place_id": "context-eval-lab"},
        input={"eval_case_id": case_id, "strategy": strategy},
        status=TaskStatus.RUNNING,
        created_by="context_runtime_eval",
        created_at=now,
        updated_at=now,
    )
    conversation = Conversation(
        id=f"conv_eval_{suffix}",
        task_id=task.id,
        title=f"Context Eval {case_id}",
        title_source="evaluation",
        created_at=now,
        updated_at=now,
    )
    task_repo = InMemoryTaskRepo()
    conversation_repo = InMemoryConversationRepo()
    chat_repo = InMemoryChatMessageRepo()
    event_repo = InMemoryEventRepo()
    run_repo = InMemoryRunRepo()
    part_repo = InMemoryPartRepo()
    tool_call_repo = InMemoryToolCallRepo()
    state_repo = InMemoryStateRepo()
    case_repo = InMemoryCaseRepo()
    debug_logger = CollectingDebugLogger()
    task_repo.create(task)
    conversation_repo.create(conversation)
    state_repo.create(_build_workspace_state(task.id, str(case.get("workspace_profile") or "minimal"), case_id))
    _append_prior_messages(case, chat_repo, task, conversation)
    _seed_tool_results(case, run_repo, tool_call_repo, task, conversation, documents.asset_ids)
    profile = replace(TASK_CHAT_AGENT, step_budget=max_agent_steps) if max_agent_steps is not None else TASK_CHAT_AGENT
    context = RuntimeContext(
        task_repo=task_repo,
        conversation_repo=conversation_repo,
        chat_repo=chat_repo,
        event_repo=event_repo,
        run_repo=run_repo,
        part_repo=part_repo,
        tool_call_repo=tool_call_repo,
        state_repo=state_repo,
        case_repo=case_repo,
        knowledge_base=InMemoryKnowledgeBase(),
        tool_registry=register_builtin_tools(ToolRegistry()),
        device_registry=DeviceRegistry(),
        projector=EventProjector(state_repo=state_repo, case_repo=case_repo),
        llm_client=llm_client,
        prompt_builder=PromptBuilder(),
        profiles={profile.name: profile},
        asset_manager=documents.asset_manager,
        document_index=documents.document_index,
        debug_logger=debug_logger,
    )
    return EvalRuntime(
        context=context,
        engine=RunEngine(context),
        task=task,
        conversation=conversation,
        debug_logger=debug_logger,
    )


def run_case(
    case: dict[str, Any],
    *,
    strategy: str,
    repeat_index: int,
    llm_client: LLMClient,
    documents: PreparedDocuments,
    judge_client: LLMClient | None = None,
    save_prompts: bool = False,
    max_agent_steps: int | None = None,
) -> dict[str, Any]:
    if strategy not in {"rule", "semantic"}:
        raise ValueError(f"Unsupported strategy: {strategy}")
    runtime = build_eval_runtime(
        case,
        strategy=strategy,
        repeat_index=repeat_index,
        llm_client=llm_client,
        documents=documents,
        max_agent_steps=max_agent_steps,
    )
    started_at = utc_now()
    checkpoints: list[dict[str, Any]] = []
    all_turns: list[dict[str, Any]] = []
    with _semantic_strategy(strategy):
        for turn_index, turn in enumerate(case.get("turns") or []):
            checkpoint = _execute_turn(
                runtime,
                case=case,
                turn=turn,
                turn_index=turn_index,
                documents=documents,
                save_prompts=save_prompts,
            )
            scored = bool(turn.get("score"))
            checkpoint["scored"] = scored
            if scored:
                judge_payload = None
                judge_error = None
                if judge_client is not None:
                    judge_payload, judge_error = judge_checkpoint(
                        judge_client,
                        question=str(turn.get("user_message") or ""),
                        gold=dict(turn.get("gold") or {}),
                        answer=str(checkpoint.get("answer") or ""),
                        tool_calls=list(checkpoint.get("tool_calls") or []),
                        evidence=list(checkpoint.get("retrieved_evidence") or []),
                    )
                checkpoint["judge"] = judge_payload
                checkpoint["judge_error"] = judge_error
                checkpoint["rule_score"] = score_checkpoint(checkpoint, judge=judge_payload)
                checkpoints.append(checkpoint)
                all_turns.append(
                    {
                        "turn_id": checkpoint.get("turn_id"),
                        "scored": True,
                        "checkpoint_index": len(checkpoints) - 1,
                    }
                )
            else:
                all_turns.append(checkpoint)
            if checkpoint.get("error") and str(case.get("execution_mode")) == "sequential_agent":
                break
    finished_at = utc_now()
    passed = bool(checkpoints) and all(bool(item.get("rule_score", {}).get("passed")) for item in checkpoints)
    pressure_result = evaluate_pressure_target(case, [*all_turns, *checkpoints])
    if pressure_result and pressure_result["measurement_available"] and not pressure_result["within_target_band"]:
        passed = False
    return {
        "experiment_key": f"{case['id']}:{strategy}:{repeat_index}",
        "case_id": str(case["id"]),
        "suite": str(case["suite"]),
        "category": str(case.get("category") or ""),
        "difficulty": str(case.get("difficulty") or ""),
        "execution_mode": str(case.get("execution_mode") or ""),
        "critical_repeat": bool(case.get("critical_repeat")),
        "strategy": strategy,
        "repeat_index": repeat_index,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "latency_ms": int((finished_at - started_at).total_seconds() * 1000),
        "passed": passed,
        "checkpoint_count": len(checkpoints),
        "turn_count": len(all_turns),
        "all_turns": all_turns,
        "checkpoints": checkpoints,
        "pressure_result": pressure_result,
    }


def evaluate_pressure_target(case: dict[str, Any], turns: list[dict[str, Any]]) -> dict[str, Any] | None:
    target = case.get("pressure_target")
    if not isinstance(target, dict):
        return None
    measurement_turn_id = str(target.get("measurement_turn_id") or "").strip() or None
    measurement_turns = (
        [turn for turn in turns if isinstance(turn, dict) and str(turn.get("turn_id") or "") == measurement_turn_id]
        if measurement_turn_id
        else turns
    )
    ratios = []
    for turn in measurement_turns:
        if not isinstance(turn, dict):
            continue
        metrics = turn.get("metrics", {})
        value = metrics.get("peak_before_reduction_token_usage_ratio")
        if value is None:
            value = metrics.get("peak_token_usage_ratio")
        if value is not None:
            ratios.append(float(value))
    actual = max(ratios) if ratios else None
    lower = float(target["min_usage_ratio"])
    upper = float(target["max_usage_ratio"])
    return {
        "target_min_usage_ratio": lower,
        "target_max_usage_ratio": upper,
        "actual_peak_token_usage_ratio": actual,
        "within_target_band": actual is not None and lower <= actual <= upper,
        "measurement_available": actual is not None,
        "measurement_turn_id": measurement_turn_id,
        "measurement_mode": "declared_turn" if measurement_turn_id else "all_turns_peak",
    }


def _execute_turn(
    runtime: EvalRuntime,
    *,
    case: dict[str, Any],
    turn: dict[str, Any],
    turn_index: int,
    documents: PreparedDocuments,
    save_prompts: bool,
) -> dict[str, Any]:
    before_debug = len(runtime.debug_logger.records)
    attachments = _turn_attachments(case, turn_index, documents)
    message = ChatMessage(
        id=new_id("msg_eval"),
        conversation_id=runtime.conversation.id,
        task_id=runtime.task.id,
        role=ChatRole.OPERATOR,
        content=str(turn.get("user_message") or ""),
        run_id=None,
        created_at=utc_now(),
        attachments=attachments,
    )
    runtime.context.chat_repo.append(message)
    events: list[dict[str, Any]] = []

    def collect_event(name: str, data: dict[str, Any]) -> None:
        if name in {
            "context_compression",
            "context_reduction",
            "context_budget",
            "prompt_composition",
            "context_aggressive_compression",
            "context_retry",
        }:
            events.append({"name": name, "data": _plain(data)})
    start = time.perf_counter()
    error: str | None = None
    answer = ""
    try:
        response = runtime.engine.run_chat(
            runtime.task.id,
            message.id,
            conversation_id=runtime.conversation.id,
            event_handler=collect_event,
        )
        if response is not None:
            answer = response.content
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    latency_ms = int((time.perf_counter() - start) * 1000)
    runs = runtime.context.run_repo.list_by_task(runtime.task.id)
    run = runs[-1] if runs else None
    run_id = run.id if run else None
    records = [
        item
        for item in runtime.debug_logger.records[before_debug:]
        if run_id is None or item.get("run_id") == run_id
    ]
    tool_calls = _collect_tool_calls(runtime, run_id)
    prompt_records = [item for item in records if item.get("stage") == "prompt_built"]
    llm_records = [item for item in records if item.get("stage") == "llm_response"]
    llm_steps = _collect_llm_step_diagnostics(records)
    pseudo_tool_call = any(
        PSEUDO_TOOL_RE.search(
            f"{item.get('payload', {}).get('reasoning') or ''}\n{item.get('payload', {}).get('content') or ''}"
        )
        for item in llm_records
    )
    prompt_steps = [_prompt_step_metrics(item.get("payload") or {}, save_prompts=save_prompts) for item in prompt_records]
    summaries = [item.get("context_summary") for item in prompt_steps if item.get("context_summary")]
    anchor_fields = _anchor_fields(summaries)
    retrieved_evidence = _collect_evidence(runtime, run_id)
    connection_error = "connection error" in f"{error or ''} {answer}".casefold()
    empty_assistant_response = (
        error is None
        and run is not None
        and run.stop_reason == "assistant_response"
        and not str(answer or "").strip()
    )
    return {
        "turn_id": str(turn.get("turn_id") or ""),
        "question": str(turn.get("user_message") or ""),
        "gold": deepcopy(turn.get("gold") or {}),
        "answer": answer,
        "run_id": run_id,
        "run_status": str(run.status) if run else None,
        "run_stop_reason": run.stop_reason if run else None,
        "latency_ms": latency_ms,
        "error": error,
        "connection_error": connection_error,
        "empty_assistant_response": empty_assistant_response,
        "events": events,
        "tool_calls": tool_calls,
        "llm_steps": llm_steps,
        "prompt_steps": prompt_steps,
        "summary_merge_count": len(summaries),
        "context_summaries": summaries,
        "anchor_fields_present": sorted(anchor_fields),
        "retrieved_evidence": retrieved_evidence,
        "pseudo_tool_call": pseudo_tool_call,
        "metrics": _aggregate_turn_metrics(prompt_steps, tool_calls),
    }


def _collect_tool_calls(runtime: EvalRuntime, run_id: str | None) -> list[dict[str, Any]]:
    if not run_id:
        return []
    items: list[dict[str, Any]] = []
    for call in runtime.context.tool_call_repo.list_by_run(run_id):
        result_data = dict(call.result.data or {}) if call.result else {}
        items.append(
            {
                "tool_result_id": call.id,
                "tool_name": call.tool_name,
                "arguments_fingerprint": _arguments_fingerprint(call.input),
                "status": str(call.status),
                "result_status": call.result.status if call.result else None,
                "found": result_data.get("found"),
                "path": result_data.get("path"),
                "result_count": result_data.get("result_count"),
                "has_error": bool(call.result.error if call.result else None),
            }
        )
    return items


def _collect_evidence(runtime: EvalRuntime, run_id: str | None) -> list[dict[str, Any]]:
    if not run_id:
        return []
    evidence: list[dict[str, Any]] = []
    for call in runtime.context.tool_call_repo.list_by_run(run_id):
        if call.result is None:
            continue
        data = dict(call.result.data or {})
        if call.tool_name == "query_uploaded_documents":
            for item in list(data.get("results") or [])[:5]:
                evidence.append(
                    {
                        "tool_result_id": call.id,
                        "asset_id": item.get("asset_id"),
                        "file_name": item.get("file_name"),
                        "page": item.get("page"),
                        "chunk_index": item.get("chunk_index"),
                        "text": str(item.get("chunk_text") or "")[:240],
                    }
                )
        elif call.tool_name == "retrieve_tool_result_detail":
            evidence.append(
                {
                    "tool_result_id": data.get("tool_result_id"),
                    "path": data.get("path"),
                    "found": data.get("found"),
                    "text": str(data.get("value") or "")[:240],
                }
            )
    return evidence


def _prompt_step_metrics(payload: dict[str, Any], *, save_prompts: bool) -> dict[str, Any]:
    context_budget = deepcopy(payload.get("context_budget") or {})
    compression = deepcopy(payload.get("context_compression") or {})
    reduction = deepcopy(payload.get("context_reduction") or {})
    # Raw Semantic output is retained only in opt-in debug JSONL, never in evaluation results.
    compression.pop("semantic_raw_output", None)
    composition = deepcopy(payload.get("prompt_composition") or {})
    messages = deepcopy(payload.get("assembled_messages") or [])
    summary = _extract_context_summary(messages)
    result = {
        "compression_mode": payload.get("compression_mode"),
        "context_retry_attempt": payload.get("context_retry_attempt"),
        "context_retry_reason": payload.get("context_retry_reason"),
        "context_budget": context_budget,
        "normal_context_budget": deepcopy(payload.get("normal_context_budget") or {}),
        "aggressive_context_budget": deepcopy(payload.get("aggressive_context_budget") or {}),
        "context_compression": compression,
        "context_reduction": reduction,
        "tool_transcript_compression": deepcopy(payload.get("tool_transcript_compression") or {}),
        "prompt_composition": composition,
        "context_summary": summary,
    }
    if save_prompts:
        result["assembled_messages"] = messages
    return result


def _collect_llm_step_diagnostics(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    request_times: dict[int, float] = {}
    diagnostics: list[dict[str, Any]] = []
    response_ordinal = 0
    for record in records:
        stage = str(record.get("stage") or "")
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        step_index = _optional_int(payload.get("step_index"))
        if stage == "llm_request" and step_index is not None:
            request_times[step_index] = float(record.get("logged_at") or 0)
            continue
        if stage != "llm_response":
            continue
        response_ordinal += 1
        if step_index is None:
            step_index = response_ordinal
        raw = payload.get("raw") if isinstance(payload.get("raw"), dict) else {}
        raw_metrics = _raw_llm_metrics(raw)
        started_at = request_times.get(step_index)
        finished_at = float(record.get("logged_at") or 0)
        latency_ms = round(max(0.0, finished_at - started_at) * 1000) if started_at is not None else None
        diagnostics.append(
            {
                "step_index": step_index,
                "latency_ms": latency_ms,
                "finish_reason": raw_metrics["finish_reason"],
                "content_chars": _text_chars(payload.get("content")),
                "reasoning_chars": _text_chars(payload.get("reasoning")),
                "raw_content_chars": raw_metrics["raw_content_chars"],
                "raw_reasoning_chars": raw_metrics["raw_reasoning_chars"],
                "tool_call_count": len(payload.get("tool_calls") or []),
                "input_tokens": raw_metrics["input_tokens"],
                "output_tokens": raw_metrics["output_tokens"],
                "reasoning_tokens": raw_metrics["reasoning_tokens"],
                "total_tokens": raw_metrics["total_tokens"],
            }
        )
    return diagnostics


def _raw_llm_metrics(raw: dict[str, Any]) -> dict[str, Any]:
    chunks = raw.get("chunks") if isinstance(raw.get("chunks"), list) else []
    sources = chunks or [raw]
    finish_reason: str | None = None
    raw_content_chars = 0
    raw_reasoning_chars = 0
    usage: dict[str, Any] = {}
    for source in sources:
        if not isinstance(source, dict):
            continue
        source_usage = source.get("usage")
        if isinstance(source_usage, dict) and source_usage:
            usage = source_usage
        choices = source.get("choices") if isinstance(source.get("choices"), list) else []
        for choice in choices[:1]:
            if not isinstance(choice, dict):
                continue
            if choice.get("finish_reason") is not None:
                finish_reason = str(choice.get("finish_reason"))
            message = choice.get("delta") if isinstance(choice.get("delta"), dict) else choice.get("message")
            if not isinstance(message, dict):
                continue
            raw_content_chars += _text_chars(message.get("content"))
            raw_reasoning_chars += _text_chars(
                message.get("reasoning_content")
                or message.get("reasoning")
                or message.get("reasoning_delta")
                or message.get("thinking")
            )
    details = usage.get("completion_tokens_details")
    if not isinstance(details, dict):
        details = usage.get("output_tokens_details") if isinstance(usage.get("output_tokens_details"), dict) else {}
    return {
        "finish_reason": finish_reason,
        "raw_content_chars": raw_content_chars,
        "raw_reasoning_chars": raw_reasoning_chars,
        "input_tokens": _optional_int(usage.get("prompt_tokens") or usage.get("input_tokens")),
        "output_tokens": _optional_int(usage.get("completion_tokens") or usage.get("output_tokens")),
        "reasoning_tokens": _optional_int(details.get("reasoning_tokens")),
        "total_tokens": _optional_int(usage.get("total_tokens")),
    }


def _text_chars(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, str):
        return len(value)
    if isinstance(value, list):
        return sum(_text_chars(item.get("text") if isinstance(item, dict) else item) for item in value)
    return len(str(value))


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _arguments_fingerprint(arguments: Any) -> str:
    encoded = json.dumps(arguments or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def _aggregate_turn_metrics(prompt_steps: list[dict[str, Any]], tool_calls: list[dict[str, Any]]) -> dict[str, Any]:
    budgets = [item.get("context_budget") or {} for item in prompt_steps]
    token_values = [int(item.get("estimated_tokens") or 0) for item in budgets if item.get("estimated_tokens") is not None]
    ratios = [float(item.get("token_usage_ratio") or 0) for item in budgets if item.get("token_usage_ratio") is not None]
    reductions = [item.get("context_reduction") or {} for item in prompt_steps if item.get("context_reduction")]
    before_reduction_ratios = [
        float(item["before_reduction_token_usage_ratio"])
        for item in reductions
        if item.get("before_reduction_token_usage_ratio") is not None
    ]
    after_reduction_ratios = [
        float(item["after_reduction_token_usage_ratio"])
        for item in reductions
        if item.get("after_reduction_token_usage_ratio") is not None
    ]
    compressions = [item.get("context_compression") or {} for item in prompt_steps]
    summary_cache_statuses = [
        str(item.get("summary_cache_status"))
        for item in reductions
        if item.get("summary_cache_status")
    ]
    semantic_attempts = [item for item in compressions if item.get("semantic_attempted") is True]
    semantic_successes = [item for item in semantic_attempts if item.get("semantic_applied")]
    semantic_skips = [
        item
        for item in compressions
        if item.get("semantic_enabled") is True and item.get("semantic_attempted") is not True
        and item.get("summary_cache_status") != "hit"
    ]
    semantic_call_latencies = [
        float(item["semantic_latency_ms"])
        for item in semantic_attempts
        if item.get("semantic_latency_ms") is not None
    ]
    applied_compressions = [item for item in compressions if item.get("applied") is True]
    summary_ratios = [
        float(item["summary_compression_ratio"])
        for item in applied_compressions
        if item.get("summary_compression_ratio") is not None
    ]
    tool_compressions = [item.get("tool_transcript_compression") or {} for item in prompt_steps]
    applied_tool_compressions = [item for item in tool_compressions if item.get("applied") is True]
    largest_components = [
        str((item.get("prompt_composition") or {}).get("largest_component") or "") for item in prompt_steps
    ]
    retrieval_calls = [item for item in tool_calls if item.get("tool_name") == "retrieve_tool_result_detail"]
    successful_retrievals = [item for item in retrieval_calls if item.get("found") is True]
    duplicate_tool_calls = len(tool_calls) - len(
        {
            (
                str(item.get("tool_name") or ""),
                str(item.get("arguments_fingerprint") or json.dumps(item.get("arguments") or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))),
            )
            for item in tool_calls
        }
    )
    tool_result_compressions = [
        item.get("compression") for item in tool_calls if isinstance(item.get("compression"), dict)
    ]
    tool_result_raw_chars = sum(int(item.get("raw_chars") or 0) for item in tool_result_compressions)
    tool_result_compact_chars = sum(int(item.get("compact_chars") or 0) for item in tool_result_compressions)
    return {
        "prompt_step_count": len(prompt_steps),
        "peak_estimated_tokens": max(token_values, default=None),
        "peak_token_usage_ratio": max(ratios, default=None),
        "final_token_usage_ratio": ratios[-1] if ratios else None,
        "peak_before_reduction_token_usage_ratio": max(before_reduction_ratios, default=None),
        "peak_after_reduction_token_usage_ratio": max(after_reduction_ratios, default=None),
        "reducer_stages": list(dict.fromkeys(str(item.get("reducer_stage")) for item in reductions if item.get("reducer_stage"))),
        "hard_limit_satisfied": all(item.get("hard_limit_satisfied") is not False for item in reductions) if reductions else None,
        "history_units_total": max([int(item.get("history_units_total") or 0) for item in reductions], default=0),
        "history_units_kept": max([int(item.get("history_units_kept") or 0) for item in reductions], default=0),
        "history_units_summarized": max([int(item.get("history_units_summarized") or 0) for item in reductions], default=0),
        "history_units_dropped": max([int(item.get("history_units_dropped") or 0) for item in reductions], default=0),
        "tool_pairs_total": max([int(item.get("tool_pairs_total") or 0) for item in reductions], default=0),
        "tool_pairs_compacted": max([int(item.get("tool_pairs_compacted") or 0) for item in reductions], default=0),
        "tool_pairs_invalid": max([int(item.get("tool_pairs_invalid") or 0) for item in reductions], default=0),
        "summary_cache_statuses": summary_cache_statuses,
        "summary_cache_hit_count": summary_cache_statuses.count("hit"),
        "summary_incremental_update_count": summary_cache_statuses.count("incremental"),
        "summary_rebuild_count": summary_cache_statuses.count("rebuild"),
        "summary_rejected_count": summary_cache_statuses.count("rejected"),
        "over_budget_prompt": any(value > 1.0 for value in ratios),
        "context_compression_triggered": bool(applied_compressions),
        "summary_compression_ratio": min(summary_ratios, default=None),
        "semantic_attempt_count": len(semantic_attempts),
        "semantic_success_count": len(semantic_successes),
        "semantic_skipped_count": len(semantic_skips),
        "semantic_fallback_reasons": [
            str(item.get("semantic_fallback_reason"))
            for item in semantic_attempts
            if item.get("semantic_fallback_reason")
        ],
        "semantic_input_chars_max": max(
            [int(item.get("semantic_input_chars") or 0) for item in semantic_attempts], default=0
        ),
        "semantic_input_original_chars_max": max(
            [int(item.get("semantic_input_original_chars") or 0) for item in semantic_attempts], default=0
        ),
        "semantic_input_trimmed": any(item.get("semantic_input_trimmed") is True for item in semantic_attempts),
        "semantic_call_latencies_ms": semantic_call_latencies,
        "semantic_latency_ms_total": (
            round(sum(semantic_call_latencies), 3)
            if semantic_call_latencies
            else None
        ),
        "semantic_latency_ms_max": max(semantic_call_latencies, default=None),
        "aggressive_compression_applied": any(item.get("compression_mode") == "aggressive" for item in prompt_steps),
        "largest_components": largest_components,
        "tool_transcript_compression_applied": bool(applied_tool_compressions),
        "tool_transcript_chars_before_max": max(
            [int(item.get("chars_before") or 0) for item in tool_compressions], default=0
        ),
        "tool_transcript_chars_after_min": min(
            [int(item.get("chars_after") or 0) for item in applied_tool_compressions], default=None
        ),
        "tool_transcript_compression_ratio": min(
            [float(item.get("compression_ratio") or 0) for item in applied_tool_compressions], default=None
        ),
        "compacted_tool_message_count": sum(
            int(item.get("compacted_tool_message_count") or 0) for item in applied_tool_compressions
        ),
        "tool_call_count": len(tool_calls),
        "duplicate_tool_call_count": duplicate_tool_calls,
        "duplicate_tool_rate": duplicate_tool_calls / len(tool_calls) if tool_calls else 0.0,
        "retrieval_call_count": len(retrieval_calls),
        "retrieval_success_count": len(successful_retrievals),
        "retrieval_success_rate": (
            len(successful_retrievals) / len(retrieval_calls) if retrieval_calls else None
        ),
        "tool_result_compression_count": len(tool_result_compressions),
        "tool_result_raw_chars_total": tool_result_raw_chars or None,
        "tool_result_compact_chars_total": tool_result_compact_chars or None,
        "tool_result_compression_ratio": (
            round(tool_result_compact_chars / tool_result_raw_chars, 6) if tool_result_raw_chars else None
        ),
    }


def _extract_context_summary(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    for message in messages:
        content = message.get("content")
        if not isinstance(content, str) or "conversation_context_summary" not in content:
            continue
        start = content.find("{")
        end = content.rfind("}")
        if start < 0 or end < start:
            continue
        try:
            payload = json.loads(content[start : end + 1])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _anchor_fields(summaries: list[dict[str, Any]]) -> set[str]:
    fields: set[str] = set()
    for summary in summaries:
        for item in summary.get("tool_result_index") or []:
            if not isinstance(item, dict):
                continue
            for key in ("tool_result_id", "tool_name", "file_id", "asset_id", "path", "retrieval_hint"):
                if item.get(key):
                    fields.add(key)
            if item.get("omitted_fields"):
                fields.add("path")
    return fields


def _append_prior_messages(case: dict[str, Any], chat_repo: InMemoryChatMessageRepo, task: Task, conversation: Conversation) -> None:
    role_map = {
        "operator": ChatRole.OPERATOR,
        "user": ChatRole.OPERATOR,
        "assistant": ChatRole.ASSISTANT,
        "system": ChatRole.SYSTEM,
    }
    for index, item in enumerate(case.get("prior_messages") or []):
        chat_repo.append(
            ChatMessage(
                id=f"msg_seed_{_slug(str(case['id']))}_{index}",
                conversation_id=conversation.id,
                task_id=task.id,
                role=role_map[str(item.get("role") or "operator")],
                content=str(item.get("content") or ""),
                run_id=None,
                created_at=utc_now(),
            )
        )


def _seed_tool_results(
    case: dict[str, Any],
    run_repo: InMemoryRunRepo,
    tool_call_repo: InMemoryToolCallRepo,
    task: Task,
    conversation: Conversation,
    asset_ids: dict[str, str],
) -> None:
    items = list(case.get("seed_tool_results") or [])
    if not items:
        return
    run = Run(
        id=f"run_seed_{_slug(str(case['id']))}",
        task_id=task.id,
        trigger_kind=RunTriggerKind.CHAT,
        trigger_event_id=None,
        trigger_message_id=None,
        agent_profile=TASK_CHAT_AGENT.name,
        status=RunStatus.COMPLETED,
        step_budget=TASK_CHAT_AGENT.step_budget,
        step_count=1,
        started_at=utc_now(),
        ended_at=utc_now(),
        summary="Seeded historical tool results for evaluation.",
        conversation_id=conversation.id,
    )
    run_repo.create(run)
    replacements = {f"${key}": value for key, value in asset_ids.items()}
    for item in items:
        result_payload = _replace_tokens(deepcopy(item.get("result") or {}), replacements)
        arguments = _replace_tokens(deepcopy(item.get("input") or {}), replacements)
        tool_call_repo.create(
            ToolCall(
                id=str(item["id"]),
                task_id=task.id,
                run_id=run.id,
                tool_name=str(item["tool_name"]),
                input=arguments,
                status=ToolCallStatus.COMPLETED,
                started_at=utc_now(),
                ended_at=utc_now(),
                result=ToolResult(
                    status=str(result_payload.get("status") or "success"),
                    data=dict(result_payload.get("data") or {}),
                    error=result_payload.get("error"),
                    metadata=dict(result_payload.get("metadata") or {}),
                ),
                conversation_id=conversation.id,
            )
        )


def _turn_attachments(case: dict[str, Any], turn_index: int, documents: PreparedDocuments) -> list[EvidenceRef]:
    if turn_index != 0 or case.get("prior_messages"):
        return []
    attachments: list[EvidenceRef] = []
    for key in case.get("documents") or []:
        asset_id = documents.asset_ids[str(key)]
        asset = documents.asset_manager.get(asset_id)
        attachments.append(
            EvidenceRef(
                kind="upload_asset",
                uri=documents.asset_manager.public_url(asset_id),
                label=asset.file_name,
                metadata={"asset_id": asset_id, "upload_kind": "document", "mime_type": asset.mime_type},
            )
        )
    return attachments


def _build_workspace_state(task_id: str, profile: str, case_id: str) -> StateSnapshot:
    metadata: dict[str, Any] = {"context_eval": {"case_id": case_id, "profile": profile}}
    if profile == "noisy_usrp":
        metadata.update(
            {
                "collector": {
                    "mode": "evaluation_fixture",
                    "devices": [{"dev_id": "usrp-30B1FDE", "status": "IDLE", "freq": 2_400_000_000}],
                    "activity": [
                        {"timestamp": f"2026-07-01T00:{index:02d}:00Z", "summary": f"无关采集活动 {index}"}
                        for index in range(12)
                    ],
                },
                "observation_history": [
                    {
                        "signal_id": f"eval-signal-{index}",
                        "freq": 2_400_000_000 + index * 100_000,
                        "classification": "observed",
                        "score": index / 10,
                    }
                    for index in range(10)
                ],
                "platform_logs": [f"[evaluation] irrelevant platform log {index}" for index in range(20)],
            }
        )
    return StateSnapshot(task_id=task_id, place_id="context-eval-lab", metadata=metadata)


@contextmanager
def _semantic_strategy(strategy: str) -> Iterator[None]:
    previous = os.environ.get(SEMANTIC_ENV)
    if strategy == "semantic":
        os.environ[SEMANTIC_ENV] = "1"
    else:
        os.environ.pop(SEMANTIC_ENV, None)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(SEMANTIC_ENV, None)
        else:
            os.environ[SEMANTIC_ENV] = previous


def _resolve_document_source(file_name: str, preferred: Path) -> Path:
    if preferred.exists():
        return preferred.resolve()
    meta_dir = PROJECT_ROOT / "main/data/runtime_assets/chat_assets/meta"
    file_dir = meta_dir.parent / "files"
    for meta_path in sorted(meta_dir.glob("*.json")):
        try:
            payload = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if str(payload.get("file_name") or "").casefold() != file_name.casefold():
            continue
        stored_name = str(payload.get("stored_name") or "")
        candidate = file_dir / stored_name
        if candidate.exists():
            return candidate.resolve()
    raise FileNotFoundError(f"Evaluation document not found: {file_name}")


def _replace_tokens(value: Any, replacements: dict[str, str]) -> Any:
    if isinstance(value, str):
        for token, replacement in replacements.items():
            value = value.replace(token, replacement)
        return value
    if isinstance(value, dict):
        return {str(key): _replace_tokens(item, replacements) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_tokens(item, replacements) for item in value]
    return value


def _plain(value: Any) -> Any:
    if is_dataclass(value):
        return _plain(asdict(value))
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_plain(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


def _slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "-", value).strip("-").lower()
