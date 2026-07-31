from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import json
import queue
import threading
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from deepem.document_index import DocumentIndexUnavailable
from deepem.devices.usrp import UsrpApiError
from deepem.agent.tool_result_compressor import ToolResultCompressor

from .demo import DemoPlatform
from .nl2sql_config import NL2SQLSessionConfig
from .protocol import ChatRole, EvidenceRef, PartKind, RunStatus, ToolResult, new_id, utc_now
from .runtime.chat_mode import ChatMode
from .tools.sql_tool import SchemaIntrospector, find_preferred_nl2sql_database, resolve_nl2sql_db_path

platform = DemoPlatform()
app = FastAPI(title="DeepEM 中文演示服务")
WEB_DIR = Path(__file__).resolve().parent / "web"
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


@app.middleware("http")
async def no_cache_static_and_pages(request: Request, call_next):
    response = await call_next(request)
    if request.url.path in {"/", "/chat"} or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


class NL2SQLOptionsIn(BaseModel):
    force_enabled: bool = False
    auto_select_tables: bool = True
    manual_selected_tables: list[str] = Field(default_factory=list)
    database_id: str = ""
    db_path: str = ""


class LLMOptionsIn(BaseModel):
    temperature: float = Field(default=1.0, ge=0.0, le=2.0)
    top_p: float = Field(default=0.95, ge=0.0, le=1.0)
    top_k: int = Field(default=20, ge=1, le=200)
    min_p: float = Field(default=0.0, ge=0.0, le=1.0)
    presence_penalty: float = Field(default=1.5, ge=-2.0, le=2.0)
    repetition_penalty: float = Field(default=1.0, ge=0.01, le=2.0)
    enable_thinking: bool = True
    preserve_thinking: bool = False
    reasoning_effort: str = "medium"
    thinking_token_budget: int = Field(default=16384, ge=1, le=131072)


class ChatIn(BaseModel):
    session_id: str
    content: str = ""
    attachment_ids: list[str] = Field(default_factory=list)
    nl2sql_options: NL2SQLOptionsIn = Field(default_factory=NL2SQLOptionsIn)
    llm_options: LLMOptionsIn = Field(default_factory=LLMOptionsIn)
    chat_mode: ChatMode = ChatMode.WORKSPACE


class ChatSessionCreateIn(BaseModel):
    title: str = ""


class ChatSessionRenameIn(BaseModel):
    title: str


class ChatStopIn(BaseModel):
    session_id: str


class DatabaseRenameIn(BaseModel):
    display_name: str


class PlaceBaselineIn(BaseModel):
    place_id: str = ""
    fingerprints: list[str] = Field(default_factory=list)
    text: str = ""


class ReviewIn(BaseModel):
    signal_id: str
    expert_info: str
    tools: list[str]


@dataclass(slots=True)
class ActiveChatStream:
    session_id: str
    task_id: str
    operator_message_id: str
    chat_mode: ChatMode = ChatMode.WORKSPACE
    stop_event: threading.Event = field(default_factory=threading.Event)
    event_queue: queue.Queue[dict[str, Any]] = field(default_factory=queue.Queue)
    reply_full_text: str = ""
    partial_text: str = ""
    reasoning_streamed: bool = False
    answer_streamed: bool = False
    run_id: str | None = None
    cancelled: bool = False
    finalized: bool = False
    finalize_lock: threading.Lock = field(default_factory=threading.Lock)
    terminal_status: str | None = None
    error_message: str | None = None
    subscriber_closed: bool = False


_active_chat_streams: dict[str, ActiveChatStream] = {}
_active_chat_streams_lock = threading.RLock()


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")

@app.get("/home")
def home_page() -> FileResponse:
    return FileResponse(WEB_DIR / "home.html")


@app.get("/chat")
def chat_page() -> FileResponse:
    return FileResponse(WEB_DIR / "chat.html")

@app.get("/detect")
def detect_page() -> FileResponse:
    return FileResponse(WEB_DIR / "detect.html")


@app.get("/detect/new")
def detect_new_page() -> FileResponse:
    return FileResponse(WEB_DIR / "detect-new.html")


@app.get("/detect/verify")
def detect_verify_page() -> FileResponse:
    return FileResponse(WEB_DIR / "detect-verify.html")


@app.get("/detect/run")
def detect_run_page() -> FileResponse:
    return FileResponse(WEB_DIR / "judge.html")


@app.get("/judge")
def judge_page() -> FileResponse:
    return FileResponse(WEB_DIR / "judge.html")


@app.get("/templates")
def templates_page() -> FileResponse:
    return FileResponse(WEB_DIR / "templates.html")


@app.get("/templates/new")
def template_new_page() -> FileResponse:
    return FileResponse(WEB_DIR / "template-new.html")


@app.get("/devices")
def devices_page() -> FileResponse:
    return FileResponse(WEB_DIR / "devices.html")


@app.get("/devices/data")
def devices_data_page() -> FileResponse:
    return FileResponse(WEB_DIR / "devices-data.html")


@app.get("/algorithms")
def algorithms_page() -> FileResponse:
    return FileResponse(WEB_DIR / "algorithms.html")


@app.get("/algorithms/new")
def algorithm_new_page() -> FileResponse:
    return FileResponse(WEB_DIR / "algorithm-new.html")


@app.get("/data")
def data_page() -> FileResponse:
    return FileResponse(WEB_DIR / "data.html")


@app.get("/system")
def system_page() -> FileResponse:
    return FileResponse(WEB_DIR / "system.html")

@app.get("/db-manager")
def db_manager_page() -> FileResponse:
    return FileResponse(WEB_DIR / "db_manager.html")


@app.get("/api/snapshot")
def snapshot() -> JSONResponse:
    return JSONResponse(platform.snapshot())


@app.get("/api/workspace/place-baseline")
def get_place_baseline(place_id: str = Query(default="")) -> JSONResponse:
    state = _current_state()
    target_place_id = place_id.strip() or state.place_id
    return JSONResponse(_serialize_place_baseline(state=state, place_id=target_place_id))


@app.post("/api/workspace/place-baseline/initialize")
def initialize_place_baseline(payload: PlaceBaselineIn | None = None) -> JSONResponse:
    state = _current_state()
    target_place_id = (payload.place_id if payload else "").strip() or state.place_id
    source_items = _normalize_baseline_items(payload.fingerprints if payload else [], payload.text if payload else "")
    if not source_items:
        source_items = _workspace_baseline_candidates(state=state, place_id=target_place_id)
    return JSONResponse(_save_place_baseline(state=state, place_id=target_place_id, fingerprints=source_items, source="workspace_knowledge"))


@app.put("/api/workspace/place-baseline")
def update_place_baseline(payload: PlaceBaselineIn) -> JSONResponse:
    state = _current_state()
    target_place_id = payload.place_id.strip() or state.place_id
    items = _normalize_baseline_items(payload.fingerprints, payload.text)
    return JSONResponse(_save_place_baseline(state=state, place_id=target_place_id, fingerprints=items, source="manual_ui"))


@app.get("/api/chat/history")
def chat_history(session_id: str = Query(default="")) -> JSONResponse:
    conversation = _get_chat_session_or_default(session_id or None)
    return JSONResponse({"items": _serialize_history(conversation.id)})


@app.get("/api/assets/{asset_id}")
def get_asset(asset_id: str) -> FileResponse:
    asset_manager = _asset_manager()
    try:
        asset = asset_manager.get(asset_id)
        path = asset_manager.resolve_path(asset_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="asset not found")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="asset file missing")
    return FileResponse(path, media_type=asset.mime_type, filename=asset.file_name)


@app.get("/api/chat/sessions")
def list_chat_sessions() -> JSONResponse:
    conversations = _ensure_chat_sessions()
    items = [_serialize_conversation(item) for item in conversations]
    current_session_id = items[0]["id"] if items else None
    return JSONResponse({"items": items, "current_session_id": current_session_id})


@app.post("/api/chat/sessions")
def create_chat_session(payload: ChatSessionCreateIn) -> JSONResponse:
    conversation = platform.app.chat_service.create_conversation(task_id=platform.task.id, title=payload.title or "")
    return JSONResponse({"item": _serialize_conversation(conversation)})


@app.patch("/api/chat/sessions/{session_id}")
def rename_chat_session(session_id: str, payload: ChatSessionRenameIn) -> JSONResponse:
    conversation = _get_chat_session_or_404(session_id)
    if _get_active_chat_stream(session_id) is not None:
        raise HTTPException(status_code=409, detail="当前会话正在生成，暂时无法重命名")
    updated = platform.app.chat_service.rename_conversation(conversation_id=conversation.id, title=payload.title)
    return JSONResponse({"item": _serialize_conversation(updated)})


@app.delete("/api/chat/sessions/{session_id}")
def delete_chat_session(session_id: str) -> JSONResponse:
    _get_chat_session_or_404(session_id)
    if _get_active_chat_stream(session_id) is not None:
        raise HTTPException(status_code=409, detail="当前会话正在生成，暂时无法删除")
    platform.app.chat_service.delete_conversation(conversation_id=session_id)
    remaining = _list_chat_sessions()
    fallback = _serialize_conversation(remaining[0]) if remaining else None
    return JSONResponse({"deleted_id": session_id, "fallback_session": fallback})


@app.get("/api/chat/sessions/{session_id}/history")
def chat_session_history(session_id: str) -> JSONResponse:
    conversation = _get_chat_session_or_404(session_id)
    return JSONResponse({"session": _serialize_conversation(conversation), "items": _serialize_history(conversation.id)})


@app.get("/api/chat/sessions/{session_id}/generation-status")
def chat_generation_status(session_id: str) -> JSONResponse:
    conversation = _get_chat_session_or_404(session_id)
    active = _get_active_chat_stream(session_id)
    if active is not None:
        return JSONResponse(
            {
                "session_id": session_id,
                "status": active.terminal_status or ("cancelling" if active.stop_event.is_set() else "running"),
                "operator_message_id": active.operator_message_id,
                "run_id": active.run_id,
                "partial_text": active.partial_text,
                "assistant_message_id": None,
                "error": active.error_message,
            }
        )

    runs = [
        run
        for run in platform.app.runtime.run_repo.list_by_task(conversation.task_id)
        if run.conversation_id == conversation.id
    ]
    runs.sort(key=lambda run: run.ended_at or run.started_at, reverse=True)
    run = runs[0] if runs else None
    if run is None:
        status = "idle"
    elif run.status is RunStatus.COMPLETED:
        status = "completed"
    elif run.status is RunStatus.FAILED:
        status = "failed"
    elif run.status is RunStatus.ABORTED:
        status = "cancelled"
    else:
        status = "failed"

    assistant_message_id = None
    if run is not None and status == "completed":
        for message in reversed(platform.app.runtime.chat_repo.list_by_conversation(conversation.id)):
            if message.role == ChatRole.ASSISTANT and message.run_id == run.id:
                assistant_message_id = message.id
                break

    return JSONResponse(
        {
            "session_id": session_id,
            "status": status,
            "operator_message_id": run.trigger_message_id if run else None,
            "run_id": run.id if run else None,
            "partial_text": "",
            "assistant_message_id": assistant_message_id,
            "error": "生成失败，请重试" if status == "failed" else None,
        }
    )


@app.post("/api/chat/stop")
def stop_chat(payload: ChatStopIn) -> JSONResponse:
    stream = _get_active_chat_stream(payload.session_id)
    if stream is None:
        return JSONResponse({"status": "idle", "session_id": payload.session_id})
    stream.stop_event.set()
    stream.event_queue.put({"type": "stop_requested", "data": {"session_id": payload.session_id}})
    return JSONResponse({"status": "stopping", "session_id": payload.session_id})


@app.post("/api/chat/uploads")
async def chat_uploads(
    session_id: str = Form(default=""),
    files: list[UploadFile] = File(...),
) -> JSONResponse:
    if session_id:
        _get_chat_session_or_404(session_id)
    if not files:
        raise HTTPException(status_code=400, detail="未选择上传文件")

    upload_processor = _upload_processor()
    document_index = _document_index()
    items: list[dict[str, Any]] = []
    try:
        for file in files:
            filename = Path(file.filename or "uploaded.bin").name
            payload = await file.read()
            if not payload:
                raise HTTPException(status_code=400, detail=f"上传文件为空：{filename}")
            result = upload_processor.process_upload(file_name=filename, content=payload, conversation_id=session_id or None)
            response_item = result.to_client_payload(_asset_manager())
            try:
                document_index.index_documents(upload_processor.build_index_payloads(result))
            except DocumentIndexUnavailable as exc:
                if result.upload_kind != "image":
                    raise
                response_item["index_warning"] = str(exc)
            items.append(response_item)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"文件上传或索引失败：{exc}") from exc
    return JSONResponse({"items": items})


@app.post("/api/chat/uploads/stream")
async def chat_uploads_stream(
    session_id: str = Form(default=""),
    files: list[UploadFile] = File(...),
) -> StreamingResponse:
    if session_id:
        _get_chat_session_or_404(session_id)
    if not files:
        raise HTTPException(status_code=400, detail="未选择上传文件")

    uploaded_files: list[tuple[str, bytes]] = []
    for file in files:
        filename = Path(file.filename or "uploaded.bin").name
        payload = await file.read()
        if not payload:
            raise HTTPException(status_code=400, detail=f"上传文件为空：{filename}")
        uploaded_files.append((filename, payload))

    event_queue: queue.Queue[dict[str, Any] | None] = queue.Queue()

    def push_event(event: dict[str, Any]) -> None:
        event_queue.put(event)

    def worker() -> None:
        upload_processor = _upload_processor()
        document_index = _document_index()
        asset_manager = _asset_manager()
        items: list[dict[str, Any]] = []
        total_files = max(1, len(uploaded_files))
        try:
            for file_index, (filename, payload) in enumerate(uploaded_files):
                def progress_callback(progress: dict[str, Any], *, file_index: int = file_index, filename: str = filename) -> None:
                    file_percent = int(progress.get("percent") or 0)
                    overall = int(((file_index * 100) + file_percent) / total_files)
                    push_event(
                        {
                            "type": "progress",
                            "percent": max(0, min(100, overall)),
                            "file_percent": max(0, min(100, file_percent)),
                            "file_index": file_index + 1,
                            "file_count": total_files,
                            "file_name": progress.get("file_name") or filename,
                            "stage": progress.get("stage") or "processing",
                            "message": progress.get("message") or "正在解析文件",
                            **{k: v for k, v in progress.items() if k not in {"percent", "file_name", "stage", "message"}},
                        }
                    )

                result = upload_processor.process_upload(
                    file_name=filename,
                    content=payload,
                    conversation_id=session_id or None,
                    progress_callback=progress_callback,
                )
                response_item = result.to_client_payload(asset_manager)
                try:
                    document_index.index_documents(upload_processor.build_index_payloads(result))
                except DocumentIndexUnavailable as exc:
                    if result.upload_kind != "image":
                        raise
                    response_item["index_warning"] = str(exc)
                progress_callback({"percent": 100, "stage": "indexed", "message": "文档解析与索引完成", "file_name": filename})
                items.append(response_item)
            push_event({"type": "done", "percent": 100, "items": items})
        except Exception as exc:
            push_event({"type": "error", "message": f"文件上传或索引失败：{exc}"})
        finally:
            event_queue.put(None)

    def iter_upload_events():
        threading.Thread(target=worker, daemon=True).start()
        while True:
            event = event_queue.get()
            if event is None:
                break
            yield json.dumps(event, ensure_ascii=False, default=str) + "\n"

    return StreamingResponse(iter_upload_events(), media_type="application/x-ndjson")


@app.get("/api/nl2sql/databases")
def list_nl2sql_databases() -> JSONResponse:
    items: list[dict[str, Any]] = []
    catalog = _database_catalog()
    preferred = find_preferred_nl2sql_database(catalog)
    default_path = resolve_nl2sql_db_path(database_id="default", database_catalog=catalog)
    if default_path.exists():
        items.append(
            {
                "database_id": "default",
                "display_name": "默认数据库",
                "file_name": default_path.name,
                "db_path": str(default_path),
                "sqlite_file_name": default_path.name,
                "tables": SchemaIntrospector(default_path).list_table_names(),
                "is_default": True,
                "source_kind": "default",
                "source_suffix": default_path.suffix.lower(),
            }
        )
    for record in catalog.list_databases():
        items.append(_serialize_database_record(record))
    return JSONResponse({"items": items, "preferred_database_id": preferred.database_id if preferred is not None else ""})


@app.patch("/api/nl2sql/databases/{database_id}")
def rename_nl2sql_database(database_id: str, payload: DatabaseRenameIn) -> JSONResponse:
    if database_id == "default":
        raise HTTPException(status_code=400, detail="默认数据库不能重命名")
    try:
        record = _database_catalog().rename(database_id=database_id, display_name=payload.display_name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="database not found") from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({"item": _serialize_database_record(record)})


@app.get("/api/nl2sql/tables")
def nl2sql_tables(
    database_id: str = Query(default=""),
    db_path: str = Query(default=""),
) -> JSONResponse:
    resolved_path = resolve_nl2sql_db_path(
        db_path=db_path or None,
        database_id=database_id or None,
        database_catalog=_database_catalog(),
    )
    if not resolved_path.exists():
        raise HTTPException(status_code=404, detail=f"未找到 SQLite 数据库文件：{resolved_path}")
    tables = SchemaIntrospector(resolved_path).list_table_names()
    return JSONResponse({"database_id": database_id or "default", "db_path": str(resolved_path), "tables": tables})


@app.post("/api/nl2sql/upload-db")
async def upload_nl2sql_db(file: UploadFile = File(...)) -> JSONResponse:
    filename = Path(file.filename or "selected.db").name
    suffix = Path(filename).suffix.lower()
    if suffix not in {".db", ".sqlite", ".sqlite3", ".xls", ".xlsx", ".csv", ".tsv"}:
        raise HTTPException(status_code=400, detail="仅支持上传 .db / .sqlite / .sqlite3 / .xls / .xlsx / .csv / .tsv 文件")

    payload = await file.read()
    if not payload:
        raise HTTPException(status_code=400, detail="上传文件为空")

    try:
        record = _database_catalog().import_file(database_id=new_id("db"), file_name=filename, content=payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return JSONResponse(
        {
            "database_id": record.database_id,
            "db_path": record.db_path,
            "file_name": record.file_name,
            "display_name": record.display_name,
            "tables": record.table_names,
            "source_kind": record.source_kind,
            "source_suffix": record.source_suffix,
            "sqlite_file_name": Path(record.db_path).name,
        }
    )


@app.get("/api/nl2sql/databases/{database_id}/tables/{table_name}/data")
def nl2sql_table_data(
    database_id: str,
    table_name: str,
    limit: int = Query(default=200, ge=1, le=5000),
    offset: int = Query(default=0, ge=0),
) -> JSONResponse:
    import sqlite3

    resolved_path = resolve_nl2sql_db_path(
        database_id=database_id or None,
        database_catalog=_database_catalog(),
    )
    if not resolved_path.exists():
        raise HTTPException(status_code=404, detail=f"数据库文件未找到：{resolved_path}")

    with sqlite3.connect(resolved_path) as conn:
        conn.row_factory = sqlite3.Row
        table_names = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        ]
        if table_name not in table_names:
            raise HTTPException(status_code=404, detail=f"表 {table_name} 不存在")

        total = conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM {table_name} LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        if rows:
            columns = list(rows[0].keys())
        else:
            columns = [
                str(col[1])
                for col in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
            ]
        data = [[_sqlite_cell_value(cell) for cell in row] for row in rows]

    return JSONResponse(
        {
            "database_id": database_id,
            "table_name": table_name,
            "columns": columns,
            "rows": data,
            "total": total,
            "limit": limit,
            "offset": offset,
        }
    )


def _sqlite_cell_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bytes):
        return f"<BLOB {len(value)} bytes>"
    if isinstance(value, float):
        return value
    if isinstance(value, int):
        return value
    return str(value)


@app.post("/api/chat/sse")
def stream_chat(payload: ChatIn) -> StreamingResponse:
    content = payload.content.strip()
    session_id = payload.session_id.strip()
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is empty")
    if not content and not payload.attachment_ids:
        raise HTTPException(status_code=400, detail="content and attachment_ids are both empty")

    conversation = _get_chat_session_or_404(session_id)
    if _get_active_chat_stream(session_id) is not None:
        raise HTTPException(status_code=409, detail="当前会话正在生成，请先停止本轮输出")

    attachments = _build_attachment_refs(payload.attachment_ids)
    operator_message = platform.app.chat_service.append_message(
        task_id=platform.task.id,
        conversation_id=conversation.id,
        content=content,
        role=ChatRole.OPERATOR,
        attachments=attachments,
        metadata={"chat_mode": payload.chat_mode.value},
    )
    conversation = _get_chat_session_or_404(session_id)
    nl2sql_options = NL2SQLSessionConfig.from_payload(payload.nl2sql_options.model_dump(mode="python"))
    active = ActiveChatStream(
        session_id=session_id,
        task_id=platform.task.id,
        operator_message_id=operator_message.id,
        chat_mode=payload.chat_mode,
    )
    _register_active_chat_stream(active)

    def push_event(event_type: str, data: dict[str, Any]) -> None:
        payload = dict(data)
        if payload.get("run_id"):
            active.run_id = str(payload["run_id"])
        if event_type in {"reasoning", "reasoning_delta", "assistant_reasoning", "reasoning_start", "assistant_reasoning_start"}:
            active.reasoning_streamed = True
        if event_type == "final_answer_delta":
            active.answer_streamed = True
            active.partial_text += str(payload.get("delta") or "")
        if event_type == "final_answer_discard":
            active.partial_text = ""
            active.answer_streamed = False
        active.event_queue.put({"type": event_type, "data": payload})

    def worker() -> None:
        try:
            with platform.app.runtime.task_lock(platform.task.id):
                llm_opts = payload.llm_options.model_dump(mode="python")
                reply = platform.app.chat_service.process_message(
                    task_id=platform.task.id,
                    conversation_id=conversation.id,
                    message_id=operator_message.id,
                    event_handler=push_event,
                    nl2sql_options=nl2sql_options,
                    llm_options=llm_opts,
                    chat_mode=active.chat_mode,
                    persist_assistant_message=False,
                    cancel_checker=lambda: active.stop_event.is_set(),
                )
            if active.stop_event.is_set():
                active.cancelled = True
                _finalize_active_chat_stream(active, status="cancelled")
                active.event_queue.put({"type": "stop_requested", "data": {"session_id": session_id}})
                return
            active.reply_full_text = (reply.content if reply else "") or ""
            active.run_id = reply.run_id if reply else None
            _finalize_active_chat_stream(active, status="completed")
            if active.reply_full_text and not active.answer_streamed:
                active.event_queue.put({"type": "final_answer_start", "data": {"session_id": session_id, "run_id": active.run_id, "provisional": False}})
                active.event_queue.put({"type": "final_answer_delta", "data": {"session_id": session_id, "run_id": active.run_id, "delta": active.reply_full_text, "provisional": False}})
            active.event_queue.put({"type": "done", "data": {"message_id": operator_message.id, "session_id": session_id, "run_id": active.run_id, "cancelled": active.cancelled}})
        except Exception as exc:
            if active.stop_event.is_set():
                active.cancelled = True
                _finalize_active_chat_stream(active, status="cancelled")
                active.event_queue.put({"type": "stop_requested", "data": {"session_id": session_id}})
                return
            _finalize_active_chat_stream(active, status="failed", error_message="生成失败，请重试")
            active.event_queue.put({"type": "error", "data": {"message": "生成失败，请重试", "session_id": session_id, "run_id": active.run_id}})
        finally:
            _pop_active_chat_stream(session_id, active)

    threading.Thread(target=worker, name=f"chat-sse-{operator_message.id}", daemon=True).start()

    def event_stream():
        try:
            # Some browsers/proxies buffer very small SSE frames. A leading
            # comment padding forces the response body to flush before the LLM
            # emits its first token, so the UI can show progress immediately.
            yield _encode_sse_comment("stream-open " + (" " * 2048))
            yield _encode_sse(
                "message",
                {
                    "role": "operator",
                    "content": operator_message.content,
                    "message_id": operator_message.id,
                    "created_at": operator_message.created_at.isoformat(),
                    "session_id": session_id,
                    "session_title": conversation.title,
                    "attachments": [_serialize_attachment(item) for item in operator_message.attachments],
                },
            )
            yield _encode_sse(
                "stream_open",
                {
                    "message_id": operator_message.id,
                    "session_id": session_id,
                    "text": "流式连接已建立，正在等待模型输出。",
                },
            )
            while True:
                try:
                    item = active.event_queue.get(timeout=15)
                except queue.Empty:
                    if active.stop_event.is_set():
                        yield _encode_sse("done", {"message_id": operator_message.id, "session_id": session_id, "cancelled": True})
                        break
                    yield _encode_sse_comment("keep-alive")
                    continue

                event_type = item["type"]
                data = dict(item.get("data") or {})
                data.setdefault("session_id", session_id)

                if event_type == "stop_requested":
                    yield _encode_sse("done", {"message_id": operator_message.id, "session_id": session_id, "cancelled": True})
                    break

                yield _encode_sse(event_type, data)
                if event_type in {"done", "error"}:
                    if event_type == "error":
                        yield _encode_sse(
                            "done",
                            {"message_id": operator_message.id, "session_id": session_id, "run_id": active.run_id, "cancelled": active.cancelled},
                        )
                    break
        finally:
            active.subscriber_closed = True

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store, no-transform, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


class DeviceParamsIn(BaseModel):
    dev_id: str | None = None
    sample_rate: float | None = None
    bandwidth: float | None = None
    gain: float | None = None
    slice_duration: float | None = None
    duration: float | None = None
    freq: float | None = None
    channel: int | None = None
    antenna: str | None = None


@app.post("/api/control/start")
def control_start(payload: DeviceParamsIn | None = None) -> dict[str, object]:
    overrides = None
    if payload is not None:
        overrides = payload.model_dump(exclude_unset=True)
        if not overrides:
            overrides = None
    try:
        session = platform.start_stream(overrides=overrides)
    except UsrpApiError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {
        "status": session.get("status", "pending"),
        "session_id": session.get("session_id"),
        "device_id": session.get("device_id"),
        "usrp_task_id": session.get("usrp_task_id"),
        "usrp_stream": session.get("usrp_stream"),
    }


@app.post("/api/control/stop")
def control_stop() -> dict[str, object]:
    session = platform.stop_stream()
    return {"status": session.get("status", "idle"), "session_id": session.get("session_id")}


@app.post("/api/control/reset")
def control_reset() -> dict[str, str]:
    platform.reset()
    return {"status": "reset"}


@app.post("/api/devices/scan")
def devices_scan() -> JSONResponse:
    result = platform.scan_devices()
    return JSONResponse(result)


@app.get("/api/devices/status")
def devices_status() -> JSONResponse:
    return JSONResponse(platform.list_devices())


@app.get("/api/devices/stream-info")
def devices_stream_info(dev_id: str | None = None) -> JSONResponse:
    return JSONResponse(platform.get_usrp_stream_info(dev_id))


@app.get("/api/devices/params")
def get_device_params() -> JSONResponse:
    return JSONResponse(platform.get_device_params())


@app.post("/api/devices/params")
def set_device_params(payload: DeviceParamsIn) -> JSONResponse:
    cleaned = platform.set_device_params(payload.model_dump(exclude_unset=True))
    return JSONResponse(cleaned)


@app.post("/api/review/start")
def start_review(payload: ReviewIn) -> dict[str, str]:
    print(f"启动复核: signal_id={payload.signal_id}, expert_info={payload.expert_info}, tools={payload.tools}")
    return {"status": "review_started", "message": "复核已启动"}


@app.post("/api/start-capture")
def start_capture(request: Request):
    try:
        session = platform.start_stream()
    except UsrpApiError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {
        "ok": True,
        "session_id": session.get("session_id"),
        "status": session.get("status"),
        "message": "capture task created",
        "frontend_hint": "poll /api/snapshot",
        "client": request.client.host if request.client else None,
    }


@app.get("/api/collector/pending")
def collector_pending(collector_id: str):
    return platform.collector_pending(collector_id)


@app.post("/api/collector/upload")
async def collector_upload(
    session_id: str = Form(...),
    collector_id: str = Form(...),
    file: UploadFile = File(...),
):
    try:
        payload = platform.collector_upload(
            session_id=session_id,
            collector_id=collector_id,
            filename=file.filename,
            content=await file.read(),
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="session not found")
    return JSONResponse(payload)


@app.post("/api/collector/session-complete")
def collector_session_complete(payload: dict):
    try:
        return platform.collector_session_complete(payload)
    except KeyError:
        raise HTTPException(status_code=404, detail="session not found")


@app.get("/api/sessions")
def list_sessions():
    snapshot = platform.snapshot()
    items = []
    for session in snapshot.get("collector", {}).get("recent_sessions", []):
        items.append(
            {
                "session_id": session.get("session_id"),
                "status": session.get("status"),
                "created_at": session.get("created_at"),
                "started_at": session.get("started_at"),
                "completed_at": session.get("completed_at"),
                "collector_id": session.get("collector_id"),
                "files_received": session.get("files_received"),
                "alert_count": len(session.get("alerts", [])),
            }
        )
    return {"items": items}


@app.get("/api/sessions/{session_id}")
def get_session(session_id: str):
    snapshot = platform.snapshot()
    for session in snapshot.get("collector", {}).get("recent_sessions", []):
        if session.get("session_id") != session_id:
            continue
        latest_results = list((session.get("latest_results") or {}).values())
        latest_results.sort(key=lambda x: (x.get("scan_index", 0), x.get("channel", 0)))
        payload = {
            "session_id": session.get("session_id"),
            "status": session.get("status"),
            "created_at": session.get("created_at"),
            "started_at": session.get("started_at"),
            "completed_at": session.get("completed_at"),
            "collector_id": session.get("collector_id"),
            "files_received": session.get("files_received"),
            "alert_count": len(session.get("alerts", [])),
            "latest_results": latest_results,
            "recent_events": list(session.get("events", []))[-20:],
            "alerts": list(session.get("alerts", []))[-20:],
        }
        return JSONResponse(payload)
    raise HTTPException(status_code=404, detail="session not found")


def _register_active_chat_stream(stream: ActiveChatStream) -> None:
    with _active_chat_streams_lock:
        _active_chat_streams[stream.session_id] = stream


def _get_active_chat_stream(session_id: str) -> ActiveChatStream | None:
    with _active_chat_streams_lock:
        return _active_chat_streams.get(session_id)


def _pop_active_chat_stream(session_id: str, stream: ActiveChatStream) -> None:
    with _active_chat_streams_lock:
        current = _active_chat_streams.get(session_id)
        if current is stream:
            _active_chat_streams.pop(session_id, None)


def _finalize_active_chat_stream(
    stream: ActiveChatStream,
    *,
    status: str,
    error_message: str | None = None,
) -> bool:
    with stream.finalize_lock:
        if stream.terminal_status is not None or stream.finalized:
            return False
        stream.error_message = error_message
        content = stream.partial_text if status == "cancelled" else stream.reply_full_text if status == "completed" else ""
        if (content or "").strip():
            existing = []
            try:
                existing = platform.app.runtime.chat_repo.list_by_conversation(stream.session_id)
            except Exception:
                existing = []
            already_persisted = any(
                item.role == ChatRole.ASSISTANT and stream.run_id and item.run_id == stream.run_id
                for item in existing
            )
            if not already_persisted:
                append_kwargs = {
                    "task_id": stream.task_id,
                    "conversation_id": stream.session_id,
                    "content": content,
                    "role": ChatRole.ASSISTANT,
                    "run_id": stream.run_id,
                    "metadata": {
                        "stopped": status == "cancelled",
                        "partial": status == "cancelled",
                        "chat_mode": stream.chat_mode.value,
                    },
                }
                try:
                    platform.app.chat_service.append_message(**append_kwargs)
                except Exception:
                    existing_after_error = []
                    try:
                        existing_after_error = platform.app.runtime.chat_repo.list_by_conversation(stream.session_id)
                    except Exception:
                        existing_after_error = []
                    if not any(
                        item.role == ChatRole.ASSISTANT and stream.run_id and item.run_id == stream.run_id
                        for item in existing_after_error
                    ):
                        platform.app.chat_service.append_message(**append_kwargs)
        stream.finalized = True
        stream.terminal_status = status
        return True


def _list_chat_sessions() -> list[Any]:
    return list(platform.app.runtime.conversation_repo.list_by_task(platform.task.id))


def _ensure_chat_sessions() -> list[Any]:
    conversations = _list_chat_sessions()
    if conversations:
        return conversations
    platform.app.chat_service.create_conversation(task_id=platform.task.id)
    return _list_chat_sessions()


def _get_chat_session_or_default(session_id: str | None) -> Any:
    if session_id:
        return _get_chat_session_or_404(session_id)
    conversations = _ensure_chat_sessions()
    return conversations[0]


def _get_chat_session_or_404(session_id: str) -> Any:
    try:
        return platform.app.runtime.conversation_repo.get(session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="chat session not found")


def _serialize_history(conversation_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
    items = platform.app.runtime.chat_repo.list_by_conversation(conversation_id, limit=limit)
    return [
        {
            "id": item.id,
            "role": item.role,
            "content": item.content,
            "created_at": item.created_at.isoformat(),
            "run_id": item.run_id,
            "attachments": [_serialize_attachment(ref) for ref in item.attachments],
            "metadata": item.metadata,
            "run_steps": _serialize_run_steps(item.run_id) if item.role == ChatRole.ASSISTANT else [],
        }
        for item in items
    ]


def _serialize_run_steps(run_id: str | None) -> list[dict[str, Any]]:
    if not run_id:
        return []
    runtime = platform.app.runtime
    try:
        parts = runtime.part_repo.list_by_run(run_id)
        tool_calls = runtime.tool_call_repo.list_by_run(run_id)
    except KeyError:
        return []

    steps: list[dict[str, Any]] = []
    for part in parts:
        if part.kind == PartKind.TEXT:
            continue
        step_type = "reasoning" if part.kind == PartKind.REASONING else str(part.kind)
        steps.append(
            {
                "id": part.id,
                "type": step_type,
                "content": part.content,
                "text": part.content,
                "created_at": part.created_at.isoformat(),
                "tool_call_id": part.tool_call_id,
            }
        )

    for tool_call in tool_calls:
        started_at = tool_call.started_at.isoformat() if tool_call.started_at else ""
        ended_at = tool_call.ended_at.isoformat() if tool_call.ended_at else started_at
        steps.append(
            {
                "id": f"{tool_call.id}:call",
                "type": "tool_call",
                "tool_call_id": tool_call.id,
                "tool_name": tool_call.tool_name,
                "arguments": tool_call.input,
                "text": f"调用工具：{tool_call.tool_name} → 参数 {_compact_json(tool_call.input)}",
                "created_at": started_at,
                "status": str(tool_call.status),
            }
        )
        if tool_call.result is not None:
            result = tool_call.result
            compression = _tool_result_compression(tool_call.tool_name, tool_call.input, result, tool_call.id)
            steps.append(
                {
                    "id": f"{tool_call.id}:result",
                    "type": "tool_result",
                    "tool_call_id": tool_call.id,
                    "tool_name": tool_call.tool_name,
                    "status": result.status,
                    "data": result.data,
                    "error": result.error,
                    "attachments": [_serialize_attachment(ref) for ref in result.attachments],
                    "compression": compression,
                    "text": _tool_result_summary(tool_call.tool_name, result),
                    "created_at": ended_at,
                }
            )

    order = {"reasoning": 0, "tool_call": 1, "tool_result": 2}
    steps.sort(key=lambda item: (str(item.get("created_at") or ""), order.get(str(item.get("type")), 9), str(item.get("id") or "")))
    return steps


def _tool_result_summary(tool_name: str, result: ToolResult) -> str:
    if result.error:
        return f"工具返回：{tool_name} 执行失败，原因：{_compact_text(result.error, limit=180)}"
    preview = _compact_json(result.data, limit=220)
    return f"工具返回：{preview or (tool_name + ' 执行成功')}"


def _tool_result_compression(tool_name: str, arguments: dict[str, Any], result: ToolResult, tool_call_id: str) -> dict[str, Any] | None:
    try:
        compact = ToolResultCompressor().compress(
            tool_name=tool_name,
            arguments=arguments,
            tool_result=result,
            tool_call_id=tool_call_id,
        )
    except Exception:
        return None
    compression = compact.get("compression")
    return compression if isinstance(compression, dict) else None


def _current_state():
    return platform.app.runtime.state_repo.get(platform.task.id)


def _normalize_baseline_items(fingerprints: list[str] | None = None, text: str = "") -> list[str]:
    raw_items: list[str] = []
    raw_items.extend(str(item or "") for item in fingerprints or [])
    if text:
        raw_items.extend(item for item in str(text).replace("，", "\n").replace(",", "\n").splitlines())
    result: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        normalized = " ".join(str(item or "").strip().split())
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def _state_baseline_meta(state, *, place_id: str) -> dict[str, Any]:
    raw = state.metadata.get("place_baseline")
    if not isinstance(raw, dict):
        return {}
    if str(raw.get("place_id") or place_id) != place_id:
        return {}
    return raw


def _workspace_baseline_candidates(*, state, place_id: str) -> list[str]:
    candidates = _normalize_baseline_items(platform.app.runtime.knowledge_base.get_place_baseline(place_id))
    if candidates:
        return candidates
    normal_fingerprints = [
        signal.fingerprint
        for signal in state.active_signals.values()
        if signal.classification == "normal" and signal.fingerprint
    ]
    return _normalize_baseline_items(normal_fingerprints)


def _serialize_place_baseline(*, state, place_id: str) -> dict[str, Any]:
    meta = _state_baseline_meta(state, place_id=place_id)
    initialized = bool(meta.get("initialized"))
    saved_items = _normalize_baseline_items(meta.get("fingerprints") if initialized else [])
    candidates = _workspace_baseline_candidates(state=state, place_id=place_id)
    return {
        "place_id": place_id,
        "initialized": initialized,
        "fingerprints": saved_items,
        "candidate_fingerprints": candidates,
        "source": str(meta.get("source") or ("manual" if initialized else "workspace_knowledge")),
        "updated_at": str(meta.get("updated_at") or ""),
    }


def _save_place_baseline(*, state, place_id: str, fingerprints: list[str], source: str) -> dict[str, Any]:
    items = _normalize_baseline_items(fingerprints)
    state.metadata["place_baseline"] = {
        "place_id": place_id,
        "initialized": True,
        "fingerprints": items,
        "source": source,
        "updated_at": utc_now().isoformat(),
    }
    platform.app.runtime.state_repo.save(state)
    setter = getattr(platform.app.runtime.knowledge_base, "set_place_baseline", None)
    if callable(setter):
        setter(place_id, items)
    return _serialize_place_baseline(state=state, place_id=place_id)


def _serialize_database_record(record: Any) -> dict[str, Any]:
    return {
        "database_id": record.database_id,
        "display_name": record.display_name,
        "file_name": record.file_name,
        "db_path": record.db_path,
        "sqlite_file_name": Path(record.db_path).name,
        "tables": list(record.table_names),
        "is_default": False,
        "source_kind": record.source_kind,
        "source_suffix": record.source_suffix,
        "created_at": record.created_at,
        "metadata": dict(getattr(record, "metadata", {}) or {}),
    }


def _serialize_conversation(conversation) -> dict[str, Any]:
    messages = platform.app.runtime.chat_repo.list_by_conversation(conversation.id)
    latest = messages[-1] if messages else None
    preview = (latest.content if latest else "").strip()
    if len(preview) > 80:
        preview = preview[:80].rstrip() + "…"
    return {
        "id": conversation.id,
        "task_id": conversation.task_id,
        "title": conversation.title,
        "title_source": conversation.title_source,
        "created_at": conversation.created_at.isoformat(),
        "updated_at": conversation.updated_at.isoformat(),
        "message_count": len(messages),
        "last_message_preview": preview,
        "last_message_role": latest.role if latest else None,
        "is_generating": _get_active_chat_stream(conversation.id) is not None,
    }


def _encode_sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _encode_sse_comment(comment: str) -> str:
    return f": {comment}\n\n"


def _chunk_text(text: str) -> list[str]:
    normalized = text or ""
    if not normalized:
        return []
    return list(normalized)


def _compact_json(value: Any, *, limit: int = 180) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        text = str(value)
    return _compact_text(text, limit=limit)


def _compact_text(text: str, *, limit: int = 160) -> str:
    normalized = " ".join(str(text or "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[:limit].rstrip() + "…"


def _asset_manager():
    manager = getattr(platform.app.runtime, "asset_manager", None)
    if manager is None:
        raise HTTPException(status_code=500, detail="asset manager is not available")
    return manager


def _database_catalog():
    catalog = getattr(platform.app.runtime, "database_catalog", None)
    if catalog is None:
        raise HTTPException(status_code=500, detail="database catalog is not available")
    return catalog


def _document_index():
    index = getattr(platform.app.runtime, "document_index", None)
    if index is None:
        raise HTTPException(status_code=500, detail="document index is not available")
    return index


def _upload_processor():
    processor = getattr(platform.app.runtime, "upload_processor", None)
    if processor is None:
        raise HTTPException(status_code=500, detail="upload processor is not available")
    return processor


def _build_attachment_refs(asset_ids: list[str]) -> list[EvidenceRef]:
    manager = _asset_manager()
    refs: list[EvidenceRef] = []
    for asset_id in asset_ids:
        if not str(asset_id or "").strip():
            continue
        try:
            asset = manager.get(str(asset_id))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"asset not found: {asset_id}") from exc
        refs.append(
            EvidenceRef(
                kind="upload_asset",
                uri=manager.public_url(asset.asset_id),
                label=asset.file_name,
                metadata={
                    "asset_id": asset.asset_id,
                    "upload_kind": asset.upload_kind,
                    "mime_type": asset.mime_type,
                    "database_id": asset.metadata.get("database_id"),
                    "preview_asset_id": asset.metadata.get("preview_asset_id"),
                },
            )
        )
    return refs


def _serialize_attachment(item: EvidenceRef) -> dict[str, Any]:
    metadata = dict(item.metadata or {})
    preview_asset_id = metadata.get("preview_asset_id")
    return {
        "kind": item.kind,
        "uri": item.uri,
        "label": item.label,
        "asset_id": metadata.get("asset_id"),
        "upload_kind": metadata.get("upload_kind"),
        "mime_type": metadata.get("mime_type"),
        "database_id": metadata.get("database_id"),
        "preview_url": _asset_manager().public_url(str(preview_asset_id)) if preview_asset_id else None,
        "metadata": metadata,
    }
