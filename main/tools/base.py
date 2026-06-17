from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

LLMStreamHandler = Callable[[str, dict[str, Any]], None]
CancelChecker = Callable[[], bool]


from deepem.devices.registry import DeviceRegistry
from deepem.nl2sql_config import NL2SQLSessionConfig
from deepem.protocol import ChatMessage, Event, EventType, EvidenceRef, Run, Task, ToolResult, utc_now
from deepem.state.repositories import CaseRepo, KnowledgeBase, StateRepo


@dataclass(slots=True)
class EventDraft:
    event_type: EventType
    source: str
    payload: dict[str, Any]
    occurred_at: datetime = field(default_factory=utc_now)
    idempotency_key: str | None = None
    evidence_refs: list[EvidenceRef] = field(default_factory=list)


@dataclass(slots=True)
class ToolExecutionResult:
    result: ToolResult
    event_drafts: list[EventDraft] = field(default_factory=list)


@dataclass(slots=True)
class ToolContext:
    task: Task
    run: Run
    trigger_event: Event | None
    trigger_message: ChatMessage | None
    state_repo: StateRepo
    case_repo: CaseRepo
    knowledge_base: KnowledgeBase
    device_registry: DeviceRegistry
    llm_client: Any | None = None
    stream_handler: LLMStreamHandler | None = None
    nl2sql_options: NL2SQLSessionConfig = field(default_factory=NL2SQLSessionConfig)
    asset_manager: Any | None = None
    document_index: Any | None = None
    database_catalog: Any | None = None
    cancel_checker: CancelChecker | None = None


ToolHandler = Callable[[dict[str, Any], ToolContext], ToolExecutionResult]


@dataclass(slots=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler
