from __future__ import annotations

import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from deepem.protocol import ChatMessage, ChatRole


def _semantic_debug_capture_enabled() -> bool:
    return str(os.getenv("DEEPEM_CONTEXT_SEMANTIC_DEBUG_CAPTURE") or "").strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }


@dataclass(slots=True)
class ContextCompressionOptions:
    keep_recent_messages: int = 8
    trigger_total_chars: int = 24000
    trigger_older_message_count: int = 1
    max_summary_chars: int = 6000
    max_tool_results: int = 8
    max_completed_actions: int = 12
    max_errors: int = 8
    max_files: int = 12
    semantic_enabled: bool = False
    semantic_timeout_seconds: float = 60.0
    semantic_max_output_chars: int = 6000
    semantic_max_input_chars: int = 12000
    semantic_previous_summary_max_chars: int = 4000
    semantic_new_history_max_chars: int = 7000
    semantic_message_max_chars: int = 1200
    semantic_max_tool_results: int = 5


NORMAL_CONTEXT_COMPRESSION = ContextCompressionOptions(
    keep_recent_messages=8,
    max_summary_chars=6000,
    max_tool_results=8,
)


AGGRESSIVE_CONTEXT_COMPRESSION = ContextCompressionOptions(
    keep_recent_messages=4,
    max_summary_chars=3000,
    max_tool_results=4,
    max_completed_actions=6,
    max_errors=4,
    max_files=6,
    semantic_max_input_chars=8000,
    semantic_new_history_max_chars=4500,
    semantic_message_max_chars=900,
    semantic_max_tool_results=3,
)


class ConversationContextCompressor:
    SEMANTIC_COMPRESSION_SYSTEM_PROMPT = (
        "You are an anchored context summarization assistant for coding and investigation sessions.\n"
        "Summarize only the older conversation history you are given. Do not answer the user's task.\n"
        "Merge previous_summary with new_history_messages, preserve still-true facts, remove stale details, "
        "and keep exact file paths, tool_result_id, tool_name, path, file_id, asset_id, run_id, and task_id when known.\n"
        "Return only valid JSON matching the requested schema."
    )

    _FILE_RE = re.compile(r"\b(?:main|docs|tests|eval|scripts|frontend|web)[\\/][\w./\\-]+")
    _KEY_VALUE_RE = re.compile(
        r"\b(max_chars|freq_count|sample_rate|bandwidth|output_file|repeat_count|duration|gain)\s*[:=]\s*([^\s,，。；;]+)"
    )
    _ACTION_RE = re.compile(r"[^。！？.!?\n]*(?:已完成|新增|修改|实现|通过|测试)[^。！？.!?\n]*[。！？.!?]?")
    _NEXT_RE = re.compile(r"[^。！？.!?\n]*(?:下一步|建议|后续|可以)[^。！？.!?\n]*[。！？.!?]?")
    _ERROR_TOKENS = ("Error", "Exception", "失败", "报错", "Traceback", "not defined")
    _QUESTION_TOKENS = ("是否", "还需要", "未解决", "下一步", "问题")

    def __init__(
        self,
        options: ContextCompressionOptions | None = None,
        *,
        llm_client: Any | None = None,
        compressor_llm_client: Any | None = None,
    ) -> None:
        self.options = options or ContextCompressionOptions()
        self.llm_client = llm_client
        self.compressor_llm_client = compressor_llm_client or llm_client

    def build_summary_message(
        self,
        *,
        older_messages: list[ChatMessage],
        transcript_messages: list[dict[str, Any]],
        assembled_messages: list[dict[str, Any]],
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        assembled_chars = len(self._json(assembled_messages))
        older_messages_chars = len(self._json(older_messages))
        has_older_messages = len(older_messages) >= self.options.trigger_older_message_count
        metrics: dict[str, Any] = {
            "applied": False,
            "method": "rule_based_minimal",
            "older_message_count": len(older_messages),
            "older_messages_chars": older_messages_chars,
            "assembled_chars_before": assembled_chars,
            "summary_chars": 0,
            "summary_compression_ratio": 0.0,
            "tool_result_count": 0,
            "trigger_total_chars": self.options.trigger_total_chars,
            "prompt_over_threshold": assembled_chars > self.options.trigger_total_chars,
            "semantic_enabled": self.options.semantic_enabled,
            "semantic_attempted": False,
            "semantic_applied": False,
            "semantic_fallback_reason": None,
            "semantic_input_original_chars": older_messages_chars,
            "semantic_input_chars": 0,
            "semantic_input_trimmed": False,
            "semantic_input_message_count": 0,
            "semantic_input_dropped_message_count": 0,
            "semantic_tool_constraint_count": 0,
            "schema_version": 1,
        }
        if not has_older_messages:
            metrics["skipped_reason"] = "no_older_messages"
            return None, metrics

        semantic_summary, semantic_fallback_reason, semantic_metrics = self._try_semantic_compression(
            older_messages=older_messages,
            transcript_messages=transcript_messages,
        )
        metrics.update(semantic_metrics)
        if semantic_summary is not None:
            summary = semantic_summary
            metrics.update(
                {
                    "method": "llm_semantic_structured_v1",
                    "semantic_applied": True,
                    "semantic_fallback_reason": None,
                    "schema_version": 2,
                }
            )
        else:
            summary = self._build_summary(older_messages=older_messages, transcript_messages=transcript_messages)
            if self.options.semantic_enabled:
                metrics["semantic_fallback_reason"] = semantic_fallback_reason or "unknown"
        if not self._has_summary_content(summary):
            metrics["skipped_reason"] = "empty_summary"
            return None, metrics
        content = self._summary_content(summary)
        if len(content) > self.options.max_summary_chars:
            summary = self._shrink_summary(summary)
            content = self._summary_content(summary)
        metrics.update(
            {
                "applied": True,
                "summary_chars": len(content),
                "summary_compression_ratio": round(len(content) / older_messages_chars, 6) if older_messages_chars else 1.0,
                "tool_result_count": len(summary.get("tool_result_index") or []),
            }
        )
        return {"role": "user", "content": content}, metrics

    def _has_summary_content(self, summary: Mapping[str, Any]) -> bool:
        for key in (
            "user_goal",
            "current_task_state",
            "completed_actions",
            "key_parameters",
            "important_files",
            "tool_result_index",
            "errors_and_resolutions",
            "open_questions",
            "next_steps",
            "evidence_refs",
            "uncertain_claims",
            "stale_or_superseded_claims",
        ):
            if summary.get(key) not in (None, "", [], {}):
                return True
        return False

    def _try_semantic_compression(
        self,
        *,
        older_messages: list[ChatMessage],
        transcript_messages: list[dict[str, Any]],
    ) -> tuple[dict[str, Any] | None, str | None, dict[str, Any]]:
        semantic_metrics: dict[str, Any] = {}
        if not self.options.semantic_enabled:
            return None, None, semantic_metrics
        if self.compressor_llm_client is None:
            return None, "missing_client", semantic_metrics

        previous_summary, new_history_messages = self._split_previous_summary(older_messages)
        tool_result_constraints = self._extract_tool_result_index(older_messages, transcript_messages)
        prompt_payload, semantic_metrics = self._build_semantic_prompt_payload(
            previous_summary=previous_summary,
            new_history_messages=new_history_messages,
            tool_result_constraints=tool_result_constraints,
        )
        prompt_content = self._json(prompt_payload)
        semantic_metrics["semantic_attempted"] = True
        semantic_metrics["semantic_input_chars"] = len(prompt_content)
        compression_messages = [
            {"role": "system", "content": self.SEMANTIC_COMPRESSION_SYSTEM_PROMPT},
            {"role": "user", "content": prompt_content},
        ]

        executor = ThreadPoolExecutor(max_workers=1)
        semantic_started = time.perf_counter()
        future = executor.submit(
            self.compressor_llm_client.complete,
                    messages=compression_messages,
                    tools=[],
                    temperature=0.0,
                    generation_options={"enable_thinking": False, "stream": False},
                    stream_handler=None,
                )
        try:
            response = future.result(timeout=max(0.001, float(self.options.semantic_timeout_seconds)))
        except TimeoutError:
            semantic_metrics["semantic_latency_ms"] = round((time.perf_counter() - semantic_started) * 1000, 3)
            future.cancel()
            executor.shutdown(wait=False, cancel_futures=True)
            return None, "timeout", semantic_metrics
        except Exception:
            semantic_metrics["semantic_latency_ms"] = round((time.perf_counter() - semantic_started) * 1000, 3)
            executor.shutdown(wait=False, cancel_futures=True)
            return None, "exception", semantic_metrics
        finally:
            if future.done():
                executor.shutdown(wait=False, cancel_futures=True)

        semantic_metrics["semantic_latency_ms"] = round((time.perf_counter() - semantic_started) * 1000, 3)

        content = str(getattr(response, "content", "") or "").strip()
        semantic_metrics["semantic_raw_output_chars"] = len(content)
        if _semantic_debug_capture_enabled():
            semantic_metrics["semantic_raw_output"] = content
        if not content:
            return None, "empty_summary", semantic_metrics
        parsed = self._parse_json_object(content)
        canonical_content = content
        if isinstance(parsed, Mapping):
            parsed = dict(parsed)
            parsed.pop("tool_result_index", None)
            parsed.pop("evidence_refs", None)
            canonical_content = self._json(parsed)
        semantic_metrics["semantic_canonical_output_chars"] = len(canonical_content)
        if len(canonical_content) > self.options.semantic_max_output_chars:
            return None, "output_too_long", semantic_metrics
        if not isinstance(parsed, Mapping):
            return None, "invalid_json", semantic_metrics
        if not self._has_semantic_core_content(parsed):
            return None, "missing_core_fields", semantic_metrics
        summary = self._normalize_semantic_summary(parsed, older_count=len(older_messages))
        if summary is None:
            return None, "missing_core_fields", semantic_metrics
        summary = self._merge_tool_result_constraints(
            summary,
            list(prompt_payload.get("tool_result_index_constraints") or []),
        )
        if not self._has_summary_content(summary):
            return None, "empty_summary", semantic_metrics
        return summary, None, semantic_metrics

    def _has_semantic_core_content(self, payload: Mapping[str, Any]) -> bool:
        for key in (
            "user_goal",
            "current_task_state",
            "completed_actions",
            "key_parameters",
            "important_files",
            "errors_and_resolutions",
            "uncertain_claims",
            "stale_or_superseded_claims",
            "open_questions",
            "next_steps",
        ):
            if payload.get(key) not in (None, "", [], {}):
                return True
        return False

    def _semantic_prompt(
        self,
        *,
        previous_summary: dict[str, Any] | None,
        new_history_messages: list[ChatMessage],
        tool_result_constraints: list[dict[str, Any]],
    ) -> str:
        payload, _ = self._build_semantic_prompt_payload(
            previous_summary=previous_summary,
            new_history_messages=new_history_messages,
            tool_result_constraints=tool_result_constraints,
        )
        return self._json(payload)

    def _build_semantic_prompt_payload(
        self,
        *,
        previous_summary: dict[str, Any] | None,
        new_history_messages: list[ChatMessage],
        tool_result_constraints: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        original_message_count = len(new_history_messages)
        trimmed_previous_summary = self._trim_semantic_previous_summary(previous_summary)
        trimmed_tool_constraints = self._trim_semantic_tool_constraints(tool_result_constraints)
        message_payloads = [self._semantic_message_payload(item) for item in new_history_messages]
        message_payloads = self._limit_payload_list_chars(
            message_payloads,
            max(0, int(self.options.semantic_new_history_max_chars)),
        )
        payload = {
            "task": "Incrementally update previous_summary using new_history_messages. Return only JSON.",
            "rules": [
                "Preserve still-valid conclusions and evidence IDs.",
                "Add new progress and current state.",
                "Move superseded claims into stale_or_superseded_claims.",
                "Move unsupported or uncertain claims into uncertain_claims.",
                "Do not repeat unchanged details unnecessarily.",
                "Do not output tool_result_index or evidence_refs; the system restores exact retrieval anchors separately.",
            ],
            "schema": self._semantic_model_schema(covered_message_count=original_message_count),
            "previous_summary": trimmed_previous_summary,
            "new_history_messages": message_payloads,
            "tool_result_index_constraints": trimmed_tool_constraints,
        }
        max_input_chars = max(1, int(self.options.semantic_max_input_chars))
        while len(self._json(payload)) > max_input_chars and payload["new_history_messages"]:
            payload["new_history_messages"].pop(0)
        while len(self._json(payload)) > max_input_chars and payload["tool_result_index_constraints"]:
            payload["tool_result_index_constraints"].pop(0)
        if len(self._json(payload)) > max_input_chars:
            payload["previous_summary"] = {}

        input_chars = len(self._json(payload))
        metrics = {
            "semantic_input_chars": input_chars,
            "semantic_input_trimmed": (
                input_chars < len(self._json(self._untrimmed_semantic_prompt_payload(
                    previous_summary=previous_summary,
                    new_history_messages=new_history_messages,
                    tool_result_constraints=tool_result_constraints,
                )))
                or len(payload["new_history_messages"]) < original_message_count
                or len(payload["tool_result_index_constraints"]) < len(tool_result_constraints)
            ),
            "semantic_input_message_count": len(payload["new_history_messages"]),
            "semantic_input_dropped_message_count": original_message_count - len(payload["new_history_messages"]),
            "semantic_tool_constraint_count": len(payload["tool_result_index_constraints"]),
        }
        return payload, metrics

    def _untrimmed_semantic_prompt_payload(
        self,
        *,
        previous_summary: dict[str, Any] | None,
        new_history_messages: list[ChatMessage],
        tool_result_constraints: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "task": "Incrementally update previous_summary using new_history_messages. Return only JSON.",
            "rules": [],
            "schema": self._empty_semantic_summary(covered_message_count=len(new_history_messages)),
            "previous_summary": previous_summary or {},
            "new_history_messages": [self._message_payload(item) for item in new_history_messages],
            "tool_result_index_constraints": tool_result_constraints,
        }

    def _split_previous_summary(self, older_messages: list[ChatMessage]) -> tuple[dict[str, Any] | None, list[ChatMessage]]:
        previous_summary: dict[str, Any] | None = None
        new_history: list[ChatMessage] = []
        for message in older_messages:
            summary = self._extract_embedded_summary(message.content or "")
            if summary is not None:
                previous_summary = summary
                continue
            new_history.append(message)
        return previous_summary, new_history

    def _message_payload(self, message: ChatMessage) -> dict[str, Any]:
        return {
            "id": message.id,
            "role": getattr(message.role, "value", str(message.role)),
            "content": self._clip(message.content or "", 1800),
            "run_id": message.run_id,
            "created_at": message.created_at,
        }

    def _semantic_message_payload(self, message: ChatMessage) -> dict[str, Any]:
        role = getattr(message.role, "value", str(message.role))
        limit = int(self.options.semantic_message_max_chars)
        if role not in {"operator", "assistant", "user"}:
            limit = min(800, limit)
        return {
            "id": message.id,
            "role": role,
            "content": self._clip(message.content or "", max(0, limit)),
            "run_id": message.run_id,
            "created_at": message.created_at,
        }

    def _trim_semantic_previous_summary(self, previous_summary: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(previous_summary, Mapping):
            return {}
        result = self._empty_semantic_summary(
            covered_message_count=int(previous_summary.get("covered_message_count") or 0)
        )
        for key in ("summary_type", "version", "compression_method", "covered_message_count"):
            if key in previous_summary:
                result[key] = previous_summary[key]
        for key in ("user_goal", "current_task_state"):
            result[key] = self._clip(str(previous_summary.get(key) or ""), 600)
        if isinstance(previous_summary.get("key_parameters"), Mapping):
            result["key_parameters"] = self._sanitize_tool_value(previous_summary.get("key_parameters"))
        for key in (
            "completed_actions",
            "important_files",
            "errors_and_resolutions",
            "uncertain_claims",
            "stale_or_superseded_claims",
            "open_questions",
            "next_steps",
        ):
            value = previous_summary.get(key)
            if isinstance(value, list):
                result[key] = self._sanitize_tool_value(value[: self.options.semantic_max_tool_results])
        max_chars = max(0, int(self.options.semantic_previous_summary_max_chars))
        while len(self._json(result)) > max_chars and max_chars > 0:
            changed = False
            for key in (
                "completed_actions",
                "important_files",
                "errors_and_resolutions",
                "uncertain_claims",
                "stale_or_superseded_claims",
                "open_questions",
                "next_steps",
            ):
                if isinstance(result.get(key), list) and len(result[key]) > 1:
                    result[key] = result[key][:-1]
                    changed = True
                    break
            if not changed:
                result["user_goal"] = self._clip(str(result.get("user_goal") or ""), 240)
                result["current_task_state"] = self._clip(str(result.get("current_task_state") or ""), 240)
                break
        return result

    def _trim_semantic_tool_constraints(self, tool_result_constraints: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return self._compact_tool_anchors(
            tool_result_constraints,
            limit=self.options.semantic_max_tool_results,
        )

    def _limit_payload_list_chars(self, items: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        result = list(items)
        while result and len(self._json(result)) > limit:
            result.pop(0)
        return result

    def _empty_semantic_summary(self, *, covered_message_count: int) -> dict[str, Any]:
        return {
            "summary_type": "conversation_context_summary",
            "version": 2,
            "compression_method": "llm_semantic_structured_v1",
            "covered_message_count": covered_message_count,
            "user_goal": "",
            "current_task_state": "",
            "completed_actions": [],
            "key_parameters": {},
            "important_files": [],
            "tool_result_index": [],
            "evidence_refs": [],
            "errors_and_resolutions": [],
            "uncertain_claims": [],
            "stale_or_superseded_claims": [],
            "open_questions": [],
            "next_steps": [],
        }

    def _semantic_model_schema(self, *, covered_message_count: int) -> dict[str, Any]:
        schema = self._empty_semantic_summary(covered_message_count=covered_message_count)
        schema.pop("tool_result_index", None)
        schema.pop("evidence_refs", None)
        return schema

    def _normalize_semantic_summary(self, payload: Mapping[str, Any], *, older_count: int) -> dict[str, Any] | None:
        if payload.get("summary_type") != "conversation_context_summary":
            return None
        summary = self._empty_semantic_summary(covered_message_count=older_count)
        summary["covered_message_count"] = int(payload.get("covered_message_count") or older_count)
        for key in ("user_goal", "current_task_state"):
            summary[key] = self._clip(str(payload.get(key) or ""), 900)
        for key in (
            "completed_actions",
            "important_files",
            "errors_and_resolutions",
            "uncertain_claims",
            "stale_or_superseded_claims",
            "open_questions",
            "next_steps",
        ):
            value = payload.get(key)
            if isinstance(value, list):
                if key == "errors_and_resolutions":
                    summary[key] = [self._sanitize_tool_value(item) for item in value[: self.options.max_tool_results]]
                else:
                    summary[key] = self._sanitize_tool_value(value)
        if isinstance(payload.get("key_parameters"), Mapping):
            summary["key_parameters"] = self._sanitize_tool_value(payload.get("key_parameters"))
        return summary

    def _merge_tool_result_constraints(
        self,
        summary: dict[str, Any],
        constraints: list[dict[str, Any]],
    ) -> dict[str, Any]:
        summary["tool_result_index"] = self._compact_tool_anchors(
            constraints,
            limit=self.options.max_tool_results,
        )
        summary["evidence_refs"] = []
        return summary

    def _build_summary(self, *, older_messages: list[ChatMessage], transcript_messages: list[dict[str, Any]]) -> dict[str, Any]:
        user_messages = [item for item in older_messages if item.role == ChatRole.OPERATOR]
        assistant_messages = [item for item in older_messages if item.role == ChatRole.ASSISTANT]
        all_texts = [item.content or "" for item in older_messages]
        assistant_texts = [item.content or "" for item in assistant_messages]

        return {
            "summary_type": "conversation_context_summary",
            "version": 1,
            "compression_method": "rule_based_minimal",
            "covered_message_count": len(older_messages),
            "user_goal": self._clip(self._goal_text(user_messages), 500),
            "current_task_state": self._clip(self._latest_text(assistant_messages), 500),
            "completed_actions": self._extract_matches(assistant_texts, self._ACTION_RE, self.options.max_completed_actions, 260),
            "key_parameters": self._extract_key_parameters(all_texts),
            "important_files": self._extract_files(all_texts),
            "tool_result_index": self._extract_tool_result_index(older_messages, transcript_messages),
            "errors_and_resolutions": self._extract_error_snippets(all_texts),
            "open_questions": self._extract_token_snippets([item.content or "" for item in user_messages], self._QUESTION_TOKENS, 8),
            "next_steps": self._extract_matches(assistant_texts, self._NEXT_RE, 8, 260),
        }

    def _summary_content(self, summary: dict[str, Any]) -> str:
        return (
            "较早历史上下文摘要（由系统压缩生成）:\n"
            f"{json.dumps(summary, ensure_ascii=False, indent=2, default=str)}\n\n"
            "若需要工具原始字段，请调用 retrieve_tool_result_detail。"
        )

    def _shrink_summary(self, summary: dict[str, Any]) -> dict[str, Any]:
        compact = dict(summary)
        compact["user_goal"] = self._clip(str(compact.get("user_goal") or ""), 300)
        compact["current_task_state"] = self._clip(str(compact.get("current_task_state") or ""), 300)
        for key in ("completed_actions", "errors_and_resolutions", "open_questions", "next_steps"):
            compact[key] = [self._clip(str(item), 180) for item in list(compact.get(key) or [])[:4]]
        compact["important_files"] = list(compact.get("important_files") or [])[:6]
        compact["tool_result_index"] = self._compact_tool_anchors(
            list(compact.get("tool_result_index") or []),
            limit=min(3, self.options.max_tool_results),
        )
        compact["evidence_refs"] = []
        return compact

    def _latest_text(self, messages: list[ChatMessage]) -> str:
        for item in reversed(messages):
            text = (item.content or "").strip()
            if text:
                return text
        return ""

    def _goal_text(self, messages: list[ChatMessage]) -> str:
        candidates = []
        for item in messages:
            text = (item.content or "").strip()
            if not text:
                continue
            if any(token in text for token in self._ERROR_TOKENS + self._QUESTION_TOKENS):
                continue
            candidates.append(text)
        if candidates:
            return max(candidates, key=len)
        return self._latest_text(messages)

    def _extract_matches(self, texts: list[str], pattern: re.Pattern[str], limit: int, clip_limit: int) -> list[str]:
        result: list[str] = []
        for text in reversed(texts):
            for match in pattern.findall(text):
                item = self._clip(match.strip(), clip_limit)
                if item and item not in result:
                    result.append(item)
                if len(result) >= limit:
                    return list(reversed(result))
        return list(reversed(result))

    def _extract_files(self, texts: list[str]) -> list[str]:
        result: list[str] = []
        for text in texts:
            for match in self._FILE_RE.findall(text):
                normalized = match.rstrip("。；;，,.)]")
                if normalized not in result:
                    result.append(normalized)
                if len(result) >= self.options.max_files:
                    return result
        return result

    def _extract_key_parameters(self, texts: list[str]) -> dict[str, str]:
        result: dict[str, str] = {}
        for text in texts:
            for key, value in self._KEY_VALUE_RE.findall(text):
                result[key] = value.strip("'\"")
        return result

    def _extract_error_snippets(self, texts: list[str]) -> list[dict[str, str]]:
        snippets = self._extract_token_snippets(texts, self._ERROR_TOKENS, self.options.max_errors)
        return [{"error": item, "resolution": ""} for item in snippets]

    def _extract_token_snippets(self, texts: list[str], tokens: tuple[str, ...], limit: int) -> list[str]:
        result: list[str] = []
        for text in reversed(texts):
            if not any(token in text for token in tokens):
                continue
            item = self._clip(text.strip(), 280)
            if item and item not in result:
                result.append(item)
            if len(result) >= limit:
                break
        return list(reversed(result))

    def _extract_tool_result_index(
        self,
        older_messages: list[ChatMessage],
        transcript_messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        for message in older_messages:
            parsed = self._try_parse_json(message.content)
            if isinstance(parsed, Mapping):
                candidates.append(dict(parsed))
        for message in transcript_messages:
            if message.get("role") != "tool":
                continue
            parsed = self._try_parse_json(str(message.get("content") or ""))
            if isinstance(parsed, Mapping):
                candidates.append(dict(parsed))

        for message in older_messages:
            summary = self._extract_embedded_summary(message.content or "")
            if summary is not None:
                candidates.extend(
                    item for item in list(summary.get("tool_result_index") or []) if isinstance(item, Mapping)
                )
        return self._compact_tool_anchors(candidates, limit=self.options.max_tool_results)

    def _compact_tool_result(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        data = payload.get("data") if isinstance(payload.get("data"), Mapping) else {}
        file_info = data.get("file") if isinstance(data.get("file"), Mapping) else {}
        first_result = self._first_mapping(data.get("results"))
        omitted_paths = self._tool_omitted_paths(payload.get("omitted_fields"))
        direct_path = payload.get("path") or data.get("path")
        if direct_path and str(direct_path) not in omitted_paths:
            omitted_paths.insert(0, str(direct_path))
        path = str(direct_path or (omitted_paths[0] if omitted_paths else "") or "")
        retrieval_hint = (
            payload.get("retrieval_hint")
            or data.get("query")
            or "如需原始字段，调用 retrieve_tool_result_detail"
        )
        key_facts = self._compact_anchor_key_facts(data.get("key_facts"))
        result = {
            "tool_result_id": payload.get("tool_result_id"),
            "tool_name": payload.get("tool_name"),
            "file_id": payload.get("file_id") or data.get("file_id") or file_info.get("file_id") or first_result.get("file_id"),
            "asset_id": payload.get("asset_id") or data.get("asset_id") or file_info.get("asset_id") or first_result.get("asset_id"),
            "key_facts": key_facts,
            "path": path or None,
            "found": data.get("found"),
            "omitted_fields": [{"path": item} for item in omitted_paths[:2]],
            "retrieval_hint": self._clip(str(retrieval_hint), 120),
        }
        return {key: value for key, value in result.items() if value not in (None, "", [], {})}

    def _compact_tool_anchors(self, items: list[dict[str, Any]] | list[Mapping[str, Any]], *, limit: int) -> list[dict[str, Any]]:
        indexed: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for item in items:
            if not isinstance(item, Mapping):
                continue
            compact = self._compact_tool_result(item)
            tool_result_id = str(compact.get("tool_result_id") or "")
            if not tool_result_id:
                continue
            if tool_result_id in indexed:
                order.remove(tool_result_id)
            indexed[tool_result_id] = compact
            order.append(tool_result_id)
        return [indexed[item_id] for item_id in order[-max(0, int(limit)) :]]

    @staticmethod
    def _tool_omitted_paths(value: Any) -> list[str]:
        paths: list[str] = []
        for item in value if isinstance(value, list) else []:
            path = item.get("path") if isinstance(item, Mapping) else item
            normalized = str(path or "").strip()
            if normalized and normalized not in paths:
                paths.append(normalized)
        return paths

    def _compact_anchor_key_facts(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            return {}
        result: dict[str, Any] = {}
        for key, item in list(value.items())[:2]:
            if isinstance(item, (str, int, float, bool)):
                result[str(key)] = self._clip(str(item), 100) if isinstance(item, str) else item
        return result

    @staticmethod
    def _first_mapping(value: Any) -> Mapping[str, Any]:
        if isinstance(value, list) and value and isinstance(value[0], Mapping):
            return value[0]
        return {}

    def _sanitize_tool_value(self, value: Any, *, depth: int = 0) -> Any:
        if value is None:
            return None
        if isinstance(value, str):
            return self._clip(value, 240)
        if isinstance(value, (int, float, bool)):
            return value
        if depth >= 3:
            return "<omitted>"
        if isinstance(value, Mapping):
            return {
                str(key): self._sanitize_tool_value(item, depth=depth + 1)
                for key, item in list(value.items())[:12]
                if str(key) not in {"value", "value_preview"}
            }
        if isinstance(value, list):
            return [self._sanitize_tool_value(item, depth=depth + 1) for item in value[:8]]
        return self._clip(str(value), 240)

    def _try_parse_json(self, text: str) -> Any:
        normalized = str(text or "").strip()
        if not normalized.startswith("{"):
            return None
        try:
            return json.loads(normalized)
        except Exception:
            return None

    def _extract_embedded_summary(self, text: str) -> dict[str, Any] | None:
        if "conversation_context_summary" not in str(text or ""):
            return None
        parsed = self._parse_json_object(text)
        if isinstance(parsed, Mapping) and parsed.get("summary_type") == "conversation_context_summary":
            return dict(parsed)
        return None

    def _parse_json_object(self, text: str) -> Any:
        normalized = str(text or "").strip()
        fence_match = re.match(r"^```(?:json)?\s*(.*?)\s*```$", normalized, flags=re.S | re.I)
        if fence_match:
            normalized = fence_match.group(1).strip()
        if not normalized.startswith("{"):
            start = normalized.find("{")
            end = normalized.rfind("}")
            if start < 0 or end <= start:
                return None
            normalized = normalized[start : end + 1]
        try:
            return json.loads(normalized)
        except Exception:
            return None

    def _json(self, value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))

    def _clip(self, text: str, limit: int) -> str:
        normalized = " ".join(str(text or "").split())
        if len(normalized) <= limit:
            return normalized
        return normalized[: max(0, limit - 3)] + "..."
