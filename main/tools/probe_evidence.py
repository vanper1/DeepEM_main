from __future__ import annotations

from typing import Any

from deepem.devices.abstract import DeviceCommand
from deepem.protocol import ToolResult, utc_now
from deepem.probe_evidence_store import get_probe_evidence_store
from deepem.tools.base import ToolContext, ToolDefinition, ToolExecutionResult


def _collect_probe_evidence(args: dict[str, Any], context: ToolContext) -> ToolExecutionResult:
    adapter = context.device_registry.find_by_capability("collect_wifi_bluetooth_probe_evidence")
    if adapter is None:
        return ToolExecutionResult(result=ToolResult(status="error", error="WiFi/蓝牙探针适配器未注册"))

    def emit(stage: str, data: dict[str, Any]) -> None:
        if context.stream_handler is not None:
            context.stream_handler("probe_progress", {"stage": stage, **dict(data or {})})

    client = getattr(adapter, "client", None)
    try:
        if client is not None and callable(getattr(client, "collect_evidence", None)):
            result = client.collect_evidence(
                probe_ids=[str(item) for item in args.get("probe_ids", [])],
                kinds=[str(item) for item in args.get("kinds", ["wifi_ap", "wifi_client", "bluetooth"])],
                active_minutes=int(args.get("active_minutes", 30)),
                page_size=int(args.get("page_size", 200)),
                progress=emit,
            )
        else:
            device_result = adapter.execute(
                DeviceCommand(
                    device_id=adapter.device_id,
                    capability="collect_wifi_bluetooth_probe_evidence",
                    args=dict(args or {}),
                )
            )
            if device_result.status != "success":
                return ToolExecutionResult(result=ToolResult(status="error", error=device_result.error or "探针证据采集失败"))
            result = dict(device_result.data or {})
    except Exception as exc:
        return ToolExecutionResult(result=ToolResult(status="error", error=str(exc)))

    capture_task_id = str(args.get("capture_task_id") or context.task.id or "capture-task")
    try:
        stored = get_probe_evidence_store().ingest(dict(result or {}), capture_task_id=capture_task_id)
    except Exception as exc:
        return ToolExecutionResult(result=ToolResult(status="error", error=f"探针证据落库失败：{exc}"))

    state = context.state_repo.get(context.task.id)
    history = list(state.metadata.get("wifi_bluetooth_probe_evidence_history") or [])
    compact_state = {
        "capture_task_id": capture_task_id,
        "evidence_set_id": stored["evidence_set_id"],
        "updated_at": utc_now().isoformat(),
        "counts": dict(stored.get("counts") or {}),
        "overview": dict(stored.get("overview") or {}),
        "raw_result_sha256": stored.get("raw_sha256"),
        "indexed_records": stored.get("indexed_records", 0),
        "storage_note": "完整探针结果已落 JSON + SQLite 索引；任务上下文仅保存有界摘要，需要细节时使用 query_probe_evidence 按需检索。",
    }
    history.append(compact_state)
    state.metadata["wifi_bluetooth_probe_evidence"] = compact_state
    state.metadata["wifi_bluetooth_probe_evidence_history"] = history[-20:]
    context.state_repo.save(state)

    # 只把有界摘要返回给智能体；完整结果不再进入任务 JSON / LLM 上下文。
    public = {
        "evidence_set_id": stored["evidence_set_id"],
        "capture_task_id": capture_task_id,
        "created_at": stored.get("created_at"),
        "counts": stored.get("counts") or {},
        "overview": stored.get("overview") or {},
        "llm_evidence": stored.get("llm_evidence") or {},
        "manifest": stored.get("manifest") or {},
        "indexed_records": stored.get("indexed_records", 0),
        "retrieval": {
            "tool": "query_probe_evidence",
            "instruction": "当需要具体 SSID、MAC、蓝牙名称、厂商或某类记录时，传入 evidence_set_id 和关键词/kind/section 按需检索。",
        },
        "partial_failure": bool(result.get("partial_failure")),
        "errors": list(result.get("errors") or [])[:20],
    }
    return ToolExecutionResult(result=ToolResult(status="success", data=public))


def _query_probe_evidence(args: dict[str, Any], context: ToolContext) -> ToolExecutionResult:
    try:
        result = get_probe_evidence_store().query(
            evidence_set_id=str(args.get("evidence_set_id") or ""),
            query=str(args.get("query") or ""),
            kinds=[str(item) for item in args.get("kinds", [])],
            sections=[str(item) for item in args.get("sections", [])],
            limit=int(args.get("limit", 40)),
        )
    except (ValueError, KeyError) as exc:
        return ToolExecutionResult(result=ToolResult(status="error", error=str(exc)))
    except Exception as exc:
        return ToolExecutionResult(result=ToolResult(status="error", error=f"探针证据检索失败：{exc}"))
    return ToolExecutionResult(result=ToolResult(status="success", data=result))


def build_collect_probe_evidence_tool() -> ToolDefinition:
    return ToolDefinition(
        name="collect_wifi_bluetooth_probe_evidence",
        description=(
            "调用 WiFi/蓝牙探针采集热点、客户端和蓝牙设备证据。完整结果自动落 JSON + SQLite 索引，"
            "工具仅返回有界摘要，避免大规模设备列表挤爆智能体上下文。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "probe_ids": {"type": "array", "items": {"type": "string"}},
                "kinds": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["wifi_ap", "wifi_client", "bluetooth"]},
                },
                "active_minutes": {"type": "integer", "minimum": 1, "maximum": 1440},
                "page_size": {"type": "integer", "minimum": 1, "maximum": 200},
                "capture_task_id": {"type": "string"},
            },
            "required": ["probe_ids"],
        },
        handler=_collect_probe_evidence,
    )


def build_query_probe_evidence_tool() -> ToolDefinition:
    return ToolDefinition(
        name="query_probe_evidence",
        description="从已落库的 WiFi/蓝牙探针证据集中按关键词、设备类型、记录分区检索小批相关证据。",
        input_schema={
            "type": "object",
            "properties": {
                "evidence_set_id": {"type": "string"},
                "query": {"type": "string"},
                "kinds": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["wifi_ap", "wifi_client", "bluetooth"]},
                },
                "sections": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["devices", "targets", "observations"]},
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "required": ["evidence_set_id"],
        },
        handler=_query_probe_evidence,
    )
