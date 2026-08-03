from __future__ import annotations

from collections import defaultdict
from threading import RLock

from deepem.protocol import (
    CaseRecord,
    ChatMessage,
    Conversation,
    Event,
    Part,
    Run,
    StateSnapshot,
    Task,
    ToolCall,
    utc_now,
)


class InMemoryTaskRepo:
    def __init__(self) -> None:
        self._items: dict[str, Task] = {}
        self._lock = RLock()

    def create(self, task: Task) -> Task:
        with self._lock:
            self._items[task.id] = task
            return task

    def get(self, task_id: str) -> Task:
        with self._lock:
            return self._items[task_id]

    def save(self, task: Task) -> Task:
        with self._lock:
            task.updated_at = utc_now()
            self._items[task.id] = task
            return task

    def list_recent(self, limit: int | None = None) -> list[Task]:
        with self._lock:
            items = sorted(self._items.values(), key=lambda item: item.updated_at, reverse=True)
            if limit is None:
                return list(items)
            return list(items[:limit])


class InMemoryConversationRepo:
    def __init__(self) -> None:
        self._items: dict[str, Conversation] = {}
        self._ids_by_task: dict[str, list[str]] = defaultdict(list)
        self._lock = RLock()

    def create(self, conversation: Conversation) -> Conversation:
        with self._lock:
            self._items[conversation.id] = conversation
            if conversation.id not in self._ids_by_task[conversation.task_id]:
                self._ids_by_task[conversation.task_id].append(conversation.id)
            return conversation

    def get(self, conversation_id: str) -> Conversation:
        with self._lock:
            return self._items[conversation_id]

    def get_by_task(self, task_id: str) -> Conversation:
        with self._lock:
            items = self.list_by_task(task_id)
            if not items:
                raise KeyError(task_id)
            return items[0]

    def list_by_task(self, task_id: str) -> list[Conversation]:
        with self._lock:
            items = [self._items[item_id] for item_id in self._ids_by_task[task_id] if item_id in self._items]
            return sorted(items, key=lambda item: (item.updated_at, item.created_at, item.id), reverse=True)

    def save(self, conversation: Conversation) -> Conversation:
        with self._lock:
            self._items[conversation.id] = conversation
            if conversation.id not in self._ids_by_task[conversation.task_id]:
                self._ids_by_task[conversation.task_id].append(conversation.id)
            return conversation

    def delete(self, conversation_id: str) -> None:
        with self._lock:
            conversation = self._items.pop(conversation_id)
            self._ids_by_task[conversation.task_id] = [item for item in self._ids_by_task[conversation.task_id] if item != conversation_id]


class InMemoryChatMessageRepo:
    def __init__(self) -> None:
        self._items_by_conversation: dict[str, list[ChatMessage]] = defaultdict(list)
        self._lock = RLock()

    def append(self, message: ChatMessage) -> ChatMessage:
        with self._lock:
            self._items_by_conversation[message.conversation_id].append(message)
            return message

    def list_by_conversation(self, conversation_id: str, limit: int | None = None) -> list[ChatMessage]:
        with self._lock:
            items = list(self._items_by_conversation[conversation_id])
            if limit is None:
                return items
            return items[-limit:]

    def delete_by_conversation(self, conversation_id: str) -> None:
        with self._lock:
            self._items_by_conversation.pop(conversation_id, None)


class InMemoryEventRepo:
    def __init__(self) -> None:
        self._events_by_task: dict[str, list[Event]] = defaultdict(list)
        self._events_by_id: dict[str, Event] = {}
        self._idempotency_keys: dict[str, set[str]] = defaultdict(set)
        self._lock = RLock()

    def append(self, event: Event) -> Event:
        with self._lock:
            expected_seq = len(self._events_by_task[event.task_id]) + 1
            if event.seq < expected_seq:
                event.seq = expected_seq
            self._events_by_task[event.task_id].append(event)
            self._events_by_id[event.id] = event
            if event.idempotency_key:
                self._idempotency_keys[event.task_id].add(event.idempotency_key)
            return event

    def get(self, event_id: str) -> Event:
        with self._lock:
            return self._events_by_id[event_id]

    def next_seq(self, task_id: str) -> int:
        with self._lock:
            return len(self._events_by_task[task_id]) + 1

    def list_by_task(self, task_id: str, limit: int | None = None) -> list[Event]:
        with self._lock:
            items = list(self._events_by_task[task_id])
            if limit is None:
                return items
            return items[-limit:]

    def list_after(self, task_id: str, seq: int, limit: int | None = None) -> list[Event]:
        with self._lock:
            items = [item for item in self._events_by_task[task_id] if item.seq > seq]
            if limit is None:
                return items
            return items[:limit]

    def exists_by_idempotency_key(self, task_id: str, key: str) -> bool:
        with self._lock:
            return key in self._idempotency_keys[task_id]


class InMemoryRunRepo:
    def __init__(self) -> None:
        self._items: dict[str, Run] = {}
        self._items_by_task: dict[str, list[str]] = defaultdict(list)
        self._lock = RLock()

    def create(self, run: Run) -> Run:
        with self._lock:
            self._items[run.id] = run
            self._items_by_task[run.task_id].append(run.id)
            return run

    def get(self, run_id: str) -> Run:
        with self._lock:
            return self._items[run_id]

    def save(self, run: Run) -> Run:
        with self._lock:
            self._items[run.id] = run
            return run

    def list_by_task(self, task_id: str, limit: int | None = None) -> list[Run]:
        with self._lock:
            items = [self._items[item_id] for item_id in self._items_by_task[task_id]]
            if limit is None:
                return items
            return items[-limit:]


class InMemoryPartRepo:
    def __init__(self) -> None:
        self._items: dict[str, Part] = {}
        self._items_by_run: dict[str, list[str]] = defaultdict(list)
        self._items_by_task: dict[str, list[str]] = defaultdict(list)
        self._lock = RLock()

    def append(self, part: Part) -> Part:
        with self._lock:
            self._items[part.id] = part
            self._items_by_run[part.run_id].append(part.id)
            self._items_by_task[part.task_id].append(part.id)
            return part

    def list_by_run(self, run_id: str) -> list[Part]:
        with self._lock:
            return [self._items[item_id] for item_id in self._items_by_run[run_id]]

    def list_recent_by_task(self, task_id: str, limit: int | None = None) -> list[Part]:
        with self._lock:
            items = [self._items[item_id] for item_id in self._items_by_task[task_id]]
            if limit is None:
                return items
            return items[-limit:]


class InMemoryToolCallRepo:
    def __init__(self) -> None:
        self._items: dict[str, ToolCall] = {}
        self._items_by_run: dict[str, list[str]] = defaultdict(list)
        self._lock = RLock()

    def create(self, tool_call: ToolCall) -> ToolCall:
        with self._lock:
            self._items[tool_call.id] = tool_call
            self._items_by_run[tool_call.run_id].append(tool_call.id)
            return tool_call

    def save(self, tool_call: ToolCall) -> ToolCall:
        with self._lock:
            self._items[tool_call.id] = tool_call
            return tool_call

    def get(self, tool_call_id: str) -> ToolCall:
        with self._lock:
            return self._items[tool_call_id]

    def list_by_run(self, run_id: str) -> list[ToolCall]:
        with self._lock:
            return [self._items[item_id] for item_id in self._items_by_run[run_id]]


class InMemoryStateRepo:
    def __init__(self) -> None:
        self._items: dict[str, StateSnapshot] = {}
        self._lock = RLock()

    def create(self, snapshot: StateSnapshot) -> StateSnapshot:
        with self._lock:
            self._items[snapshot.task_id] = snapshot
            return snapshot

    def get(self, task_id: str) -> StateSnapshot:
        with self._lock:
            return self._items[task_id]

    def save(self, snapshot: StateSnapshot) -> StateSnapshot:
        with self._lock:
            snapshot.updated_at = utc_now()
            self._items[snapshot.task_id] = snapshot
            return snapshot


class InMemoryCaseRepo:
    def __init__(self) -> None:
        self._items_by_task: dict[str, dict[str, CaseRecord]] = defaultdict(dict)
        self._lock = RLock()

    def get_by_signal(self, task_id: str, signal_id: str) -> CaseRecord | None:
        with self._lock:
            return self._items_by_task[task_id].get(signal_id)

    def save(self, case: CaseRecord) -> CaseRecord:
        with self._lock:
            case.updated_at = utc_now()
            self._items_by_task[case.task_id][case.signal_id] = case
            return case

    def list_by_task(self, task_id: str) -> list[CaseRecord]:
        with self._lock:
            return list(self._items_by_task[task_id].values())


class InMemoryKnowledgeBase:
    def __init__(
        self,
        *,
        place_baselines: dict[str, list[str]] | None = None,
        signal_knowledge: dict[str, dict[str, object]] | None = None,
    ) -> None:
        self._place_baselines = place_baselines or {}
        self._signal_knowledge = signal_knowledge or {}
        self._lock = RLock()

    def get_place_baseline(self, place_id: str) -> list[str]:
        with self._lock:
            return list(self._place_baselines.get(place_id, []))

    def set_place_baseline(self, place_id: str, fingerprints: list[str]) -> list[str]:
        with self._lock:
            normalized = _normalize_fingerprints(fingerprints)
            self._place_baselines[place_id] = normalized
            return list(normalized)

    def describe_place(self, place_id: str) -> dict[str, object]:
        baseline = self.get_place_baseline(place_id)
        return {
            "place_id": place_id,
            "baseline_fingerprints": baseline,
            "expected_signal_count": len(baseline),
        }

    def lookup_signal(self, fingerprint: str) -> dict[str, object] | None:
        with self._lock:
            item = self._signal_knowledge.get(fingerprint)
            if item is None:
                return None
            return dict(item)

    def search(self, query: str, place_id: str | None = None) -> dict[str, object]:
        with self._lock:
            matches = [
                {"fingerprint": key, "knowledge": value}
                for key, value in self._signal_knowledge.items()
                if query.lower() in key.lower()
            ]
        return {
            "query": query,
            "place_id": place_id,
            "matches": matches,
        }


def _normalize_fingerprints(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in values:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result
