from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timezone
from typing import Any, Callable

from deepem.protocol import ToolResult, utc_now


Strategy = Callable[[Mapping[str, Any], ToolResult, str], dict[str, Any]]


@dataclass(slots=True)
class CompressionOptions:
    max_output_chars: int = 8000
    default_snippet_chars: int = 240
    top_rows: int = 3
    top_items: int = 3
    max_depth: int = 4


@dataclass(slots=True)
class ToolTranscriptCompressionOptions:
    keep_recent_tool_messages: int = 2
    snippet_chars: int = 160


class ToolResultCompressor:
    def __init__(self, options: CompressionOptions | None = None) -> None:
        self.options = options or CompressionOptions()
        self._strategies: dict[str, Strategy] = {
            "query_local_database": self._compress_nl2sql,
            "query_uploaded_documents": self._compress_documents,
            "run_autonomous_usrp_task": self._compress_usrp_code_result,
            "generate_usrp_task_code": self._compress_usrp_code_result,
            "execute_usrp_task_code": self._compress_usrp_code_result,
            "retrieve_tool_result_detail": self._compress_tool_result_detail,
        }
        self._omitted_fields: list[dict[str, str]] = []

    def compress(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, Any],
        tool_result: ToolResult,
        tool_call_id: str,
        preserve_retrieved_detail: bool = False,
    ) -> dict[str, Any]:
        return self.compress_raw_data(
            tool_name=tool_name,
            arguments=arguments,
            status=tool_result.status,
            data=tool_result.data,
            error=tool_result.error,
            emitted_event_ids=tool_result.emitted_event_ids,
            tool_call_id=tool_call_id,
            preserve_retrieved_detail=preserve_retrieved_detail,
        )

    def compress_raw_data(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, Any],
        status: str,
        data: Any,
        error: str | None,
        emitted_event_ids: list[str],
        tool_call_id: str,
        preserve_retrieved_detail: bool = False,
    ) -> dict[str, Any]:
        self._omitted_fields = []
        raw_payload = {
            "status": status,
            "data": data,
            "error": error,
            "emitted_event_ids": emitted_event_ids,
        }
        if isinstance(data, Mapping):
            tool_result = ToolResult(status=status, data=dict(data), error=error)
            if preserve_retrieved_detail and tool_name == "retrieve_tool_result_detail":
                compact_data = self._preserve_tool_result_detail(arguments, tool_result, "data")
            else:
                strategy = self._strategies.get(tool_name, self._compress_generic_mapping)
                compact_data = strategy(arguments, tool_result, "data")
        else:
            compact_data = {
                "unsupported_data_type": type(data).__name__,
                "data_preview": self._clip_scalar(data, path="data", limit=self.options.default_snippet_chars),
            }
            self._omit("data", f"unsupported data type {type(data).__name__}; stored preview only")

        payload: dict[str, Any] = {
            "tool_result_id": tool_call_id,
            "tool_name": tool_name,
            "raw_result_sha256": self._hash_payload(raw_payload),
            "compressed_at": utc_now().astimezone(timezone.utc).isoformat(),
            "raw_result_stored": True,
            "can_retrieve_more": bool(self._omitted_fields),
            "compressed": True,
            "status": status,
            "data": compact_data,
            "error": error,
            "emitted_event_ids": emitted_event_ids,
            "truncated": bool(self._omitted_fields),
            "omitted_fields": list(self._omitted_fields),
        }
        if preserve_retrieved_detail and tool_name == "retrieve_tool_result_detail":
            found = bool(data.get("found")) if isinstance(data, Mapping) else False
            truncated = bool(data.get("truncated")) if isinstance(data, Mapping) else False
            payload["terminal"] = found and not truncated
            payload["can_retrieve_more"] = truncated
            payload["truncated"] = truncated
            payload["omitted_fields"] = []
            if payload["terminal"]:
                payload["retrieval_hint"] = "详情已完整返回，请使用现有内容回答，不要重复回捞同一路径。"
            elif found and truncated:
                payload["retrieval_hint"] = "详情被截断；如需更多内容，可使用更大的 max_chars 再次回捞。"
            else:
                payload["retrieval_hint"] = "未找到请求的详情；请检查 tool_result_id 或 path，不要使用相同参数重复调用。"
            return self._with_compression_metrics(payload, raw_payload, enforce_max_output=False)
        return self._with_compression_metrics(self._fit_payload(payload), raw_payload)

    def _preserve_tool_result_detail(
        self, arguments: Mapping[str, Any], tool_result: ToolResult, path: str
    ) -> dict[str, Any]:
        data = tool_result.data
        value = data.get("value", "")
        if value is None:
            value = ""
        return {
            "tool_result_id": data.get("tool_result_id") or arguments.get("tool_result_id"),
            "tool_name": data.get("tool_name") or "retrieve_tool_result_detail",
            "path": data.get("path") or arguments.get("path"),
            "found": bool(data.get("found")),
            "value_type": data.get("value_type"),
            "value": str(value),
            "truncated": bool(data.get("truncated")),
            "original_chars": data.get("original_chars"),
            "returned_chars": data.get("returned_chars"),
            "max_chars": data.get("max_chars"),
            "error": data.get("error") or tool_result.error,
        }

    def _compress_nl2sql(self, arguments: Mapping[str, Any], tool_result: ToolResult, path: str) -> dict[str, Any]:
        data = tool_result.data
        rows = data.get("rows")
        top_rows = self._compact_list(rows, f"{path}.rows", limit=self.options.top_rows, depth=1)

        for field in ("rows", "schema_overview", "attempts", "mschema"):
            if field in data and field != "rows":
                self._omit(f"{path}.{field}", "omitted noisy NL2SQL field")

        return self._drop_empty(
            {
                "question": data.get("question") or arguments.get("question"),
                "sql": data.get("sql"),
                "executed_sql": data.get("executed_sql"),
                "columns": data.get("columns"),
                "row_count": data.get("row_count", len(rows) if isinstance(rows, list) else None),
                "top_rows": top_rows,
                "summary": data.get("summary"),
                "truncated": bool(data.get("truncated")) or bool(self._omitted_fields),
            }
        )

    def _compress_documents(self, arguments: Mapping[str, Any], tool_result: ToolResult, path: str) -> dict[str, Any]:
        data = tool_result.data
        raw_results = data.get("results")
        compact_results: list[dict[str, Any]] = []
        if isinstance(raw_results, list):
            for index, item in enumerate(raw_results[: self.options.top_items]):
                if not isinstance(item, Mapping):
                    continue
                chunk_text = str(item.get("chunk_text") or "")
                if chunk_text:
                    self._omit(f"{path}.results[{index}].chunk_text", f"stored {self.options.default_snippet_chars}-char snippet")
                if item.get("preview_markdown"):
                    self._omit(f"{path}.results[{index}].preview_markdown", "preview markdown omitted from model context")
                compact_results.append(
                    self._drop_empty(
                        {
                            "asset_id": item.get("asset_id"),
                            "file_name": item.get("file_name"),
                            "chunk_index": item.get("chunk_index"),
                            "page": item.get("page"),
                            "sheet": item.get("sheet"),
                            "score": item.get("score"),
                            "snippet": self._clip_text(chunk_text, self.options.default_snippet_chars, f"{path}.results[{index}].snippet"),
                        }
                    )
                )
            if len(raw_results) > self.options.top_items:
                self._omit(f"{path}.results", f"kept first {self.options.top_items} results out of {len(raw_results)}")

        return self._drop_empty(
            {
                "query": data.get("query") or arguments.get("query"),
                "file_id": data.get("file_id") or arguments.get("file_id"),
                "file": self._sanitize_value(data.get("file"), f"{path}.file", depth=1),
                "result_count": data.get("result_count", len(raw_results) if isinstance(raw_results, list) else None),
                "results": compact_results,
                "summary": data.get("summary"),
            }
        )

    def _compress_usrp_code_result(self, arguments: Mapping[str, Any], tool_result: ToolResult, path: str) -> dict[str, Any]:
        data = tool_result.data
        large_fields = {
            "generated_code",
            "code",
            "stdout",
            "stderr",
            "traceback",
            "fft_arrays",
            "per_frequency_results",
            "knowledge_chunks",
        }

        def first_present(*values: Any) -> Any:
            for value in values:
                if value not in (None, "", [], {}):
                    return value
            return None

        def mark_large_fields(container: Mapping[str, Any], base_path: str) -> None:
            for name in large_fields:
                if name not in container or not container.get(name):
                    continue
                reason = "large generated code/log/array omitted"
                if name in {"stdout", "stderr", "traceback"}:
                    reason = "full log omitted; summary/root cause retained"
                elif name == "knowledge_chunks":
                    reason = "retrieval chunks omitted; compact facts retained"
                self._omit(f"{base_path}.{name}", reason)

        mark_large_fields(data, path)
        execution = data.get("execution_result") if isinstance(data.get("execution_result"), Mapping) else {}
        if execution:
            mark_large_fields(execution, f"{path}.execution_result")
        validation = data.get("validation") if isinstance(data.get("validation"), Mapping) else {}
        task_plan = data.get("task_plan") if isinstance(data.get("task_plan"), Mapping) else {}

        freq_range_hz = data.get("freq_range_hz")
        if not freq_range_hz and task_plan.get("freq_start_hz") and task_plan.get("freq_stop_hz"):
            freq_range_hz = [task_plan.get("freq_start_hz"), task_plan.get("freq_stop_hz")]
        key_facts = self._drop_empty(
            {
                "data_source": first_present(data.get("data_source"), execution.get("data_source")),
                "freq_range_hz": freq_range_hz,
                "freq_count": execution.get("freq_count"),
                "repeat_count": execution.get("repeat_count"),
                "frame_count": execution.get("frame_count"),
                "elapsed_sec": execution.get("elapsed_sec"),
                "peak_freq_hz": data.get("peak_freq_hz"),
                "peak_power_db": data.get("peak_power_db"),
                "output_file": first_present(data.get("output_file"), execution.get("output_file")),
                "artifact_id": data.get("artifact_id"),
                "dry_run": data.get("dry_run"),
            }
        )
        validation_summary = self._drop_empty(
            {
                "valid": validation.get("valid"),
                "errors": self._compact_list(validation.get("errors"), f"{path}.validation.errors", limit=self.options.top_items, depth=2)
                if isinstance(validation.get("errors"), list)
                else self._sanitize_value(validation.get("errors"), f"{path}.validation.errors", depth=2),
                "warnings": self._compact_list(validation.get("warnings"), f"{path}.validation.warnings", limit=self.options.top_items, depth=2)
                if isinstance(validation.get("warnings"), list)
                else self._sanitize_value(validation.get("warnings"), f"{path}.validation.warnings", depth=2),
            }
        )
        stderr_root = self._extract_error_root(str(first_present(data.get("stderr"), execution.get("stderr")) or ""))
        traceback_root = self._extract_error_root(str(first_present(data.get("traceback"), execution.get("traceback")) or ""))
        error_root = self._extract_error_root(str(first_present(tool_result.error, data.get("error"), execution.get("error")) or ""))
        return self._drop_empty(
            {
                "summary": data.get("summary"),
                "stage": data.get("stage"),
                "status": tool_result.status,
                "key_facts": key_facts,
                "validation": validation_summary,
                "error_root": error_root or traceback_root or stderr_root,
                "stderr_summary": stderr_root,
                "traceback_summary": traceback_root,
            }
        )

    def _compress_generic_mapping(self, arguments: Mapping[str, Any], tool_result: ToolResult, path: str) -> dict[str, Any]:
        result: dict[str, Any] = {}
        priority = ("summary", "status", "error", "message", "id", "asset_id", "artifact_id", "file_id", "path", "url", "preview_url")
        for key in priority:
            if key in tool_result.data:
                result[key] = self._sanitize_value(tool_result.data[key], f"{path}.{key}", depth=1)
        for key, value in tool_result.data.items():
            if key in result:
                continue
            if len(result) >= 12:
                self._omit(path, "generic mapping key limit reached")
                break
            result[str(key)] = self._sanitize_value(value, f"{path}.{key}", depth=1)
        return self._drop_empty(result)

    def _compress_tool_result_detail(self, arguments: Mapping[str, Any], tool_result: ToolResult, path: str) -> dict[str, Any]:
        data = tool_result.data
        value = data.get("value")
        if "value" in data and value not in (None, ""):
            self._omit(f"{path}.value", "full retrieved value omitted; preview retained")
        preview_limit = min(self.options.default_snippet_chars, int(data.get("max_chars") or self.options.default_snippet_chars), 1000)
        return self._drop_empty(
            {
                "tool_result_id": data.get("tool_result_id") or arguments.get("tool_result_id"),
                "tool_name": data.get("tool_name"),
                "path": data.get("path") or arguments.get("path"),
                "found": data.get("found"),
                "value_type": data.get("value_type"),
                "value_preview": self._tool_result_detail_preview(value, path=f"{path}.value_preview", limit=preview_limit)
                if value not in (None, "")
                else None,
                "truncated": data.get("truncated"),
                "original_chars": data.get("original_chars"),
                "returned_chars": data.get("returned_chars"),
                "max_chars": data.get("max_chars"),
                "error": data.get("error"),
            }
        )

    def _tool_result_detail_preview(self, value: Any, *, path: str, limit: int) -> str:
        text = str(value)
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            if self._looks_like_retrieved_document_text(text):
                return self._focused_text_window(text, max(limit, min(1000, self.options.default_snippet_chars * 2)), path)
            return self._clip_text(text, limit, path)
        if not isinstance(parsed, Mapping) or "chunk_text" not in parsed:
            if self._looks_like_retrieved_document_text(text):
                return self._focused_text_window(text, max(limit, min(1000, self.options.default_snippet_chars * 2)), path)
            return self._clip_text(text, limit, path)

        limit = max(limit, min(1000, self.options.default_snippet_chars * 2))
        source_text = " ".join(
            f"{key}={parsed[key]}"
            for key in ("file_name", "page", "chunk_index")
            if parsed.get(key) not in (None, "")
        )
        text_limit = max(120, limit - len(source_text) - 24)
        chunk_preview = self._focused_text_window(str(parsed.get("chunk_text") or ""), text_limit, path)
        return " ".join(f"{source_text} chunk_text={chunk_preview}".split())

    def _focused_text_window(self, text: str, limit: int, path: str) -> str:
        normalized = " ".join(str(text).split())
        if len(normalized) <= limit:
            return normalized
        lowered = normalized.lower()
        positions = [
            match.start()
            for match in re.finditer(r"\b(?:figure|fig)\.?\s*1\b|perception|recognition|decision", lowered)
        ]
        positions = [position for position in positions if position >= 0]
        if not positions:
            return self._clip_text(normalized, limit, path)

        start = max(0, min(positions) - max(0, limit // 5))
        end = min(len(normalized), start + limit)
        start = max(0, end - limit)
        self._omit(path, f"focused string window from {len(normalized)} to {limit} chars")
        return ("..." if start > 0 else "") + normalized[start:end] + ("..." if end < len(normalized) else "")

    def _looks_like_retrieved_document_text(self, text: str) -> bool:
        lowered = text.lower()
        return (
            "chunk_text" in lowered
            or "figure" in lowered
            or "fig." in lowered
            or ("perception" in lowered and ("recognition" in lowered or "decision" in lowered))
        )

    def _compact_list(self, value: Any, path: str, *, limit: int, depth: int) -> list[Any]:
        if not isinstance(value, list):
            return []
        items = [self._sanitize_value(item, f"{path}[{index}]", depth=depth) for index, item in enumerate(value[:limit])]
        if len(value) > limit:
            self._omit(path, f"kept first {limit} items out of {len(value)}")
        return items

    def _sanitize_value(self, value: Any, path: str, *, depth: int) -> Any:
        if value is None:
            return None
        if depth > self.options.max_depth:
            self._omit(path, f"max depth {self.options.max_depth} exceeded")
            return "<omitted>"
        if isinstance(value, (str, int, float, bool)):
            return self._clip_scalar(value, path=path, limit=self.options.default_snippet_chars)
        if isinstance(value, Mapping):
            result: dict[str, Any] = {}
            for index, (key, item) in enumerate(value.items()):
                if index >= 12:
                    self._omit(path, "mapping key limit reached")
                    break
                result[str(key)] = self._sanitize_value(item, f"{path}.{key}", depth=depth + 1)
            return self._drop_empty(result)
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            items = []
            for index, item in enumerate(list(value)[: self.options.top_items]):
                items.append(self._sanitize_value(item, f"{path}[{index}]", depth=depth + 1))
            if len(value) > self.options.top_items:
                self._omit(path, f"kept first {self.options.top_items} items out of {len(value)}")
            return items
        return self._clip_scalar(str(value), path=path, limit=self.options.default_snippet_chars)

    def _clip_scalar(self, value: Any, *, path: str, limit: int) -> Any:
        if not isinstance(value, str):
            return value
        return self._clip_text(value, limit, path)

    def _clip_text(self, text: str, limit: int, path: str) -> str:
        normalized = " ".join(str(text).split())
        if len(normalized) <= limit:
            return normalized
        self._omit(path, f"truncated string from {len(normalized)} to {limit} chars")
        return normalized[: max(0, limit - 3)] + "..."

    def _fit_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        while len(self._json(payload)) > self.options.max_output_chars:
            data = payload.get("data")
            if isinstance(data, dict):
                if self._shrink_document_data(data):
                    payload["truncated"] = True
                    payload["omitted_fields"] = list(self._omitted_fields)
                    continue
                if self._shrink_nl2sql_data(data):
                    payload["truncated"] = True
                    payload["omitted_fields"] = list(self._omitted_fields)
                    continue
            payload["data"] = self._minimal_data(data)
            self._omit("data", f"payload exceeded max_output_chars={self.options.max_output_chars}; minimized data")
            payload["truncated"] = True
            payload["omitted_fields"] = list(self._omitted_fields)
            if len(self._json(payload)) <= self.options.max_output_chars:
                break
            payload["data"] = {"summary": self._clip_text(str(payload["data"]), 120, "data.summary")}
            payload["omitted_fields"] = list(self._omitted_fields)
            break
        return payload

    def _with_compression_metrics(
        self,
        payload: dict[str, Any],
        raw_payload: dict[str, Any],
        *,
        enforce_max_output: bool = True,
    ) -> dict[str, Any]:
        payload.pop("compression", None)
        raw_chars = len(self._json(raw_payload))
        data_chars = len(self._json(payload.get("data")))
        omitted_fields_chars = len(self._json(payload.get("omitted_fields") or []))
        compression = {
            "raw_chars": raw_chars,
            "compact_chars": 0,
            "data_chars": data_chars,
            "omitted_fields_chars": omitted_fields_chars,
            "envelope_chars": 0,
            "compression_ratio": 0.0,
            "data_compression_ratio": round(data_chars / raw_chars, 6) if raw_chars else 1.0,
        }
        payload["compression"] = compression
        for _ in range(8):
            compact_chars = len(self._json(payload))
            next_compression = {
                **compression,
                "compact_chars": compact_chars,
                "compression_ratio": round(compact_chars / raw_chars, 6) if raw_chars else 1.0,
            }
            next_compression["envelope_chars"] = max(
                0,
                compact_chars - data_chars - omitted_fields_chars - len(self._json(next_compression)),
            )
            if next_compression == compression:
                break
            compression = next_compression
            payload["compression"] = compression
        if enforce_max_output and len(self._json(payload)) > self.options.max_output_chars:
            payload.pop("compression", None)
            if not self._shrink_for_compression_metrics(payload):
                return payload
            return self._with_compression_metrics(payload, raw_payload, enforce_max_output=True)
        if not enforce_max_output and len(self._json(payload)) > self.options.max_output_chars:
            payload.pop("compression", None)
        return payload

    def _shrink_for_compression_metrics(self, payload: dict[str, Any]) -> bool:
        data = payload.get("data")
        if isinstance(data, dict):
            if self._shrink_document_data(data) or self._shrink_nl2sql_data(data):
                payload["truncated"] = True
                payload["omitted_fields"] = list(self._omitted_fields)
                return True
        minimized = self._minimal_data(data)
        if minimized != data:
            payload["data"] = minimized
            self._omit("data", "minimized data to fit compression metrics")
            payload["truncated"] = True
            payload["omitted_fields"] = list(self._omitted_fields)
            return True
        return False

    def _shrink_document_data(self, data: dict[str, Any]) -> bool:
        results = data.get("results")
        if isinstance(results, list) and len(results) > 1:
            removed = len(results) - 1
            data["results"] = results[:1]
            self._omit("data.results", f"removed {removed} compact results to fit output budget")
            return True
        if isinstance(results, list) and results and isinstance(results[0], dict):
            snippet = str(results[0].get("snippet") or "")
            if len(snippet) > 80:
                results[0]["snippet"] = snippet[:77] + "..."
                self._omit("data.results[0].snippet", "shortened snippet to fit output budget")
                return True
        return False

    def _shrink_nl2sql_data(self, data: dict[str, Any]) -> bool:
        rows = data.get("top_rows")
        if isinstance(rows, list) and len(rows) > 1:
            removed = len(rows) - 1
            data["top_rows"] = rows[:1]
            self._omit("data.top_rows", f"removed {removed} preview rows to fit output budget")
            return True
        return False

    def _minimal_data(self, data: Any) -> dict[str, Any]:
        if isinstance(data, Mapping):
            return self._drop_empty(
                {
                    "summary": data.get("summary"),
                    "status": data.get("status"),
                    "error": data.get("error"),
                    "key_facts": data.get("key_facts"),
                }
            )
        return {"summary": self._clip_text(str(data), 160, "data")}

    def _extract_error_root(self, text: str) -> str | None:
        normalized = str(text or "").strip()
        if not normalized:
            return None
        lines = [line.strip() for line in normalized.splitlines() if line.strip()]
        for line in reversed(lines):
            if re.search(r"(Error|Exception|Timeout|Traceback|failed|失败)", line, flags=re.I):
                exception_match = re.search(r"([A-Za-z_][\w.]*?(?:Error|Exception|Timeout):\s*.+)$", line)
                if exception_match:
                    return self._clip_text(exception_match.group(1), 260, "data.error_root")
                return self._clip_text(line, 260, "data.error_root")
        return self._clip_text(lines[-1] if lines else normalized, 260, "data.error_root")

    def _hash_payload(self, payload: dict[str, Any]) -> str:
        text = self._json(payload)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def _json(self, payload: Any) -> str:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))

    def _omit(self, path: str, reason: str) -> None:
        item = {"path": path, "reason": reason}
        if item not in self._omitted_fields:
            self._omitted_fields.append(item)

    @staticmethod
    def _drop_empty(value: dict[str, Any]) -> dict[str, Any]:
        return {key: item for key, item in value.items() if item not in (None, "", [], {})}


class ToolTranscriptCompressor:
    def __init__(self, options: ToolTranscriptCompressionOptions | None = None) -> None:
        self.options = options or ToolTranscriptCompressionOptions()

    def compress_messages(self, messages: list[dict[str, Any]], *, aggressive: bool) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        tool_indexes = [index for index, item in enumerate(messages) if item.get("role") == "tool"]
        chars_before = self._messages_chars(messages)
        base_metrics = {
            "applied": False,
            "tool_message_count": len(tool_indexes),
            "compacted_tool_message_count": 0,
            "kept_recent_tool_messages": self.options.keep_recent_tool_messages,
            "malformed_tool_message_count": 0,
            "chars_before": chars_before,
            "chars_after": chars_before,
            "compression_ratio": 1.0,
        }
        if not aggressive or not tool_indexes:
            return messages, base_metrics

        keep = max(0, self.options.keep_recent_tool_messages)
        keep_indexes = set(tool_indexes[-keep:]) if keep else set()
        compressed: list[dict[str, Any]] = []
        compacted = 0
        malformed = 0
        for index, message in enumerate(messages):
            if message.get("role") != "tool" or index in keep_indexes:
                compressed.append(message)
                continue
            next_message, was_malformed = self._mini_tool_message(message)
            malformed += 1 if was_malformed else 0
            compacted += 1
            compressed.append(next_message)

        chars_after = self._messages_chars(compressed)
        metrics = {
            **base_metrics,
            "applied": compacted > 0,
            "compacted_tool_message_count": compacted,
            "malformed_tool_message_count": malformed,
            "chars_after": chars_after,
            "compression_ratio": round(chars_after / chars_before, 6) if chars_before else 1.0,
        }
        return compressed, metrics

    def _mini_tool_message(self, message: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
        raw_content = str(message.get("content") or "")
        malformed = False
        try:
            payload = json.loads(raw_content)
        except Exception:
            payload = {}
            malformed = True
        if not isinstance(payload, Mapping):
            payload = {}
            malformed = True

        mini = self._mini_payload(payload, malformed=malformed, raw_content=raw_content)
        return (
            {
                **dict(message),
                "content": json.dumps(mini, ensure_ascii=False),
            },
            malformed,
        )

    def _mini_payload(self, payload: Mapping[str, Any], *, malformed: bool, raw_content: str) -> dict[str, Any]:
        if malformed:
            return self._drop_empty(
                {
                    "status": "malformed_tool_content",
                    "can_retrieve_more": False,
                    "data": {"content_preview": self._clip(raw_content)},
                }
            )
        data = payload.get("data") if isinstance(payload.get("data"), Mapping) else {}
        return self._drop_empty(
            {
                "tool_result_id": payload.get("tool_result_id"),
                "tool_name": payload.get("tool_name"),
                "status": payload.get("status"),
                "terminal": payload.get("terminal"),
                "can_retrieve_more": payload.get("can_retrieve_more"),
                "data": self._mini_data(str(payload.get("tool_name") or ""), data),
                "omitted_fields": self._sanitize_omitted_fields(payload.get("omitted_fields")),
                "retrieval_hint": (
                    "详情已完整返回，请使用现有内容回答，不要重复回捞同一路径。"
                    if payload.get("terminal")
                    else payload.get("retrieval_hint") or "如需原始字段，调用 retrieve_tool_result_detail"
                ),
            }
        )

    def _mini_data(self, tool_name: str, data: Mapping[str, Any]) -> dict[str, Any]:
        file_info = data.get("file") if isinstance(data.get("file"), Mapping) else {}
        common = {
            "query": self._clip(data.get("query")),
            "file_id": data.get("file_id") or file_info.get("file_id"),
            "asset_id": data.get("asset_id") or file_info.get("asset_id"),
            "path": data.get("path"),
            "summary": self._clip(data.get("summary")),
            "result_count": data.get("result_count"),
        }
        if tool_name == "query_uploaded_documents":
            return self._drop_empty(
                {
                    **common,
                    "results": self._mini_results(data.get("results")),
                }
            )
        if tool_name == "retrieve_tool_result_detail":
            value_preview = data.get("value_preview")
            if value_preview in (None, "") and data.get("value") is not None:
                value_preview = data.get("value")
            return self._drop_empty(
                {
                    **common,
                    "tool_result_id": data.get("tool_result_id"),
                    "found": data.get("found"),
                    "value_type": data.get("value_type"),
                    "value_preview": self._clip(value_preview),
                    "truncated": data.get("truncated"),
                    "original_chars": data.get("original_chars"),
                    "returned_chars": data.get("returned_chars"),
                    "max_chars": data.get("max_chars"),
                }
            )
        if tool_name == "query_local_database":
            return self._drop_empty(
                {
                    **common,
                    "sql": self._clip(data.get("sql")),
                    "columns": data.get("columns"),
                    "row_count": data.get("row_count"),
                    "top_rows": self._first_list_item(data.get("top_rows")),
                }
            )
        if tool_name in {"run_autonomous_usrp_task", "generate_usrp_task_code", "execute_usrp_task_code"}:
            return self._drop_empty(
                {
                    **common,
                    "stage": data.get("stage"),
                    "status": data.get("status"),
                    "key_facts": self._sanitize_value(data.get("key_facts"), depth=0),
                    "error_root": self._clip(data.get("error_root")),
                    "stderr_summary": self._clip(data.get("stderr_summary")),
                    "traceback_summary": self._clip(data.get("traceback_summary")),
                }
            )
        return self._drop_empty({**common, "key_facts": self._sanitize_value(data.get("key_facts"), depth=0)})

    def _mini_results(self, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list) or not value:
            return []
        first = value[0]
        if not isinstance(first, Mapping):
            return []
        return [
            self._drop_empty(
                {
                    "asset_id": first.get("asset_id"),
                    "file_id": first.get("file_id"),
                    "file_name": first.get("file_name"),
                    "page": first.get("page"),
                    "chunk_index": first.get("chunk_index"),
                    "score": first.get("score"),
                    "snippet": self._clip(first.get("snippet")),
                }
            )
        ]

    def _sanitize_omitted_fields(self, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        result = []
        for item in value[:8]:
            if not isinstance(item, Mapping):
                continue
            result.append(self._drop_empty({"path": item.get("path"), "reason": self._clip(item.get("reason"))}))
        return result

    def _sanitize_value(self, value: Any, *, depth: int) -> Any:
        if value in (None, "", [], {}):
            return None
        if depth >= 2:
            return "<omitted>"
        if isinstance(value, str):
            return self._clip(value)
        if isinstance(value, (int, float, bool)):
            return value
        if isinstance(value, Mapping):
            return self._drop_empty({str(key): self._sanitize_value(item, depth=depth + 1) for key, item in list(value.items())[:8]})
        if isinstance(value, list):
            return [self._sanitize_value(item, depth=depth + 1) for item in value[:3]]
        return self._clip(str(value))

    def _first_list_item(self, value: Any) -> list[Any]:
        if not isinstance(value, list) or not value:
            return []
        return [value[0]]

    def _clip(self, value: Any) -> str | None:
        if value in (None, ""):
            return None
        text = " ".join(str(value).split())
        limit = max(0, self.options.snippet_chars)
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 3)] + "..."

    def _messages_chars(self, messages: list[dict[str, Any]]) -> int:
        return len(json.dumps(messages, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":")))

    @staticmethod
    def _drop_empty(value: dict[str, Any]) -> dict[str, Any]:
        return {key: item for key, item in value.items() if item not in (None, "", [], {})}
