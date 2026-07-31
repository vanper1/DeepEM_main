from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from deepem.nl2sql_config import NL2SQLSessionConfig
from deepem.protocol import (
    DEFAULT_CONVERSATION_TITLE,
    ChatMessage,
    ChatRole,
    Conversation,
    Event,
    EventType,
    EvidenceRef,
    StateSnapshot,
    Task,
    TaskStatus,
    TaskType,
    new_id,
    utc_now,
)
from deepem.runtime.engine import RunEngine
from deepem.runtime.chat_mode import ChatMode
from deepem.runtime.projector import EventProjector
from deepem.state.repositories import ChatMessageRepo, ConversationRepo, EventRepo, StateRepo, TaskRepo


@dataclass(slots=True)
class TaskService:
    task_repo: TaskRepo
    conversation_repo: ConversationRepo
    state_repo: StateRepo

    def create_place_detection_task(self, *, place_id: str, created_by: str = "operator") -> Task:
        now = utc_now()
        task = Task(
            id=new_id("task"),
            task_type=TaskType.PLACE_DETECTION,
            target={"place_id": place_id},
            input={"place_id": place_id},
            status=TaskStatus.RUNNING,
            created_by=created_by,
            created_at=now,
            updated_at=now,
        )
        self.task_repo.create(task)
        self.conversation_repo.create(Conversation(id=new_id("conv"), task_id=task.id, created_at=now, updated_at=now))
        self.state_repo.create(StateSnapshot(task_id=task.id, place_id=place_id))
        return task


@dataclass(slots=True)
class EventIngestService:
    event_repo: EventRepo
    projector: EventProjector

    def ingest(
        self,
        *,
        task_id: str,
        event_type: EventType,
        payload: dict[str, object],
        source: str,
        idempotency_key: str | None = None,
        evidence_refs: list[EvidenceRef] | None = None,
    ) -> Event:
        if idempotency_key and self.event_repo.exists_by_idempotency_key(task_id, idempotency_key):
            existing = next(
                item for item in reversed(self.event_repo.list_by_task(task_id)) if item.idempotency_key == idempotency_key
            )
            return existing
        event = Event(
            id=new_id("evt"),
            task_id=task_id,
            seq=self.event_repo.next_seq(task_id),
            event_type=event_type,
            source=source,
            payload=payload,
            occurred_at=utc_now(),
            recorded_at=utc_now(),
            idempotency_key=idempotency_key,
            evidence_refs=list(evidence_refs or []),
        )
        self.event_repo.append(event)
        self.projector.apply(event)
        return event


@dataclass(slots=True)
class ChatService:
    conversation_repo: ConversationRepo
    chat_repo: ChatMessageRepo
    run_engine: RunEngine

    def create_conversation(self, *, task_id: str, title: str | None = None) -> Conversation:
        now = utc_now()
        normalized = self._normalize_title(title)
        conversation = Conversation(
            id=new_id("conv"),
            task_id=task_id,
            created_at=now,
            updated_at=now,
            title=normalized or DEFAULT_CONVERSATION_TITLE,
            title_source="manual" if normalized else "auto",
        )
        self.conversation_repo.create(conversation)
        return conversation

    def rename_conversation(self, *, conversation_id: str, title: str) -> Conversation:
        conversation = self.conversation_repo.get(conversation_id)
        conversation.title = self._normalize_title(title) or DEFAULT_CONVERSATION_TITLE
        conversation.title_source = "manual"
        conversation.updated_at = utc_now()
        return self.conversation_repo.save(conversation)

    def delete_conversation(self, *, conversation_id: str) -> None:
        # Conversation deletion is a workspace boundary: remove the visible
        # messages and every persisted artifact produced by that session
        # (runs, parts, tool calls, events, cases, and session-scoped state).
        if hasattr(self.chat_repo, "delete_workspace_by_conversation"):
            self.chat_repo.delete_workspace_by_conversation(conversation_id)  # type: ignore[attr-defined]
        self.chat_repo.delete_by_conversation(conversation_id)
        self.conversation_repo.delete(conversation_id)

    def append_message(
        self,
        *,
        task_id: str,
        content: str,
        role: ChatRole = ChatRole.OPERATOR,
        conversation_id: str | None = None,
        run_id: str | None = None,
        attachments: list[EvidenceRef] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ChatMessage:
        conversation = self.conversation_repo.get(conversation_id) if conversation_id else self.conversation_repo.get_by_task(task_id)
        is_first_message = not self.chat_repo.list_by_conversation(conversation.id, limit=1)
        message = ChatMessage(
            id=new_id("msg"),
            conversation_id=conversation.id,
            task_id=task_id,
            role=role,
            content=content,
            run_id=run_id,
            created_at=utc_now(),
            attachments=list(attachments or []),
            metadata=dict(metadata or {}),
        )
        self.chat_repo.append(message)
        conversation.updated_at = message.created_at
        if role == ChatRole.OPERATOR and conversation.title_source != "manual" and (
            is_first_message or not conversation.title.strip() or conversation.title == DEFAULT_CONVERSATION_TITLE
        ):
            conversation.title = self._generate_title(content)
            conversation.title_source = "auto"
        self.conversation_repo.save(conversation)
        return message

    def process_message(
        self,
        *,
        task_id: str,
        message_id: str,
        conversation_id: str | None = None,
        event_handler: Callable[[str, dict[str, Any]], None] | None = None,
        nl2sql_options: NL2SQLSessionConfig | None = None,
        llm_options: Mapping[str, Any] | None = None,
        chat_mode: ChatMode | str = ChatMode.WORKSPACE,
        persist_assistant_message: bool = True,
        cancel_checker: Callable[[], bool] | None = None,
    ) -> ChatMessage | None:
        return self.run_engine.run_chat(
            task_id,
            message_id,
            conversation_id=conversation_id,
            event_handler=event_handler,
            nl2sql_options=nl2sql_options or NL2SQLSessionConfig(),
            llm_options=llm_options,
            chat_mode=chat_mode,
            persist_assistant_message=persist_assistant_message,
            cancel_checker=cancel_checker,
        )

    @staticmethod
    def _normalize_title(title: str | None) -> str:
        if title is None:
            return ""
        return " ".join(str(title).split()).strip()

    @classmethod
    def _generate_title(cls, content: str) -> str:
        normalized = cls._normalize_title(content)
        normalized = normalized.strip("，,。.!！?？：:;；、")
        if not normalized:
            return DEFAULT_CONVERSATION_TITLE
        if len(normalized) <= 24:
            return normalized
        return normalized[:24].rstrip() + "…"
