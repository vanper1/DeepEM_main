from __future__ import annotations

import json
import os
from collections import deque
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class DebugTraceLogger:
    """Best-effort JSONL tracer for agent execution.

    Logging failures must never affect the product workflow.
    """

    def __init__(self, log_dir: str | Path | None = None) -> None:
        configured = os.getenv("DEEPEM_DEBUG_LOG_DIR")
        target = Path(log_dir or configured or (Path.cwd() / "deepem_debug_logs")).expanduser().resolve()
        target.mkdir(parents=True, exist_ok=True)
        self.log_dir = target
        self._lock = RLock()
        self._recent_lines: deque[str] = deque(maxlen=500)

    def log(self, *, run_id: str, stage: str, payload: dict[str, Any]) -> None:
        normalized = self._normalize_record(stage=stage, payload=payload)
        if normalized is None:
            return
        record = {
            "logged_at": _utc_now_iso(),
            "stage": normalized["stage"],
            "payload": self._serialize(normalized["payload"]),
        }
        line = json.dumps(record, ensure_ascii=False)
        with self._lock:
            run_path = self.log_dir / f"{run_id}.jsonl"
            with run_path.open("a", encoding="utf-8") as fh:
                fh.write(line)
                fh.write("\n")
            index_path = self.log_dir / "runs.index.jsonl"
            with index_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"logged_at": record["logged_at"], "run_id": run_id, "stage": record["stage"]}, ensure_ascii=False))
                fh.write("\n")
            self._recent_lines.append(self._format_line(record))

    def safe_log(self, *, run_id: str, stage: str, payload: dict[str, Any]) -> None:
        try:
            self.log(run_id=run_id, stage=stage, payload=payload)
        except Exception:
            return

    def recent_lines(self, *, limit: int = 200) -> list[str]:
        with self._lock:
            if limit <= 0:
                return []
            return list(self._recent_lines)[-limit:]

    def _serialize(self, value: Any) -> Any:
        if is_dataclass(value):
            return self._serialize(asdict(value))
        if isinstance(value, dict):
            return {str(key): self._serialize(item) for key, item in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [self._serialize(item) for item in value]
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, bytes):
            return {"type": "bytes", "size": len(value)}
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        if hasattr(value, "model_dump"):
            try:
                return self._serialize(value.model_dump(mode="json"))
            except Exception:
                pass
        if hasattr(value, "__dict__"):
            try:
                return self._serialize(vars(value))
            except Exception:
                pass
        return repr(value)

    def _normalize_record(self, *, stage: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        if stage == "prompt_built":
            return {
                "stage": "llm_prompt",
                "payload": {
                    "messages": payload.get("assembled_messages") or payload.get("base_messages") or [],
                },
            }
        if stage == "llm_response":
            return {
                "stage": "llm_output",
                "payload": {
                    "step_index": payload.get("step_index"),
                    "reasoning": payload.get("reasoning"),
                    "content": payload.get("content"),
                    "tool_calls": payload.get("tool_calls") or [],
                },
            }
        if stage == "tool_call_started":
            return {
                "stage": "tool_input",
                "payload": {
                    "invocation_id": payload.get("invocation_id"),
                    "tool_name": payload.get("tool_name") or _extract_tool_name(payload.get("tool_call")),
                    "arguments": payload.get("arguments") or _extract_tool_input(payload.get("tool_call")),
                },
            }
        if stage == "tool_call_outcome":
            return {
                "stage": "tool_output",
                "payload": {
                    "invocation_id": payload.get("invocation_id"),
                    "tool_name": payload.get("tool_name"),
                    "status": _extract_result_field(payload.get("result"), "status"),
                    "data": _extract_result_field(payload.get("result"), "data"),
                    "error": _extract_result_field(payload.get("result"), "error"),
                },
            }
        if stage == "tool_call_exception":
            return {
                "stage": "tool_output",
                "payload": {
                    "invocation_id": payload.get("invocation_id"),
                    "tool_name": payload.get("tool_name"),
                    "status": "error",
                    "data": None,
                    "error": payload.get("error"),
                },
            }
        return None

    def _format_line(self, record: dict[str, Any]) -> str:
        payload = json.dumps(record.get("payload") or {}, ensure_ascii=False)
        return f"[{record.get('logged_at')}] {record.get('stage')}: {payload}"


def _extract_tool_name(tool_call: Any) -> Any:
    if tool_call is None:
        return None
    return getattr(tool_call, "tool_name", None) or getattr(tool_call, "name", None)


def _extract_tool_input(tool_call: Any) -> Any:
    if tool_call is None:
        return None
    return getattr(tool_call, "input", None) or getattr(tool_call, "arguments", None)


def _extract_result_field(result: Any, field_name: str) -> Any:
    if result is None:
        return None
    if isinstance(result, dict):
        return result.get(field_name)
    return getattr(result, field_name, None)
