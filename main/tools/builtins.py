from __future__ import annotations

from copy import deepcopy
from deepem.devices.abstract import DeviceCommand
from deepem.protocol import CaseRecord, CaseStatus, EventType, SignalRecord, ToolResult, new_id, utc_now
from deepem.tools.base import EventDraft, ToolContext, ToolDefinition, ToolExecutionResult
from deepem.tools.document_tool import build_query_uploaded_documents_tool
from deepem.tools.autonomous_usrp import (
    build_execute_usrp_task_code_tool,
    build_generate_usrp_task_code_tool,
    build_retrieve_usrp_api_knowledge_tool,
    build_run_autonomous_usrp_task_tool,
)
from deepem.tools.registry import ToolRegistry
from deepem.tools.spectrum_collection import (
    build_execute_spectrum_collection_tool,
    build_prepare_spectrum_collection_tool,
)
from deepem.tools.spectrum_baseline import (
    build_compare_spectrum_with_baseline_tool,
    build_extract_baseline_spectrum_peaks_tool,
)
from deepem.tools.sql_tool import build_query_local_database_tool


def _serialize_state(state) -> dict[str, object]:
    return {
        "task_id": state.task_id,
        "place_id": state.place_id,
        "metadata": dict(state.metadata),
        "active_signals": {
            signal_id: {
                "signal_id": item.signal_id,
                "fingerprint": item.fingerprint,
                "classification": item.classification,
                "carries_information": item.carries_information,
                "suspected_device_type": item.suspected_device_type,
                "metadata": dict(item.metadata),
            }
            for signal_id, item in state.active_signals.items()
        },
    }


def _serialize_cases(cases: list[CaseRecord]) -> list[dict[str, object]]:
    return [
        {
            "case_id": case.id,
            "signal_id": case.signal_id,
            "status": case.status,
            "risk_level": case.risk_level,
            "hypothesis": case.hypothesis,
            "next_actions": list(case.next_actions),
            "notes": list(case.notes),
        }
        for case in cases
    ]


def _query_state(args: dict[str, object], context: ToolContext) -> ToolExecutionResult:
    state = deepcopy(context.state_repo.get(context.task.id))
    state.active_signals = {
        key: value
        for key, value in state.active_signals.items()
        if value.conversation_id == context.run.conversation_id
    }
    return ToolExecutionResult(result=ToolResult(status="success", data=_serialize_state(state)))


def _query_cases(args: dict[str, object], context: ToolContext) -> ToolExecutionResult:
    cases = [
        case for case in context.case_repo.list_by_task(context.task.id)
        if case.conversation_id == context.run.conversation_id
    ]
    return ToolExecutionResult(result=ToolResult(status="success", data={"cases": _serialize_cases(cases)}))


def _query_knowledge(args: dict[str, object], context: ToolContext) -> ToolExecutionResult:
    place_id = str(args.get("place_id", context.task.target["place_id"]))
    fingerprint = args.get("fingerprint")
    query = args.get("query")
    place = context.knowledge_base.describe_place(place_id)
    try:
        state = context.state_repo.get(context.task.id)
        baseline_meta = state.metadata.get("place_baseline")
        if isinstance(baseline_meta, dict) and baseline_meta.get("initialized") and str(baseline_meta.get("place_id") or place_id) == place_id:
            fingerprints = [str(item).strip() for item in baseline_meta.get("fingerprints", []) if str(item).strip()]
            place = {
                **place,
                "baseline_fingerprints": fingerprints,
                "expected_signal_count": len(fingerprints),
                "baseline_initialized": True,
                "baseline_source": baseline_meta.get("source", "manual_ui"),
                "baseline_updated_at": baseline_meta.get("updated_at", ""),
            }
        else:
            place = {
                **place,
                "candidate_baseline_fingerprints": list(place.get("baseline_fingerprints", [])),
                "baseline_fingerprints": [],
                "expected_signal_count": 0,
                "baseline_initialized": False,
                "baseline_note": "场所基线尚未由操作员手动初始化。",
            }
    except Exception:
        pass
    result = {
        "place": place,
        "signal": context.knowledge_base.lookup_signal(str(fingerprint)) if fingerprint else None,
        "search": context.knowledge_base.search(str(query), place_id=place_id) if query else None,
    }
    return ToolExecutionResult(result=ToolResult(status="success", data=result))


def _query_recent_observations(args: dict[str, object], context: ToolContext) -> ToolExecutionResult:
    limit = int(args.get("limit", 10))
    state = context.state_repo.get(context.task.id)
    items = list(state.metadata.get("recent_observations", []))[-limit:]
    return ToolExecutionResult(result=ToolResult(status="success", data={"observations": items}))


def _list_usrp_devices(args: dict[str, object], context: ToolContext) -> ToolExecutionResult:
    adapter = context.device_registry.find_by_capability("list_usrp_devices")
    if adapter is None:
        return ToolExecutionResult(result=ToolResult(status="error", error="USRP device adapter is not registered"))
    result = adapter.execute(DeviceCommand(device_id=adapter.device_id, capability="list_usrp_devices", args={}))
    return ToolExecutionResult(result=ToolResult(status=result.status, data=result.data, error=result.error))


def _scan_usrp_devices(args: dict[str, object], context: ToolContext) -> ToolExecutionResult:
    adapter = context.device_registry.find_by_capability("scan_usrp_devices")
    if adapter is None:
        return ToolExecutionResult(result=ToolResult(status="error", error="USRP device adapter is not registered"))
    result = adapter.execute(DeviceCommand(device_id=adapter.device_id, capability="scan_usrp_devices", args={}))
    return ToolExecutionResult(result=ToolResult(status=result.status, data=result.data, error=result.error))


def _configure_usrp_capture(args: dict[str, object], context: ToolContext) -> ToolExecutionResult:
    adapter = context.device_registry.find_by_capability("configure_usrp_capture")
    if adapter is None:
        return ToolExecutionResult(result=ToolResult(status="error", error="USRP device adapter is not registered"))
    result = adapter.execute(DeviceCommand(device_id=adapter.device_id, capability="configure_usrp_capture", args=dict(args or {})))
    return ToolExecutionResult(result=ToolResult(status=result.status, data=result.data, error=result.error))


def _query_usrp_task(args: dict[str, object], context: ToolContext) -> ToolExecutionResult:
    task_id = str(args.get("task_id") or "")
    adapter = context.device_registry.find_by_capability("query_usrp_task")
    if adapter is None:
        return ToolExecutionResult(result=ToolResult(status="error", error="USRP device adapter is not registered"))
    result = adapter.execute(DeviceCommand(device_id=adapter.device_id, capability="query_usrp_task", args={"task_id": task_id}))
    return ToolExecutionResult(result=ToolResult(status=result.status, data=result.data, error=result.error))


def _request_focused_collection(args: dict[str, object], context: ToolContext) -> ToolExecutionResult:
    signal_id = str(args["signal_id"])
    state = context.state_repo.get(context.task.id)
    recent = list(state.metadata.get("recent_observations", []))
    seed = next((item for item in reversed(recent) if item.get("signal_id") == signal_id), None)
    collector = dict(state.metadata.get("collector") or {})
    active_session = dict(collector.get("active_session") or {})
    usrp_task_id = str(active_session.get("usrp_task_id") or "")
    task_status = None
    if usrp_task_id:
        adapter = context.device_registry.find_by_capability("query_usrp_task")
        if adapter is not None:
            task_status = adapter.execute(
                DeviceCommand(device_id=adapter.device_id, capability="query_usrp_task", args={"task_id": usrp_task_id})
            ).data
    focused = {
        "signal_id": signal_id,
        "capture_mode": "usrp_event_evidence_review",
        "note": "真实 USRP 采集由平台计划驱动；本工具只汇总已上报事件证据，不生成模拟采样窗口。",
        "active_session": {
            "session_id": active_session.get("session_id"),
            "status": active_session.get("status"),
            "device_id": active_session.get("device_id"),
            "usrp_task_id": usrp_task_id,
            "files_received": active_session.get("files_received"),
            "current_plan_step": active_session.get("current_plan_step"),
        },
        "matched_observation": seed,
        "usrp_task_status": task_status,
    }
    event_draft = EventDraft(
        event_type=EventType.COLLECTION_COMPLETED,
        source="tool:request_focused_collection",
        payload=focused,
        idempotency_key=f"focused:{context.task.id}:{signal_id}:{context.run.id}",
        evidence_refs=list(context.trigger_event.evidence_refs) if context.trigger_event else [],
    )
    return ToolExecutionResult(result=ToolResult(status="success", data=focused), event_drafts=[event_draft])


def _request_deep_analysis(args: dict[str, object], context: ToolContext) -> ToolExecutionResult:
    signal_id = str(args["signal_id"])
    adapter = context.device_registry.find_by_capability("analyze")
    device_result = None
    if adapter is not None:
        device_result = adapter.execute(
            DeviceCommand(device_id=adapter.device_id, capability="analyze", args={"signal_id": signal_id, **args})
        )
    state = context.state_repo.get(context.task.id)
    signal = state.active_signals.get(signal_id)
    signal_meta = signal.metadata if signal else {}
    rms = float(signal_meta.get("rms_energy", args.get("rms_energy", 0.28)))
    deviation = float(signal_meta.get("baseline_deviation_db", args.get("baseline_deviation_db", 0.0)))+0.3
    high_freq = float(signal_meta.get("high_frequency_score", args.get("high_frequency_score", 0.12)))
    score = deviation * 0.45 + high_freq * 8.0 + max((rms - 0.28) * 5.0, 0.0)
    if signal and signal.carries_information:
        score += 1.2
    abnormal = score >= 0.9 or deviation >= 0.9
    verdict = "异常" if abnormal else "倾向正常"
    risk_level = "high" if score >= 6.0 else ("medium" if abnormal else "low")
    hypothesis = "未知 2.4G 脉冲源，且与当前基线明显偏离" if abnormal else "更像是邻近基线设备引起的短时无线活动"
    summary = (
        f"聚焦复核评分={score:.2f}，基线偏离={deviation:.2f}dB，高频分数={high_freq:.3f}；最终判定={verdict}。"
    )
    payload = {
        "signal_id": signal_id,
        "verdict": verdict,
        "risk_level": risk_level,
        "hypothesis": hypothesis,
        "summary": summary,
        "analysis_score": round(score, 3),
        "device_result": device_result.data if device_result else None,
    }
    event_draft = EventDraft(
        event_type=EventType.PROTOCOL_ANALYSIS_COMPLETED,
        source="tool:request_deep_analysis",
        payload=payload,
        idempotency_key=f"deep-analysis:{context.task.id}:{signal_id}:{context.run.id}",
        evidence_refs=list(context.trigger_event.evidence_refs) if context.trigger_event else [],
    )
    return ToolExecutionResult(result=ToolResult(status="success", data=payload), event_drafts=[event_draft])


def _update_case(args: dict[str, object], context: ToolContext) -> ToolExecutionResult:
    signal_id = str(args["signal_id"])
    risk_level = str(args.get("risk_level", "medium"))
    note = str(args.get("note", ""))
    status = str(args.get("status", CaseStatus.INVESTIGATING))
    hypothesis = args.get("hypothesis")
    next_actions = [str(item) for item in args.get("next_actions", [])]
    case = context.case_repo.get_by_signal(context.task.id, signal_id)
    if case is not None and case.conversation_id != context.run.conversation_id:
        case = None
    if case is None:
        case = CaseRecord(id=new_id("case"), task_id=context.task.id, signal_id=signal_id, status=CaseStatus(status), risk_level=risk_level, conversation_id=context.run.conversation_id)
    case.status = CaseStatus(status)
    case.risk_level = risk_level
    if hypothesis:
        case.hypothesis = str(hypothesis)
    if note:
        case.notes.append(note)
    if next_actions:
        case.next_actions = next_actions
    case.updated_at = utc_now()
    case.conversation_id = case.conversation_id or context.run.conversation_id
    context.case_repo.save(case)
    return ToolExecutionResult(result=ToolResult(status="success", data={"case_id": case.id, "signal_id": signal_id, "status": case.status}))


def _update_state(args: dict[str, object], context: ToolContext) -> ToolExecutionResult:
    state = context.state_repo.get(context.task.id)
    metadata = dict(args.get("metadata", {}))
    state.metadata.update(metadata)
    signal_id = args.get("signal_id")
    if signal_id:
        signal_id = str(signal_id)
        record = state.active_signals.get(signal_id)
        if record is None:
            record = SignalRecord(signal_id=signal_id, fingerprint=str(args.get("fingerprint", "")), classification=str(args.get("classification", "observed")), conversation_id=context.run.conversation_id)
        if "classification" in args:
            record.classification = str(args["classification"])
        record.metadata.update(dict(args.get("signal_metadata", {})))
        record.conversation_id = record.conversation_id or context.run.conversation_id
        state.active_signals[signal_id] = record
    context.state_repo.save(state)
    return ToolExecutionResult(result=ToolResult(status="success", data=_serialize_state(state)))


def _record_operator_feedback(args: dict[str, object], context: ToolContext) -> ToolExecutionResult:
    event_draft = EventDraft(
        event_type=EventType.OPERATOR_FEEDBACK_RECEIVED,
        source="tool:record_operator_feedback",
        payload={
            "signal_id": str(args["signal_id"]),
            "verdict": str(args["verdict"]),
            "note": str(args.get("note", "")),
        },
        idempotency_key=f"feedback:{context.task.id}:{args['signal_id']}:{context.run.id}",
        evidence_refs=list(context.trigger_message.attachments) if context.trigger_message else [],
    )
    return ToolExecutionResult(result=ToolResult(status="success", data=event_draft.payload), event_drafts=[event_draft])


def register_builtin_tools(registry: ToolRegistry) -> ToolRegistry:
    registry.register(ToolDefinition(name="query_state", description="读取当前任务的状态快照。", input_schema={"type": "object", "properties": {}}, handler=_query_state))
    registry.register(ToolDefinition(name="query_cases", description="读取当前任务下的异常 Case 列表。", input_schema={"type": "object", "properties": {}}, handler=_query_cases))
    registry.register(ToolDefinition(name="query_knowledge", description="查询场所基线与已知专家经验。", input_schema={"type": "object", "properties": {"place_id": {"type": "string"}, "fingerprint": {"type": "string"}, "query": {"type": "string"}}}, handler=_query_knowledge))
    registry.register(ToolDefinition(name="query_recent_observations", description="读取处理层产生的近期实时观测。", input_schema={"type": "object", "properties": {"limit": {"type": "integer"}}}, handler=_query_recent_observations))
    registry.register(ToolDefinition(name="scan_usrp_devices", description="调用新 USRP scan API 扫描硬件并刷新设备能力与状态。", input_schema={"type": "object", "properties": {}}, handler=_scan_usrp_devices))
    registry.register(ToolDefinition(name="list_usrp_devices", description="调用新 USRP devices API 查询设备能力、状态、current_config 和 task_id。", input_schema={"type": "object", "properties": {}}, handler=_list_usrp_devices))
    registry.register(ToolDefinition(name="configure_usrp_capture", description="调用新 USRP configure API 预校验并保存采集参数，不启动采集。", input_schema={"type": "object", "properties": {"dev_id": {"type": "string"}, "freq": {"type": "number"}, "sample_rate": {"type": "number"}, "bandwidth": {"type": "number"}, "gain": {"type": "number"}, "slice_duration": {"type": "number"}, "duration": {"type": ["number", "null"]}, "antenna": {"type": ["string", "null"]}}, "required": ["dev_id", "freq", "sample_rate", "bandwidth", "gain", "slice_duration"]}, handler=_configure_usrp_capture))
    registry.register(ToolDefinition(name="query_usrp_task", description="按新 USRP devices API 推断运行中任务状态；完成/停止/失败历史以平台回调为准。", input_schema={"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}, handler=_query_usrp_task))
    registry.register(ToolDefinition(name="request_focused_collection", description="登记并汇总可疑信号的真实 USRP 事件证据，不生成模拟数据。", input_schema={"type": "object", "properties": {"signal_id": {"type": "string"}, "sample_hint": {"type": "integer"}}, "required": ["signal_id"]}, handler=_request_focused_collection))
    registry.register(ToolDefinition(name="request_deep_analysis", description="结合聚焦复采结果与状态特征，对可疑信号执行更深层分析。", input_schema={"type": "object", "properties": {"signal_id": {"type": "string"}}, "required": ["signal_id"]}, handler=_request_deep_analysis))
    registry.register(ToolDefinition(name="update_case", description="在任务工作区中持久化 Case 更新。", input_schema={"type": "object", "properties": {"signal_id": {"type": "string"}, "risk_level": {"type": "string"}, "status": {"type": "string"}, "hypothesis": {"type": "string"}, "note": {"type": "string"}, "next_actions": {"type": "array", "items": {"type": "string"}}}, "required": ["signal_id"]}, handler=_update_case))
    registry.register(ToolDefinition(name="update_state", description="在任务工作区中持久化受控状态更新。", input_schema={"type": "object", "properties": {"signal_id": {"type": "string"}, "fingerprint": {"type": "string"}, "classification": {"type": "string"}, "metadata": {"type": "object"}, "signal_metadata": {"type": "object"}}}, handler=_update_state))
    registry.register(ToolDefinition(name="record_operator_feedback", description="将聊天反馈转换为正式的操作员反馈事件。", input_schema={"type": "object", "properties": {"signal_id": {"type": "string"}, "verdict": {"type": "string"}, "note": {"type": "string"}}, "required": ["signal_id", "verdict"]}, handler=_record_operator_feedback))
    registry.register(build_query_local_database_tool())
    registry.register(build_query_uploaded_documents_tool())
    registry.register(build_retrieve_usrp_api_knowledge_tool())
    registry.register(build_generate_usrp_task_code_tool())
    registry.register(build_execute_usrp_task_code_tool())
    registry.register(build_run_autonomous_usrp_task_tool())
    registry.register(build_prepare_spectrum_collection_tool())
    registry.register(build_execute_spectrum_collection_tool())
    registry.register(build_extract_baseline_spectrum_peaks_tool())
    registry.register(build_compare_spectrum_with_baseline_tool())
    return registry
