from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from deepem.agent.config import LLMSettings
from deepem.agent.llm import LLMClient, LocalWorkflowLLMClient, OpenAICompatibleLLMClient
from deepem.agent.profiles import CAPTURE_AGENT, PLACE_DETECTION_AGENT, TASK_CHAT_AGENT
from deepem.agent.prompt_builder import PromptBuilder
from deepem.assets import AssetManager
from deepem.control.services import ChatService, EventIngestService, TaskService
from deepem.database_catalog import StructuredDatabaseCatalog
from deepem.devices import DeviceRegistry, UsrpDeviceAdapter
from deepem.document_index import ElasticsearchDocumentIndex, InMemoryDocumentIndex
from deepem.protocol import ChatMessage, ChatRole, Event, EventType, EvidenceRef, Task
from deepem.runtime.context import RuntimeContext
from deepem.runtime.debug_trace import DebugTraceLogger
from deepem.runtime.engine import RunEngine
from deepem.runtime.projector import EventProjector
from deepem.state.memory import InMemoryKnowledgeBase
from deepem.state.sqlite import (
    SQLiteCaseRepo,
    SQLiteChatMessageRepo,
    SQLiteConversationRepo,
    SQLiteEventRepo,
    SQLitePartRepo,
    SQLiteRunRepo,
    SQLiteStateRepo,
    SQLiteStorage,
    SQLiteTaskRepo,
    SQLiteToolCallRepo,
)
from deepem.tools import ToolRegistry, register_builtin_tools
from deepem.upload_processing import UploadProcessor


DEFAULT_DB_FILENAME = "deepem_demo.sqlite3"


@dataclass(slots=True)
class DeepEMApp:
    task_service: TaskService
    event_service: EventIngestService
    chat_service: ChatService
    runtime: RuntimeContext
    run_engine: RunEngine

    def close(self) -> None:
        persistence = getattr(self.runtime, "persistence", None)
        if persistence is not None and hasattr(persistence, "close"):
            persistence.close()

    def create_task(self, *, place_id: str, created_by: str = "operator") -> Task:
        return self.task_service.create_place_detection_task(place_id=place_id, created_by=created_by)

    def ingest_structured_signal(
        self,
        *,
        task_id: str,
        signal_id: str,
        batch_id: str,
        fingerprint: str,
        classification: str = "observed",
        carries_information: bool = False,
        suspected_device_type: str | None = None,
        source: str = "realtime",
        evidence_refs: list[EvidenceRef] | None = None,
        features: dict[str, object] | None = None,
        raw_sample_count: int | None = None,
    ) -> Event:
        return self.event_service.ingest(
            task_id=task_id,
            event_type=EventType.STRUCTURED_SIGNAL_DETECTED,
            payload={
                "signal_id": signal_id,
                "batch_id": batch_id,
                "fingerprint": fingerprint,
                "classification": classification,
                "carries_information": carries_information,
                "suspected_device_type": suspected_device_type,
                "features": features or {},
                "raw_sample_count": raw_sample_count,
                "summary": f"{'异常' if classification == 'suspicious' else '正常'}信号 {signal_id}（指纹：{fingerprint}）",
            },
            source=source,
            idempotency_key=f"signal:{batch_id}:{signal_id}:{classification}",
            evidence_refs=evidence_refs,
        )

    def append_chat(self, *, task_id: str, content: str, role: ChatRole = ChatRole.OPERATOR) -> ChatMessage:
        return self.chat_service.append_message(task_id=task_id, content=content, role=role)


def _default_db_path() -> Path:
    configured = os.getenv("DEEPEM_DB_PATH")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path.cwd() / DEFAULT_DB_FILENAME


def build_app(
    *,
    llm_client: LLMClient,
    place_baselines: dict[str, list[str]] | None = None,
    signal_knowledge: dict[str, dict[str, object]] | None = None,
    db_path: str | Path | None = None,
    asset_manager: AssetManager | None = None,
    document_index: Any | None = None,
    database_catalog: StructuredDatabaseCatalog | None = None,
    upload_processor: UploadProcessor | None = None,
) -> DeepEMApp:
    storage = SQLiteStorage(db_path or _default_db_path())
    uploads_root = Path(__file__).resolve().parent / "data" / "runtime_assets"
    asset_manager = asset_manager or AssetManager(uploads_root / "chat_assets")
    document_index = document_index or _build_document_index_from_env()
    database_catalog = database_catalog or StructuredDatabaseCatalog(uploads_root / "nl2sql_dbs")
    upload_processor = upload_processor or UploadProcessor(asset_manager=asset_manager, document_index=document_index, llm_client=llm_client)
    task_repo = SQLiteTaskRepo(storage)
    conversation_repo = SQLiteConversationRepo(storage)
    chat_repo = SQLiteChatMessageRepo(storage)
    event_repo = SQLiteEventRepo(storage)
    run_repo = SQLiteRunRepo(storage)
    part_repo = SQLitePartRepo(storage)
    tool_call_repo = SQLiteToolCallRepo(storage)
    state_repo = SQLiteStateRepo(storage)
    case_repo = SQLiteCaseRepo(storage)
    knowledge_base = InMemoryKnowledgeBase(place_baselines=place_baselines, signal_knowledge=signal_knowledge)
    device_registry = DeviceRegistry()
    device_registry.register(UsrpDeviceAdapter())
    tool_registry = register_builtin_tools(ToolRegistry())
    projector = EventProjector(state_repo=state_repo, case_repo=case_repo)
    runtime = RuntimeContext(
        task_repo=task_repo,
        conversation_repo=conversation_repo,
        chat_repo=chat_repo,
        event_repo=event_repo,
        run_repo=run_repo,
        part_repo=part_repo,
        tool_call_repo=tool_call_repo,
        state_repo=state_repo,
        case_repo=case_repo,
        knowledge_base=knowledge_base,
        tool_registry=tool_registry,
        device_registry=device_registry,
        projector=projector,
        llm_client=llm_client,
        prompt_builder=PromptBuilder(),
        profiles={PLACE_DETECTION_AGENT.name: PLACE_DETECTION_AGENT, TASK_CHAT_AGENT.name: TASK_CHAT_AGENT, CAPTURE_AGENT.name: CAPTURE_AGENT},
        asset_manager=asset_manager,
        document_index=document_index,
        database_catalog=database_catalog,
        upload_processor=upload_processor,
        persistence=storage,
        debug_logger=DebugTraceLogger(),
    )
    run_engine = RunEngine(runtime)
    return DeepEMApp(
        task_service=TaskService(task_repo=task_repo, conversation_repo=conversation_repo, state_repo=state_repo),
        event_service=EventIngestService(event_repo=event_repo, projector=projector),
        chat_service=ChatService(conversation_repo=conversation_repo, chat_repo=chat_repo, run_engine=run_engine),
        runtime=runtime,
        run_engine=run_engine,
    )


def _resolve_llm_client(prefix: str = "DEEPEM_LLM_") -> LLMClient:
    try:
        settings = LLMSettings.from_env(prefix=prefix)
        return OpenAICompatibleLLMClient(settings)
    except Exception:
        return LocalWorkflowLLMClient()


def _build_document_index_from_env() -> Any:
    url = (os.getenv("DEEPEM_ES_URL") or "http://127.0.0.1:9200").strip()
    index_name = (os.getenv("DEEPEM_ES_INDEX") or "deepem_knowledge").strip()
    username = (os.getenv("DEEPEM_ES_USERNAME") or "").strip() or None
    password = (os.getenv("DEEPEM_ES_PASSWORD") or "").strip() or None
    if os.getenv("DEEPEM_USE_MEMORY_DOCUMENT_INDEX") == "1":
        return InMemoryDocumentIndex()
    return ElasticsearchDocumentIndex(url=url, index_name=index_name, username=username, password=password)


def build_app_from_env(
    *,
    place_baselines: dict[str, list[str]] | None = None,
    signal_knowledge: dict[str, dict[str, object]] | None = None,
    prefix: str = "DEEPEM_LLM_",
    db_path: str | Path | None = None,
) -> DeepEMApp:
    return build_app(
        llm_client=_resolve_llm_client(prefix=prefix),
        place_baselines=place_baselines,
        signal_knowledge=signal_knowledge,
        db_path=db_path,
    )
