from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from deepem.protocol import EventType, PartKind


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def fmt_float(value: Any, digits: int = 2) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def to_cn_agent_name(agent_name: str) -> str:
    mapping = {
        "place_detection_agent": "异常复核智能体",
        "task_chat_agent": "对话问答智能体",
        "realtime": "实时解析层",
        "tool": "工具链",
    }
    return mapping.get(agent_name, agent_name)


def to_cn_event_type(event_type: Any) -> str:
    mapping = {
        EventType.STRUCTURED_SIGNAL_DETECTED: "实时信号读入",
        EventType.COLLECTION_COMPLETED: "聚焦复核完成",
        EventType.PROTOCOL_ANALYSIS_COMPLETED: "深度分析完成",
        EventType.OPERATOR_FEEDBACK_RECEIVED: "人工反馈已记录",
    }
    return mapping.get(event_type, str(event_type))


def to_cn_part_kind(kind: Any) -> str:
    mapping = {
        PartKind.REASONING: "思考",
        PartKind.TOOL_CALL: "工具调用",
        PartKind.TEXT: "结论",
        PartKind.OBSERVATION: "观察",
        PartKind.CASE_UPDATE: "Case更新",
    }
    return mapping.get(kind, str(kind))


def to_cn_classification(value: str | None) -> str:
    mapping = {
        "observed": "正常",
        "suspicious": "异常",
        "unknown": "未知",
    }
    return mapping.get(str(value), str(value) if value is not None else "-")


def to_cn_case_status(value: str | None) -> str:
    mapping = {
        "open": "待研判",
        "investigating": "研判中",
        "closed": "研判完成",
    }
    return mapping.get(str(value), str(value) if value is not None else "-")


def determine_status(case, related_run, analysis_event) -> str:
    if analysis_event is not None:
        return "研判完成"
    if related_run is not None:
        return "研判中"
    if case is not None:
        return "待研判"
    return "待研判"


def to_cn_risk_level(value: str | None) -> str:
    mapping = {
        "low": "低",
        "medium": "中",
        "high": "高",
    }
    return mapping.get(str(value), str(value) if value is not None else "-")


def classify_level(event) -> str:
    if event.event_type == EventType.STRUCTURED_SIGNAL_DETECTED:
        return "warning" if event.payload.get("classification") == "suspicious" else "normal"
    if event.event_type in {
        EventType.COLLECTION_COMPLETED,
        EventType.PROTOCOL_ANALYSIS_COMPLETED,
        EventType.OPERATOR_FEEDBACK_RECEIVED,
    }:
        return "warning"
    return "info"


def normal_evidence_lines(event, baseline_fingerprints: list[str]) -> list[str]:
    payload = event.payload
    features = payload.get("features", {}) if isinstance(payload.get("features"), dict) else {}
    fingerprint = str(payload.get("fingerprint", "-"))
    evidence: list[str] = []
    if fingerprint in baseline_fingerprints:
        evidence.append(f"指纹 {fingerprint} 位于当前场景的已知基线名单中。")
    score = features.get("score")
    if score is not None:
        evidence.append(f"真实采集分析评分为 {fmt_float(score, 3)}，未达到告警阈值。")
    crest = features.get("crest")
    if crest is not None:
        evidence.append(f"峰均比（crest）为 {fmt_float(crest, 3)}。")
    burst_ratio = features.get("burst_ratio")
    if burst_ratio is not None:
        evidence.append(f"突发占比为 {fmt_float(burst_ratio, 4)}，整体更接近常规背景流量。")
    if not payload.get("carries_information"):
        evidence.append("未检测到明显的突发承载信息特征。")
    evidence.append("该 NPZ 文件已保留在主平台存储目录，便于后续人工回看。")
    return evidence


def event_summary(event) -> str:
    payload = event.payload
    signal_id = payload.get("signal_id", "-")
    fingerprint = payload.get("fingerprint", "-")
    if event.event_type == EventType.STRUCTURED_SIGNAL_DETECTED:
        classification = payload.get("classification")
        if classification == "suspicious":
            return f"检测到异常信号 {signal_id}（指纹：{fingerprint}）"
        return f"检测到正常信号 {signal_id}（指纹：{fingerprint}）"
    if event.event_type == EventType.COLLECTION_COMPLETED:
        return f"已完成对 {signal_id} 的聚焦复核"
    if event.event_type == EventType.PROTOCOL_ANALYSIS_COMPLETED:
        verdict = payload.get("verdict", "-")
        return f"智能体已完成 {signal_id} 的深度分析，结论：{verdict}"
    if event.event_type == EventType.OPERATOR_FEEDBACK_RECEIVED:
        return f"已记录人工反馈：{signal_id} / {payload.get('verdict', '-')}"
    return str(event.event_type)


def structured_signal_timeline_summary(event, related_run, analysis_event) -> str:
    payload = event.payload
    signal_id = payload.get("signal_id", "-")
    fingerprint = payload.get("fingerprint", "-")
    if payload.get("classification") != "suspicious":
        return f"检测到正常信号 {signal_id}（指纹：{fingerprint}）"
    if analysis_event is not None:
        return f"异常信号 {signal_id} 已完成大模型深度研判（指纹：{fingerprint}）"
    if related_run is not None:
        return f"异常信号 {signal_id} 正在由大模型研判（指纹：{fingerprint}）"
    return f"异常信号 {signal_id} 已进入大模型研判队列（指纹：{fingerprint}）"


def format_final_result(case, analysis_event) -> str:
    if analysis_event is not None:
        risk = to_cn_risk_level(str(analysis_event.payload.get("risk_level", "-")))
        verdict = str(analysis_event.payload.get("verdict", "-") or "-")
        hypothesis = str(analysis_event.payload.get("hypothesis", "") or "")
        summary = str(analysis_event.payload.get("summary", "") or "")
        parts = [f"判定：{verdict}", f"风险：{risk}"]
        if hypothesis:
            parts.append(f"假设：{hypothesis}")
        if summary:
            parts.append(summary)
        return "；".join(parts)
    if case is not None:
        parts = [f"状态：{to_cn_case_status(str(case.status))}", f"风险：{to_cn_risk_level(case.risk_level)}"]
        if case.hypothesis:
            parts.append(f"假设：{case.hypothesis}")
        return "；".join(parts)
    return "智能体尚未完成最终判定。"


def build_demo_snapshot(platform) -> dict[str, Any]:
    app = platform.app
    task = platform.task
    state = app.runtime.state_repo.get(task.id)
    events = app.runtime.event_repo.list_by_task(task.id)
    runs = app.runtime.run_repo.list_by_task(task.id)
    parts = app.runtime.part_repo.list_recent_by_task(task.id)
    conversation = app.runtime.conversation_repo.get_by_task(task.id)
    chats = app.runtime.chat_repo.list_by_conversation(conversation.id)
    cases = app.runtime.case_repo.list_by_task(task.id)

    observation_history = list(state.metadata.get("observation_history", []))
    recent_observations = observation_history[-12:]
    collector = dict(state.metadata.get("collector") or {})
    latest_spectrogram = collector.get("latest_spectrogram")
    baseline_meta = state.metadata.get("place_baseline")
    if isinstance(baseline_meta, dict) and baseline_meta.get("initialized"):
        baseline_fingerprints = [str(item).strip() for item in baseline_meta.get("fingerprints", []) if str(item).strip()]
    else:
        baseline_fingerprints = []

    runs_by_trigger_event = {run.trigger_event_id: run for run in runs if run.trigger_event_id}
    parts_by_run: dict[str, list[dict[str, Any]]] = {}
    for part in parts:
        parts_by_run.setdefault(part.run_id, []).append(
            {
                "kind": str(part.kind),
                "kind_label": to_cn_part_kind(part.kind),
                "content": part.content,
                "metadata": part.metadata,
            }
        )

    timeline = []
    signal_status_map: dict[str, str] = {}
    analysis_event_by_signal: dict[str, Any] = {}
    for event in events:
        if event.event_type == EventType.PROTOCOL_ANALYSIS_COMPLETED:
            analysis_event_by_signal[str(event.payload.get("signal_id", ""))] = event

    for event in events:
        related_run = runs_by_trigger_event.get(event.id)
        level = classify_level(event)
        detail_lines = list(parts_by_run.get(related_run.id, [])) if related_run else []
        normal_evidence = normal_evidence_lines(event, baseline_fingerprints) if level == "normal" else []
        is_structured_signal = event.event_type == EventType.STRUCTURED_SIGNAL_DETECTED
        is_suspicious_signal = is_structured_signal and event.payload.get("classification") == "suspicious"
        signal_id = str(event.payload.get("signal_id", "")) if is_structured_signal else ""
        analysis_event = analysis_event_by_signal.get(signal_id) if signal_id else None
        if event.event_type == EventType.STRUCTURED_SIGNAL_DETECTED:
            signal_status_map[str(event.payload.get("signal_id", ""))] = determine_status(
                next((case for case in cases if case.signal_id == event.payload.get("signal_id")), None),
                related_run,
                analysis_event,
            )
        agent_name = related_run.agent_profile if related_run else ("place_detection_agent" if is_suspicious_signal else ("realtime" if is_structured_signal else "tool"))
        summary = structured_signal_timeline_summary(event, related_run, analysis_event) if is_structured_signal else event_summary(event)
        timeline.append(
            {
                "timestamp": event.recorded_at.isoformat(),
                "event_type": str(event.event_type),
                "event_type_label": to_cn_event_type(event.event_type),
                "agent": to_cn_agent_name(agent_name),
                "summary": summary,
                "level": level,
                "details": detail_lines,
                "normal_evidence": normal_evidence,
                "expandable": bool(normal_evidence),
            }
        )

    for item in list(collector.get("activity") or [])[-100:]:
        timeline.append(
            {
                "timestamp": str(item.get("timestamp") or utc_now_iso()),
                "event_type": str(item.get("event_type") or "collector.activity"),
                "event_type_label": str(item.get("event_type_label") or "USRP采集"),
                "agent": str(item.get("agent") or "采集控制"),
                "summary": str(item.get("summary") or ""),
                "level": str(item.get("level") or "info"),
                "details": list(item.get("details") or []),
                "normal_evidence": list(item.get("normal_evidence") or []),
                "expandable": bool(item.get("expandable")),
            }
        )
    timeline.sort(key=lambda item: str(item.get("timestamp") or ""))

    abnormal_insights_full = []
    for event in events:
        if event.event_type != EventType.STRUCTURED_SIGNAL_DETECTED:
            continue
        if str(event.payload.get("classification")) != "suspicious":
            continue
        signal_id = str(event.payload.get("signal_id", ""))
        case = next((item for item in cases if item.signal_id == signal_id), None)
        related_run = runs_by_trigger_event.get(event.id)
        analysis_event = analysis_event_by_signal.get(signal_id)
        abnormal_insights_full.append(
            {
                "title": f"{signal_id} 异常事件",
                "timestamp": event.recorded_at.isoformat(),
                "fingerprint": str(event.payload.get("fingerprint", "-")),
                "classification": to_cn_classification(str(event.payload.get("classification", "suspicious"))),
                "risk_level": to_cn_risk_level((analysis_event.payload.get("risk_level") if analysis_event else (case.risk_level if case else "medium"))),
                "status": determine_status(case, related_run, analysis_event),
                "summary": str(event.payload.get("summary", "") or event_summary(event)),
                "final_result": format_final_result(case, analysis_event),
                "steps": list(parts_by_run.get(related_run.id, [])) if related_run else [],
                "hypothesis": (
                    (analysis_event.payload.get("hypothesis") if analysis_event is not None else None)
                    or (case.hypothesis if case is not None else "")
                    or ""
                ),
                "analysis_score": analysis_event.payload.get("analysis_score") if analysis_event is not None else None,
            }
        )
    abnormal_insights = abnormal_insights_full[:5]
    ai_status_counts = {"queued": 0, "running": 0, "completed": 0}
    for item in abnormal_insights_full:
        status = str(item.get("status", ""))
        if status == "研判完成":
            ai_status_counts["completed"] += 1
        elif status == "研判中":
            ai_status_counts["running"] += 1
        else:
            ai_status_counts["queued"] += 1

    session_order = list(collector.get("session_order", []))
    sessions = dict(collector.get("sessions") or {})
    recent_sessions = [sessions[item] for item in reversed(session_order[-10:]) if item in sessions]
    active_session = collector.get("active_session") or (recent_sessions[0] if recent_sessions else None)
    latest_batch = state.metadata.get("latest_batch", {})
    total_received = len(observation_history)
    active_signal_count = len(state.active_signals)
    active_detection_job = getattr(platform, "_active_detection_job", None)
    debug_logger = getattr(app.runtime, "debug_logger", None)
    debug_lines = debug_logger.recent_lines(limit=200) if debug_logger and hasattr(debug_logger, "recent_lines") else []

    return {
        "task_id": task.id,
        "place_id": state.place_id,
        "streaming": bool(active_session and active_session.get("status") in {"pending", "starting", "running"}),
        "tick": total_received,
        "queues": {"detection": platform._detection_queue.qsize(), "chat": platform._chat_queue.qsize()},
        "ai_review": {
            "queued": platform._detection_queue.qsize(),
            "pending_history": ai_status_counts["queued"],
            "running": 1 if active_detection_job else 0,
            "running_history": ai_status_counts["running"],
            "completed": ai_status_counts["completed"],
            "total": len(abnormal_insights_full),
            "active_event_id": active_detection_job.get("event_id") if active_detection_job else None,
        },
        "collector": {
            "mode": collector.get("mode", "real_collector"),
            "active_session": active_session,
            "recent_sessions": recent_sessions,
            "pending_count": len(collector.get("pending_queue", [])),
            "uploads_root": collector.get("uploads_root"),
            "evidence_root": collector.get("evidence_root"),
            "devices": list(collector.get("devices") or []),
            "stream_info": collector.get("stream_info"),
            "last_scan": collector.get("last_scan"),
            "last_configure_result": collector.get("last_configure_result"),
            "last_configure_error": collector.get("last_configure_error"),
            "activity": list(collector.get("activity") or [])[-50:],
            "latest_spectrogram": latest_spectrogram,
        },
        "realtime": {
            "recent_spectrogram": latest_spectrogram,
            "recent_observations": recent_observations,
            "all_observations": observation_history,
            "total_received_signals": total_received,
            "latest_batch": latest_batch,
            "recent_evidence": list(state.metadata.get("signal_evidence", []))[-20:],
        },
        "state_overview": {
            "active_signal_count": active_signal_count,
            "open_case_count": len([item for item in cases if str(item.status) != "closed"]),
            "knowledge_baseline": baseline_fingerprints,
        },
        "timeline": timeline,
        "anomaly_insights": abnormal_insights,
        "all_anomaly_insights": abnormal_insights_full,
        "cases": [
            {
                "case_id": item.id,
                "signal_id": item.signal_id,
                "status": signal_status_map.get(item.signal_id, to_cn_case_status(str(item.status))),
                "risk_level": to_cn_risk_level(item.risk_level),
                "hypothesis": item.hypothesis,
                "notes": item.notes[-5:],
                "next_actions": item.next_actions,
            }
            for item in sorted(cases, key=lambda item: item.updated_at, reverse=True)
        ],
        "chat": [
            {
                "role": str(item.role),
                "content": item.content,
                "created_at": item.created_at.isoformat(),
                "agent": "对话问答智能体" if str(item.role) == "assistant" else "操作员",
            }
            for item in chats[-20:]
        ],
        "logs": debug_lines,
        "updated_at": utc_now_iso(),
    }
