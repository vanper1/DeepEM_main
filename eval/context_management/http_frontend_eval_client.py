from __future__ import annotations

import json
import mimetypes
import time
import uuid
from pathlib import Path
from typing import Any, Iterable, Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


BOOTSTRAP_EVENTS = {"message", "stream_open"}
VISIBLE_TOKEN_EVENTS = {"reasoning_delta", "assistant_reasoning", "thought_delta", "reasoning", "thought", "final_answer_delta"}


class FrontendEvalHTTPError(RuntimeError):
    pass


def parse_sse_stream(chunks: Iterable[bytes]) -> Iterator[tuple[str, dict[str, Any]]]:
    buffer = ""
    for chunk in chunks:
        buffer += chunk.decode("utf-8", errors="replace")
        while True:
            split_at, separator_length = _frame_boundary(buffer)
            if split_at < 0:
                break
            frame = buffer[:split_at]
            buffer = buffer[split_at + separator_length :]
            parsed = _parse_sse_frame(frame)
            if parsed is not None:
                yield parsed
    parsed = _parse_sse_frame(buffer)
    if parsed is not None:
        yield parsed


class FrontendEvalClient:
    def __init__(self, base_url: str, *, timeout_seconds: float = 600.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def create_session(self, *, title: str) -> dict[str, Any]:
        payload = self._json_request("POST", "/api/chat/sessions", {"title": title})
        item = payload.get("item")
        if not isinstance(item, dict) or not item.get("id"):
            raise FrontendEvalHTTPError("create session response has no item.id")
        return item

    def detect_chat_mode_capability(self) -> bool:
        payload = self._json_request("GET", "/openapi.json")
        components = payload.get("components") if isinstance(payload.get("components"), dict) else {}
        schemas = components.get("schemas") if isinstance(components.get("schemas"), dict) else {}
        chat_schema = schemas.get("ChatIn") if isinstance(schemas.get("ChatIn"), dict) else {}
        properties = chat_schema.get("properties") if isinstance(chat_schema.get("properties"), dict) else {}
        return "chat_mode" in properties

    def upload_files(self, *, session_id: str, files: list[tuple[str, Path]]) -> list[dict[str, Any]]:
        if not files:
            return []
        boundary = f"----DeepEMEval{uuid.uuid4().hex}"
        body = _multipart_body(boundary=boundary, session_id=session_id, files=files)
        request = Request(
            self._url("/api/chat/uploads/stream"),
            data=body,
            method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}", "Accept": "application/x-ndjson"},
        )
        with self._open(request) as response:
            raw = response.read().decode("utf-8", errors="replace")
        items: list[dict[str, Any]] = []
        for line in raw.splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            if event.get("type") == "error":
                raise FrontendEvalHTTPError(str(event.get("message") or "upload failed"))
            if event.get("type") == "done":
                items = list(event.get("items") or [])
        if not items:
            raise FrontendEvalHTTPError("upload stream ended without a done event")
        return items

    def execute_turn(
        self,
        *,
        session_id: str,
        content: str,
        attachment_ids: list[str] | None = None,
        llm_options: dict[str, Any] | None = None,
        nl2sql_options: dict[str, Any] | None = None,
        chat_mode: str | None = None,
        send_chat_mode: bool = False,
    ) -> dict[str, Any]:
        payload = {
            "session_id": session_id,
            "content": content,
            "attachment_ids": list(attachment_ids or []),
            "nl2sql_options": dict(nl2sql_options or {}),
        }
        if llm_options is not None:
            payload["llm_options"] = dict(llm_options)
        if send_chat_mode:
            if chat_mode not in {"general", "workspace"}:
                raise ValueError(f"Invalid explicit chat_mode: {chat_mode!r}")
            payload["chat_mode"] = chat_mode
        started_wall = time.time()
        started = time.perf_counter()
        request = Request(
            self._url("/api/chat/sse"),
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
        )
        events: list[dict[str, Any]] = []
        answer_parts: list[str] = []
        reasoning_parts: list[str] = []
        timings: dict[str, float | None] = {
            "time_to_stream_open_ms": None,
            "time_to_first_activity_ms": None,
            "time_to_first_visible_token_ms": None,
            "time_to_final_answer_token_ms": None,
        }
        error: str | None = None
        done = False
        run_id: str | None = None
        observed_execution_policy: dict[str, Any] | None = None
        with self._open(request) as response:
            for event_name, data in parse_sse_stream(_response_chunks(response)):
                elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
                events.append({"name": event_name, "data": data, "elapsed_ms": elapsed_ms})
                if event_name == "stream_open" and timings["time_to_stream_open_ms"] is None:
                    timings["time_to_stream_open_ms"] = elapsed_ms
                if event_name not in BOOTSTRAP_EVENTS and timings["time_to_first_activity_ms"] is None:
                    timings["time_to_first_activity_ms"] = elapsed_ms
                text = str(data.get("delta") or data.get("text") or "")
                if event_name in VISIBLE_TOKEN_EVENTS and text and timings["time_to_first_visible_token_ms"] is None:
                    timings["time_to_first_visible_token_ms"] = elapsed_ms
                if event_name == "final_answer_delta" and text:
                    answer_parts.append(text)
                    if timings["time_to_final_answer_token_ms"] is None:
                        timings["time_to_final_answer_token_ms"] = elapsed_ms
                elif event_name in {"reasoning_delta", "assistant_reasoning", "thought_delta", "reasoning", "thought"}:
                    reasoning_parts.append(text)
                if data.get("run_id"):
                    run_id = str(data["run_id"])
                if event_name == "execution_policy":
                    observed_execution_policy = dict(data)
                if event_name == "error":
                    error = str(data.get("message") or "SSE error")
                if event_name == "done":
                    done = True
                    break
        elapsed_total = round((time.perf_counter() - started) * 1000, 3)
        if not done and error is None:
            error = "SSE stream ended without done"
        return {
            "http_request_started_at": started_wall,
            **timings,
            "turn_end_to_end_ms": elapsed_total,
            "events": events,
            "answer": "".join(answer_parts),
            "reasoning_chars": len("".join(reasoning_parts)),
            "run_id": run_id,
            "observed_execution_policy": observed_execution_policy,
            "error": error,
            "done": done,
        }

    def get_history(self, session_id: str) -> dict[str, Any]:
        return self._json_request("GET", f"/api/chat/sessions/{quote(session_id, safe='')}/history")

    def _json_request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(self._url(path), data=data, method=method, headers={"Content-Type": "application/json"})
        with self._open(request) as response:
            decoded = json.loads(response.read().decode("utf-8"))
        if not isinstance(decoded, dict):
            raise FrontendEvalHTTPError(f"{path} returned a non-object response")
        return decoded

    def _open(self, request: Request):
        try:
            return urlopen(request, timeout=self.timeout_seconds)
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise FrontendEvalHTTPError(f"HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise FrontendEvalHTTPError(f"connection error: {exc.reason}") from exc

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"


def _frame_boundary(buffer: str) -> tuple[int, int]:
    positions = [(buffer.find("\n\n"), 2), (buffer.find("\r\n\r\n"), 4)]
    available = [(index, length) for index, length in positions if index >= 0]
    return min(available, default=(-1, 0), key=lambda item: item[0])


def _response_chunks(response, size: int = 4096) -> Iterator[bytes]:
    read = getattr(response, "read1", response.read)
    while True:
        chunk = read(size)
        if not chunk:
            return
        yield chunk


def _parse_sse_frame(frame: str) -> tuple[str, dict[str, Any]] | None:
    event_name = "message"
    data_lines: list[str] = []
    for raw_line in frame.splitlines():
        line = raw_line.rstrip("\r")
        if not line or line.startswith(":"):
            continue
        if line.startswith("event:"):
            event_name = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].strip())
    if not data_lines:
        return None
    payload = json.loads("\n".join(data_lines))
    if not isinstance(payload, dict):
        payload = {"value": payload}
    return event_name, payload


def _multipart_body(*, boundary: str, session_id: str, files: list[tuple[str, Path]]) -> bytes:
    chunks = [
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"session_id\"\r\n\r\n{session_id}\r\n".encode()
    ]
    for file_name, path in files:
        content_type = mimetypes.guess_type(file_name)[0] or "application/octet-stream"
        chunks.append(
            (
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"files\"; filename=\"{file_name}\"\r\n"
                f"Content-Type: {content_type}\r\n\r\n"
            ).encode("utf-8")
        )
        chunks.append(path.read_bytes())
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks)
