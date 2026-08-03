from __future__ import annotations

import json
import sqlite3
from dataclasses import fields, is_dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from threading import RLock
from typing import Any

from deepem.protocol import (
    CaseRecord,
    CaseStatus,
    ChatMessage,
    ChatRole,
    Conversation,
    DEFAULT_CONVERSATION_TITLE,
    Event,
    EventType,
    EvidenceRef,
    Part,
    PartKind,
    Run,
    RunStatus,
    RunTriggerKind,
    SignalRecord,
    StateSnapshot,
    Task,
    TaskStatus,
    TaskType,
    ToolCall,
    ToolCallStatus,
    ToolResult,
    utc_now,
)


def _normalize(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {field.name: _normalize(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, dict):
        return {str(key): _normalize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    return value


def _dumps(value: Any) -> str:
    return json.dumps(_normalize(value), ensure_ascii=False, separators=(",", ":"))


def _loads(value: str) -> Any:
    return json.loads(value)


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


def _evidence_ref_from_dict(data: dict[str, Any]) -> EvidenceRef:
    return EvidenceRef(
        kind=str(data["kind"]),
        uri=str(data["uri"]),
        label=data.get("label"),
        metadata=dict(data.get("metadata") or {}),
    )


def _signal_record_from_dict(data: dict[str, Any]) -> SignalRecord:
    return SignalRecord(
        signal_id=str(data["signal_id"]),
        fingerprint=str(data["fingerprint"]),
        classification=str(data.get("classification", "observed")),
        carries_information=bool(data.get("carries_information", False)),
        suspected_device_type=data.get("suspected_device_type"),
        evidence_refs=[_evidence_ref_from_dict(item) for item in data.get("evidence_refs", [])],
        metadata=dict(data.get("metadata") or {}),
        conversation_id=data.get("conversation_id"),
    )


def _tool_result_from_dict(data: dict[str, Any] | None) -> ToolResult | None:
    if data is None:
        return None
    return ToolResult(
        status=str(data.get("status", "success")),
        data=dict(data.get("data") or {}),
        attachments=[_evidence_ref_from_dict(item) for item in data.get("attachments", [])],
        emitted_event_ids=[str(item) for item in data.get("emitted_event_ids", [])],
        error=data.get("error"),
        metadata=dict(data.get("metadata") or {}),
    )


def _task_from_dict(data: dict[str, Any]) -> Task:
    return Task(
        id=str(data["id"]),
        task_type=TaskType(data["task_type"]),
        target=dict(data.get("target") or {}),
        input=dict(data.get("input") or {}),
        status=TaskStatus(data["status"]),
        created_by=str(data["created_by"]),
        created_at=_parse_dt(data["created_at"]),
        updated_at=_parse_dt(data["updated_at"]),
    )


def _conversation_from_dict(data: dict[str, Any]) -> Conversation:
    return Conversation(
        id=str(data["id"]),
        task_id=str(data["task_id"]),
        created_at=_parse_dt(data["created_at"]),
        updated_at=_parse_dt(data["updated_at"]),
        title=str(data.get("title") or DEFAULT_CONVERSATION_TITLE),
        title_source=str(data.get("title_source") or "auto"),
    )


def _chat_message_from_dict(data: dict[str, Any]) -> ChatMessage:
    return ChatMessage(
        id=str(data["id"]),
        conversation_id=str(data["conversation_id"]),
        task_id=str(data["task_id"]),
        role=ChatRole(data["role"]),
        content=str(data.get("content", "")),
        run_id=data.get("run_id"),
        created_at=_parse_dt(data["created_at"]),
        attachments=[_evidence_ref_from_dict(item) for item in data.get("attachments", [])],
        metadata=dict(data.get("metadata") or {}),
    )


def _event_from_dict(data: dict[str, Any]) -> Event:
    return Event(
        id=str(data["id"]),
        task_id=str(data["task_id"]),
        seq=int(data["seq"]),
        event_type=EventType(data["event_type"]),
        source=str(data["source"]),
        payload=dict(data.get("payload") or {}),
        occurred_at=_parse_dt(data["occurred_at"]),
        recorded_at=_parse_dt(data["recorded_at"]),
        causation_event_id=data.get("causation_event_id"),
        causation_tool_call_id=data.get("causation_tool_call_id"),
        idempotency_key=data.get("idempotency_key"),
        evidence_refs=[_evidence_ref_from_dict(item) for item in data.get("evidence_refs", [])],
        conversation_id=data.get("conversation_id"),
    )


def _run_from_dict(data: dict[str, Any]) -> Run:
    return Run(
        id=str(data["id"]),
        task_id=str(data["task_id"]),
        trigger_kind=RunTriggerKind(data["trigger_kind"]),
        trigger_event_id=data.get("trigger_event_id"),
        trigger_message_id=data.get("trigger_message_id"),
        agent_profile=str(data["agent_profile"]),
        status=RunStatus(data["status"]),
        step_budget=int(data.get("step_budget", 0)),
        step_count=int(data.get("step_count", 0)),
        started_at=_parse_dt(data["started_at"]),
        ended_at=_parse_dt(data.get("ended_at")),
        stop_reason=data.get("stop_reason"),
        summary=data.get("summary"),
        conversation_id=data.get("conversation_id"),
    )


def _part_from_dict(data: dict[str, Any]) -> Part:
    return Part(
        id=str(data["id"]),
        task_id=str(data["task_id"]),
        run_id=str(data["run_id"]),
        kind=PartKind(data["kind"]),
        content=str(data.get("content", "")),
        created_at=_parse_dt(data["created_at"]),
        event_id=data.get("event_id"),
        message_id=data.get("message_id"),
        tool_call_id=data.get("tool_call_id"),
        metadata=dict(data.get("metadata") or {}),
        conversation_id=data.get("conversation_id"),
    )


def _tool_call_from_dict(data: dict[str, Any]) -> ToolCall:
    return ToolCall(
        id=str(data["id"]),
        task_id=str(data["task_id"]),
        run_id=str(data["run_id"]),
        tool_name=str(data["tool_name"]),
        input=dict(data.get("input") or {}),
        status=ToolCallStatus(data["status"]),
        started_at=_parse_dt(data.get("started_at")),
        ended_at=_parse_dt(data.get("ended_at")),
        result=_tool_result_from_dict(data.get("result")),
        conversation_id=data.get("conversation_id"),
    )


def _state_from_dict(data: dict[str, Any]) -> StateSnapshot:
    return StateSnapshot(
        task_id=str(data["task_id"]),
        place_id=str(data["place_id"]),
        active_signals={
            str(key): _signal_record_from_dict(value)
            for key, value in dict(data.get("active_signals") or {}).items()
        },
        metadata=dict(data.get("metadata") or {}),
        updated_at=_parse_dt(data.get("updated_at")) or utc_now(),
    )


def _case_from_dict(data: dict[str, Any]) -> CaseRecord:
    return CaseRecord(
        id=str(data["id"]),
        task_id=str(data["task_id"]),
        signal_id=str(data["signal_id"]),
        status=CaseStatus(data["status"]),
        risk_level=str(data.get("risk_level", "medium")),
        hypothesis=data.get("hypothesis"),
        next_actions=[str(item) for item in data.get("next_actions", [])],
        notes=[str(item) for item in data.get("notes", [])],
        evidence_refs=[_evidence_ref_from_dict(item) for item in data.get("evidence_refs", [])],
        updated_at=_parse_dt(data.get("updated_at")) or utc_now(),
        conversation_id=data.get("conversation_id"),
    )


class SQLiteStorage:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_db()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def clear_all(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                DELETE FROM tool_calls;
                DELETE FROM parts;
                DELETE FROM runs;
                DELETE FROM events;
                DELETE FROM chat_messages;
                DELETE FROM chat_sessions;
                DELETE FROM conversations;
                DELETE FROM cases;
                DELETE FROM states;
                DELETE FROM tasks;
                """
            )
            self._conn.commit()


    def delete_conversation_workspace(self, conversation_id: str) -> None:
        """Delete every persisted workspace artifact that belongs to one chat session.

        Artifacts created before this version may not have a top-level conversation_id.
        For those, we also remove rows linked to runs/tool calls that do have one.
        """
        with self._lock:
            run_rows = self._fetchall("SELECT id, data FROM runs", ())
            run_ids = []
            for row in run_rows:
                try:
                    data = _loads(row["data"])
                except Exception:
                    continue
                if data.get("conversation_id") == conversation_id:
                    run_ids.append(str(row["id"]))
            placeholders = ",".join("?" for _ in run_ids)
            if run_ids:
                self._conn.execute(f"DELETE FROM parts WHERE run_id IN ({placeholders})", tuple(run_ids))
                self._conn.execute(f"DELETE FROM tool_calls WHERE run_id IN ({placeholders})", tuple(run_ids))
                self._conn.execute(f"DELETE FROM runs WHERE id IN ({placeholders})", tuple(run_ids))

            for table in ("events", "parts", "tool_calls", "cases"):
                rows = self._fetchall(f"SELECT id, data FROM {table}", ())
                ids = []
                for row in rows:
                    try:
                        data = _loads(row["data"])
                    except Exception:
                        continue
                    if data.get("conversation_id") == conversation_id:
                        ids.append(str(row["id"]))
                if ids:
                    ph = ",".join("?" for _ in ids)
                    self._conn.execute(f"DELETE FROM {table} WHERE id IN ({ph})", tuple(ids))

            # Remove session-scoped active signals/state updates so a deleted chat
            # cannot leak them into the next prompt's workspace snapshot.
            state_rows = self._fetchall("SELECT task_id, data FROM states", ())
            for row in state_rows:
                try:
                    data = _loads(row["data"])
                except Exception:
                    continue
                active = dict(data.get("active_signals") or {})
                filtered = {
                    key: value
                    for key, value in active.items()
                    if not (isinstance(value, dict) and value.get("conversation_id") == conversation_id)
                }
                if filtered != active:
                    data["active_signals"] = filtered
                    self._conn.execute(
                        "UPDATE states SET updated_at = ?, data = ? WHERE task_id = ?",
                        (utc_now().isoformat(), _dumps(data), row["task_id"]),
                    )
            self._conn.commit()

    def _init_db(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS chat_sessions (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    title TEXT NOT NULL,
                    title_source TEXT NOT NULL,
                    data TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_chat_sessions_task_updated_at ON chat_sessions(task_id, updated_at, created_at);
                CREATE TABLE IF NOT EXISTS chat_messages (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    data TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_chat_messages_conversation_created_at ON chat_messages(conversation_id, created_at, id);
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    recorded_at TEXT NOT NULL,
                    idempotency_key TEXT,
                    data TEXT NOT NULL,
                    UNIQUE(task_id, seq)
                );
                CREATE INDEX IF NOT EXISTS idx_events_task_recorded_at ON events(task_id, recorded_at, seq);
                CREATE INDEX IF NOT EXISTS idx_events_task_idempotency ON events(task_id, idempotency_key);
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    data TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_runs_task_started_at ON runs(task_id, started_at);
                CREATE TABLE IF NOT EXISTS parts (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    data TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_parts_run_created_at ON parts(run_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_parts_task_created_at ON parts(task_id, created_at);
                CREATE TABLE IF NOT EXISTS tool_calls (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    started_at TEXT,
                    data TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_tool_calls_run_started_at ON tool_calls(run_id, started_at);
                CREATE TABLE IF NOT EXISTS states (
                    task_id TEXT PRIMARY KEY,
                    updated_at TEXT NOT NULL,
                    data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cases (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    signal_id TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    data TEXT NOT NULL,
                    UNIQUE(task_id, signal_id)
                );
                CREATE INDEX IF NOT EXISTS idx_cases_task_updated_at ON cases(task_id, updated_at);
                """
            )
            self._conn.commit()

    def _execute(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        cursor = self._conn.execute(sql, params)
        self._conn.commit()
        return cursor

    def _fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        return self._conn.execute(sql, params).fetchone()

    def _fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        return list(self._conn.execute(sql, params).fetchall())


class SQLiteTaskRepo:
    def __init__(self, storage: SQLiteStorage) -> None:
        self.storage = storage

    def create(self, task: Task) -> Task:
        with self.storage._lock:
            self.storage._execute(
                "INSERT OR REPLACE INTO tasks (id, created_at, updated_at, data) VALUES (?, ?, ?, ?)",
                (task.id, task.created_at.isoformat(), task.updated_at.isoformat(), _dumps(task)),
            )
            return task

    def get(self, task_id: str) -> Task:
        with self.storage._lock:
            row = self.storage._fetchone("SELECT data FROM tasks WHERE id = ?", (task_id,))
            if row is None:
                raise KeyError(task_id)
            return _task_from_dict(_loads(row["data"]))

    def save(self, task: Task) -> Task:
        with self.storage._lock:
            task.updated_at = utc_now()
            self.storage._execute(
                "UPDATE tasks SET created_at = ?, updated_at = ?, data = ? WHERE id = ?",
                (task.created_at.isoformat(), task.updated_at.isoformat(), _dumps(task), task.id),
            )
            return task

    def list_recent(self, limit: int | None = None) -> list[Task]:
        sql = "SELECT data FROM tasks ORDER BY updated_at DESC, created_at DESC"
        params: tuple[Any, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (limit,)
        with self.storage._lock:
            return [_task_from_dict(_loads(row["data"])) for row in self.storage._fetchall(sql, params)]


class SQLiteConversationRepo:
    def __init__(self, storage: SQLiteStorage) -> None:
        self.storage = storage

    def _upsert_chat_session_unlocked(self, conversation: Conversation) -> Conversation:
        self.storage._execute(
            "INSERT OR REPLACE INTO chat_sessions (id, task_id, created_at, updated_at, title, title_source, data) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                conversation.id,
                conversation.task_id,
                conversation.created_at.isoformat(),
                conversation.updated_at.isoformat(),
                conversation.title,
                conversation.title_source,
                _dumps(conversation),
            ),
        )
        return conversation

    def _bootstrap_legacy_for_task_unlocked(self, task_id: str) -> None:
        existing = self.storage._fetchone("SELECT id FROM chat_sessions WHERE task_id = ? LIMIT 1", (task_id,))
        if existing is not None:
            return
        legacy = self.storage._fetchone("SELECT data FROM conversations WHERE task_id = ? LIMIT 1", (task_id,))
        if legacy is None:
            return
        conversation = _conversation_from_dict(_loads(legacy["data"]))
        self._upsert_chat_session_unlocked(conversation)

    def _bootstrap_legacy_for_id_unlocked(self, conversation_id: str) -> None:
        existing = self.storage._fetchone("SELECT id FROM chat_sessions WHERE id = ? LIMIT 1", (conversation_id,))
        if existing is not None:
            return
        legacy = self.storage._fetchone("SELECT data FROM conversations WHERE id = ? LIMIT 1", (conversation_id,))
        if legacy is None:
            return
        conversation = _conversation_from_dict(_loads(legacy["data"]))
        self._upsert_chat_session_unlocked(conversation)

    def create(self, conversation: Conversation) -> Conversation:
        with self.storage._lock:
            return self._upsert_chat_session_unlocked(conversation)

    def get(self, conversation_id: str) -> Conversation:
        with self.storage._lock:
            self._bootstrap_legacy_for_id_unlocked(conversation_id)
            row = self.storage._fetchone("SELECT data FROM chat_sessions WHERE id = ?", (conversation_id,))
            if row is None:
                raise KeyError(conversation_id)
            return _conversation_from_dict(_loads(row["data"]))

    def get_by_task(self, task_id: str) -> Conversation:
        with self.storage._lock:
            self._bootstrap_legacy_for_task_unlocked(task_id)
            row = self.storage._fetchone(
                "SELECT data FROM chat_sessions WHERE task_id = ? ORDER BY updated_at DESC, created_at DESC, id DESC LIMIT 1",
                (task_id,),
            )
            if row is None:
                raise KeyError(task_id)
            return _conversation_from_dict(_loads(row["data"]))

    def list_by_task(self, task_id: str) -> list[Conversation]:
        with self.storage._lock:
            self._bootstrap_legacy_for_task_unlocked(task_id)
            rows = self.storage._fetchall(
                "SELECT data FROM chat_sessions WHERE task_id = ? ORDER BY updated_at DESC, created_at DESC, id DESC",
                (task_id,),
            )
            return [_conversation_from_dict(_loads(row["data"])) for row in rows]

    def save(self, conversation: Conversation) -> Conversation:
        with self.storage._lock:
            return self._upsert_chat_session_unlocked(conversation)

    def delete(self, conversation_id: str) -> None:
        with self.storage._lock:
            self.storage._execute("DELETE FROM chat_sessions WHERE id = ?", (conversation_id,))


class SQLiteChatMessageRepo:
    def __init__(self, storage: SQLiteStorage) -> None:
        self.storage = storage

    def append(self, message: ChatMessage) -> ChatMessage:
        with self.storage._lock:
            self.storage._execute(
                "INSERT OR REPLACE INTO chat_messages (id, conversation_id, task_id, created_at, data) VALUES (?, ?, ?, ?, ?)",
                (
                    message.id,
                    message.conversation_id,
                    message.task_id,
                    message.created_at.isoformat(),
                    _dumps(message),
                ),
            )
            return message

    def list_by_conversation(self, conversation_id: str, limit: int | None = None) -> list[ChatMessage]:
        sql = "SELECT data FROM chat_messages WHERE conversation_id = ? ORDER BY created_at ASC, id ASC"
        params: tuple[Any, ...] = (conversation_id,)
        if limit is not None:
            sql = (
                "SELECT data FROM (SELECT data, created_at, id FROM chat_messages "
                "WHERE conversation_id = ? ORDER BY created_at DESC, id DESC LIMIT ?) "
                "ORDER BY created_at ASC, id ASC"
            )
            params = (conversation_id, limit)
        with self.storage._lock:
            return [_chat_message_from_dict(_loads(row["data"])) for row in self.storage._fetchall(sql, params)]

    def delete_by_conversation(self, conversation_id: str) -> None:
        with self.storage._lock:
            self.storage._execute("DELETE FROM chat_messages WHERE conversation_id = ?", (conversation_id,))

    def delete_workspace_by_conversation(self, conversation_id: str) -> None:
        self.storage.delete_conversation_workspace(conversation_id)


class SQLiteEventRepo:
    def __init__(self, storage: SQLiteStorage) -> None:
        self.storage = storage

    def append(self, event: Event) -> Event:
        with self.storage._lock:
            next_seq = self._next_seq_unlocked(event.task_id)
            if event.seq < next_seq:
                event.seq = next_seq
            self.storage._execute(
                "INSERT OR REPLACE INTO events (id, task_id, seq, recorded_at, idempotency_key, data) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    event.id,
                    event.task_id,
                    event.seq,
                    event.recorded_at.isoformat(),
                    event.idempotency_key,
                    _dumps(event),
                ),
            )
            return event

    def get(self, event_id: str) -> Event:
        with self.storage._lock:
            row = self.storage._fetchone("SELECT data FROM events WHERE id = ?", (event_id,))
            if row is None:
                raise KeyError(event_id)
            return _event_from_dict(_loads(row["data"]))

    def next_seq(self, task_id: str) -> int:
        with self.storage._lock:
            return self._next_seq_unlocked(task_id)

    def _next_seq_unlocked(self, task_id: str) -> int:
        row = self.storage._fetchone("SELECT COALESCE(MAX(seq), 0) AS value FROM events WHERE task_id = ?", (task_id,))
        return int(row["value"]) + 1

    def list_by_task(self, task_id: str, limit: int | None = None) -> list[Event]:
        sql = "SELECT data FROM events WHERE task_id = ? ORDER BY seq ASC"
        params: tuple[Any, ...] = (task_id,)
        if limit is not None:
            sql = (
                "SELECT data FROM (SELECT data, seq FROM events WHERE task_id = ? ORDER BY seq DESC LIMIT ?) "
                "ORDER BY seq ASC"
            )
            params = (task_id, limit)
        with self.storage._lock:
            return [_event_from_dict(_loads(row["data"])) for row in self.storage._fetchall(sql, params)]

    def list_after(self, task_id: str, seq: int, limit: int | None = None) -> list[Event]:
        sql = "SELECT data FROM events WHERE task_id = ? AND seq > ? ORDER BY seq ASC"
        params: tuple[Any, ...] = (task_id, seq)
        if limit is not None:
            sql += " LIMIT ?"
            params = (task_id, seq, limit)
        with self.storage._lock:
            return [_event_from_dict(_loads(row["data"])) for row in self.storage._fetchall(sql, params)]

    def exists_by_idempotency_key(self, task_id: str, key: str) -> bool:
        with self.storage._lock:
            row = self.storage._fetchone(
                "SELECT 1 FROM events WHERE task_id = ? AND idempotency_key = ? LIMIT 1",
                (task_id, key),
            )
            return row is not None


class SQLiteRunRepo:
    def __init__(self, storage: SQLiteStorage) -> None:
        self.storage = storage

    def create(self, run: Run) -> Run:
        with self.storage._lock:
            self.storage._execute(
                "INSERT OR REPLACE INTO runs (id, task_id, started_at, data) VALUES (?, ?, ?, ?)",
                (run.id, run.task_id, run.started_at.isoformat(), _dumps(run)),
            )
            return run

    def get(self, run_id: str) -> Run:
        with self.storage._lock:
            row = self.storage._fetchone("SELECT data FROM runs WHERE id = ?", (run_id,))
            if row is None:
                raise KeyError(run_id)
            return _run_from_dict(_loads(row["data"]))

    def save(self, run: Run) -> Run:
        with self.storage._lock:
            self.storage._execute(
                "UPDATE runs SET task_id = ?, started_at = ?, data = ? WHERE id = ?",
                (run.task_id, run.started_at.isoformat(), _dumps(run), run.id),
            )
            return run

    def list_by_task(self, task_id: str, limit: int | None = None) -> list[Run]:
        sql = "SELECT data FROM runs WHERE task_id = ? ORDER BY started_at ASC, id ASC"
        params: tuple[Any, ...] = (task_id,)
        if limit is not None:
            sql = (
                "SELECT data FROM (SELECT data, started_at, id FROM runs WHERE task_id = ? "
                "ORDER BY started_at DESC, id DESC LIMIT ?) ORDER BY started_at ASC, id ASC"
            )
            params = (task_id, limit)
        with self.storage._lock:
            return [_run_from_dict(_loads(row["data"])) for row in self.storage._fetchall(sql, params)]


class SQLitePartRepo:
    def __init__(self, storage: SQLiteStorage) -> None:
        self.storage = storage

    def append(self, part: Part) -> Part:
        with self.storage._lock:
            self.storage._execute(
                "INSERT OR REPLACE INTO parts (id, task_id, run_id, created_at, data) VALUES (?, ?, ?, ?, ?)",
                (part.id, part.task_id, part.run_id, part.created_at.isoformat(), _dumps(part)),
            )
            return part

    def list_by_run(self, run_id: str) -> list[Part]:
        with self.storage._lock:
            return [
                _part_from_dict(_loads(row["data"]))
                for row in self.storage._fetchall(
                    "SELECT data FROM parts WHERE run_id = ? ORDER BY created_at ASC, id ASC", (run_id,)
                )
            ]

    def list_recent_by_task(self, task_id: str, limit: int | None = None) -> list[Part]:
        sql = "SELECT data FROM parts WHERE task_id = ? ORDER BY created_at ASC, id ASC"
        params: tuple[Any, ...] = (task_id,)
        if limit is not None:
            sql = (
                "SELECT data FROM (SELECT data, created_at, id FROM parts WHERE task_id = ? "
                "ORDER BY created_at DESC, id DESC LIMIT ?) ORDER BY created_at ASC, id ASC"
            )
            params = (task_id, limit)
        with self.storage._lock:
            return [_part_from_dict(_loads(row["data"])) for row in self.storage._fetchall(sql, params)]


class SQLiteToolCallRepo:
    def __init__(self, storage: SQLiteStorage) -> None:
        self.storage = storage

    def create(self, tool_call: ToolCall) -> ToolCall:
        with self.storage._lock:
            self.storage._execute(
                "INSERT OR REPLACE INTO tool_calls (id, task_id, run_id, started_at, data) VALUES (?, ?, ?, ?, ?)",
                (
                    tool_call.id,
                    tool_call.task_id,
                    tool_call.run_id,
                    tool_call.started_at.isoformat() if tool_call.started_at else None,
                    _dumps(tool_call),
                ),
            )
            return tool_call

    def get(self, tool_call_id: str) -> ToolCall:
        with self.storage._lock:
            row = self.storage._fetchone("SELECT data FROM tool_calls WHERE id = ?", (tool_call_id,))
            if row is None:
                raise KeyError(tool_call_id)
            return _tool_call_from_dict(_loads(row["data"]))

    def save(self, tool_call: ToolCall) -> ToolCall:
        with self.storage._lock:
            self.storage._execute(
                "UPDATE tool_calls SET task_id = ?, run_id = ?, started_at = ?, data = ? WHERE id = ?",
                (
                    tool_call.task_id,
                    tool_call.run_id,
                    tool_call.started_at.isoformat() if tool_call.started_at else None,
                    _dumps(tool_call),
                    tool_call.id,
                ),
            )
            return tool_call

    def list_by_run(self, run_id: str) -> list[ToolCall]:
        with self.storage._lock:
            return [
                _tool_call_from_dict(_loads(row["data"]))
                for row in self.storage._fetchall(
                    "SELECT data FROM tool_calls WHERE run_id = ? ORDER BY COALESCE(started_at, ''), id ASC",
                    (run_id,),
                )
            ]


class SQLiteStateRepo:
    def __init__(self, storage: SQLiteStorage) -> None:
        self.storage = storage

    def create(self, snapshot: StateSnapshot) -> StateSnapshot:
        with self.storage._lock:
            self.storage._execute(
                "INSERT OR REPLACE INTO states (task_id, updated_at, data) VALUES (?, ?, ?)",
                (snapshot.task_id, snapshot.updated_at.isoformat(), _dumps(snapshot)),
            )
            return snapshot

    def get(self, task_id: str) -> StateSnapshot:
        with self.storage._lock:
            row = self.storage._fetchone("SELECT data FROM states WHERE task_id = ?", (task_id,))
            if row is None:
                raise KeyError(task_id)
            return _state_from_dict(_loads(row["data"]))

    def save(self, snapshot: StateSnapshot) -> StateSnapshot:
        with self.storage._lock:
            snapshot.updated_at = utc_now()
            self.storage._execute(
                "UPDATE states SET updated_at = ?, data = ? WHERE task_id = ?",
                (snapshot.updated_at.isoformat(), _dumps(snapshot), snapshot.task_id),
            )
            return snapshot


class SQLiteCaseRepo:
    def __init__(self, storage: SQLiteStorage) -> None:
        self.storage = storage

    def get_by_signal(self, task_id: str, signal_id: str) -> CaseRecord | None:
        with self.storage._lock:
            row = self.storage._fetchone(
                "SELECT data FROM cases WHERE task_id = ? AND signal_id = ?",
                (task_id, signal_id),
            )
            if row is None:
                return None
            return _case_from_dict(_loads(row["data"]))

    def save(self, case: CaseRecord) -> CaseRecord:
        with self.storage._lock:
            case.updated_at = utc_now()
            self.storage._execute(
                "INSERT OR REPLACE INTO cases (id, task_id, signal_id, updated_at, data) VALUES (?, ?, ?, ?, ?)",
                (case.id, case.task_id, case.signal_id, case.updated_at.isoformat(), _dumps(case)),
            )
            return case

    def list_by_task(self, task_id: str) -> list[CaseRecord]:
        with self.storage._lock:
            return [
                _case_from_dict(_loads(row["data"]))
                for row in self.storage._fetchall(
                    "SELECT data FROM cases WHERE task_id = ? ORDER BY updated_at ASC, id ASC",
                    (task_id,),
                )
            ]
