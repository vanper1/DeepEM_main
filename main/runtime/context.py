from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from threading import RLock
from typing import Any

from deepem.agent.llm import LLMClient
from deepem.agent.prompt_builder import PromptBuilder
from deepem.agent.profiles import AgentProfile
from deepem.devices.registry import DeviceRegistry
from deepem.runtime.projector import EventProjector
from deepem.state.repositories import (
    CaseRepo,
    ChatMessageRepo,
    ConversationRepo,
    EventRepo,
    KnowledgeBase,
    PartRepo,
    RunRepo,
    StateRepo,
    TaskRepo,
    ToolCallRepo,
)
from deepem.tools.registry import ToolRegistry


def _new_task_locks() -> dict[str, RLock]:
    return defaultdict(RLock)


@dataclass(slots=True)
class RuntimeContext:
    task_repo: TaskRepo
    conversation_repo: ConversationRepo
    chat_repo: ChatMessageRepo
    event_repo: EventRepo
    run_repo: RunRepo
    part_repo: PartRepo
    tool_call_repo: ToolCallRepo
    state_repo: StateRepo
    case_repo: CaseRepo
    knowledge_base: KnowledgeBase
    tool_registry: ToolRegistry
    device_registry: DeviceRegistry
    projector: EventProjector
    llm_client: LLMClient
    prompt_builder: PromptBuilder
    profiles: dict[str, AgentProfile]
    asset_manager: Any | None = None
    document_index: Any | None = None
    database_catalog: Any | None = None
    upload_processor: Any | None = None
    persistence: Any | None = None
    debug_logger: Any | None = None
    _task_locks: dict[str, RLock] = field(default_factory=_new_task_locks, repr=False)

    def task_lock(self, task_id: str) -> RLock:
        return self._task_locks[task_id]
