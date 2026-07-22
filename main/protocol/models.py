from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import uuid4


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


DEFAULT_CONVERSATION_TITLE = "新对话"


class TaskType(StrEnum):
    PLACE_DETECTION = "place_detection"


class TaskStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    WAITING_FEEDBACK = "waiting_feedback"
    COMPLETED = "completed"
    FAILED = "failed"


class EventType(StrEnum):
    STRUCTURED_SIGNAL_DETECTED = "structured_signal.detected"
    COLLECTION_COMPLETED = "collection.completed"
    PROTOCOL_ANALYSIS_COMPLETED = "protocol_analysis.completed"
    OPERATOR_FEEDBACK_RECEIVED = "operator_feedback.received"


class RunTriggerKind(StrEnum):
    BOOTSTRAP = "bootstrap"
    EVENT = "event"
    CHAT = "chat"


class RunStatus(StrEnum):
    RUNNING = "running"
    WAITING_EVENT = "waiting_event"
    WAITING_FEEDBACK = "waiting_feedback"
    COMPLETED = "completed"
    FAILED = "failed"
    ABORTED = "aborted"


class PartKind(StrEnum):
    REASONING = "reasoning"
    TOOL_CALL = "tool_call"
    TEXT = "text"
    OBSERVATION = "observation"
    CASE_UPDATE = "case_update"


class ToolCallStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    ERROR = "error"


class ChatRole(StrEnum):
    OPERATOR = "operator"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class CaseStatus(StrEnum):
    OPEN = "open"
    INVESTIGATING = "investigating"
    CLOSED = "closed"


@dataclass(slots=True)
class EvidenceRef:
    kind: str
    uri: str
    label: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Task:
    id: str
    task_type: TaskType
    target: dict[str, Any]
    input: dict[str, Any]
    status: TaskStatus
    created_by: str
    created_at: datetime
    updated_at: datetime


@dataclass(slots=True)
class Conversation:
    id: str
    task_id: str
    created_at: datetime
    updated_at: datetime
    title: str = DEFAULT_CONVERSATION_TITLE
    title_source: str = "auto"


@dataclass(slots=True)
class ChatMessage:
    id: str
    conversation_id: str
    task_id: str
    role: ChatRole
    content: str
    run_id: str | None
    created_at: datetime
    attachments: list[EvidenceRef] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Event:
    id: str
    task_id: str
    seq: int
    event_type: EventType
    source: str
    payload: dict[str, Any]
    occurred_at: datetime
    recorded_at: datetime
    causation_event_id: str | None = None
    causation_tool_call_id: str | None = None
    idempotency_key: str | None = None
    evidence_refs: list[EvidenceRef] = field(default_factory=list)
    conversation_id: str | None = None


@dataclass(slots=True)
class Run:
    id: str
    task_id: str
    trigger_kind: RunTriggerKind
    trigger_event_id: str | None
    trigger_message_id: str | None
    agent_profile: str
    status: RunStatus
    step_budget: int
    step_count: int
    started_at: datetime
    ended_at: datetime | None = None
    stop_reason: str | None = None
    summary: str | None = None
    conversation_id: str | None = None


@dataclass(slots=True)
class ToolResult:
    status: str
    data: dict[str, Any] = field(default_factory=dict)
    attachments: list[EvidenceRef] = field(default_factory=list)
    emitted_event_ids: list[str] = field(default_factory=list)
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ToolCall:
    id: str
    task_id: str
    run_id: str
    tool_name: str
    input: dict[str, Any]
    status: ToolCallStatus
    started_at: datetime | None = None
    ended_at: datetime | None = None
    result: ToolResult | None = None
    conversation_id: str | None = None


@dataclass(slots=True)
class Part:
    id: str
    task_id: str
    run_id: str
    kind: PartKind
    content: str
    created_at: datetime
    event_id: str | None = None
    message_id: str | None = None
    tool_call_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    conversation_id: str | None = None


@dataclass(slots=True)
class SignalRecord:
    signal_id: str
    fingerprint: str
    classification: str
    carries_information: bool = False
    suspected_device_type: str | None = None
    evidence_refs: list[EvidenceRef] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    conversation_id: str | None = None


@dataclass(slots=True)
class StateSnapshot:
    task_id: str
    place_id: str
    active_signals: dict[str, SignalRecord] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    updated_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class CaseRecord:
    id: str
    task_id: str
    signal_id: str
    status: CaseStatus
    risk_level: str
    hypothesis: str | None = None
    next_actions: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    evidence_refs: list[EvidenceRef] = field(default_factory=list)
    updated_at: datetime = field(default_factory=utc_now)
    conversation_id: str | None = None
