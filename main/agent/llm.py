from __future__ import annotations

import json
import re
from collections import deque
from dataclasses import dataclass, field
from itertools import count
from typing import Any, Callable, Mapping, Protocol

try:  # pragma: no cover - optional dependency in demo package
    from openai import OpenAI
except Exception:  # pragma: no cover
    OpenAI = None

from deepem.agent.config import LLMSettings
from deepem.tools.base import ToolDefinition


@dataclass(slots=True)
class LLMToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(slots=True)
class LLMResponse:
    content: str = ""
    reasoning: str = ""
    tool_calls: list[LLMToolCall] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


LLMStreamHandler = Callable[[str, dict[str, Any]], None]
LLMCancelChecker = Callable[[], bool]
LLMGenerationOptions = Mapping[str, Any]


class LLMCancelledError(RuntimeError):
    pass


class LLMClient(Protocol):
    def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[ToolDefinition],
        temperature: float,
        generation_options: LLMGenerationOptions | None = None,
        stream_handler: LLMStreamHandler | None = None,
        cancel_checker: LLMCancelChecker | None = None,
    ) -> LLMResponse: ...


class OpenAICompatibleLLMClient:
    def __init__(self, settings: LLMSettings) -> None:
        if OpenAI is None:
            raise RuntimeError("openai package is not installed")
        self.settings = settings
        self.client = OpenAI(
            api_key=settings.api_key,
            base_url=settings.base_url,
            timeout=settings.timeout_seconds,
            default_headers=settings.extra_headers or None,
        )
        self._tool_call_counter = count(1)

    def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[ToolDefinition],
        temperature: float,
        generation_options: LLMGenerationOptions | None = None,
        stream_handler: LLMStreamHandler | None = None,
        cancel_checker: LLMCancelChecker | None = None,
    ) -> LLMResponse:
        self._raise_if_cancelled(cancel_checker)
        options = dict(generation_options or {})
        payload: dict[str, Any] = {
            "model": self.settings.model,
            "messages": messages,
            "temperature": self._float_option(options, "temperature", temperature),
            "stream": True,
        }

        self._apply_generation_options(payload, options)
        extra = payload.get("extra_body", {})
        # Reconstruct the final HTTP JSON body as vLLM will receive it
        # (OpenAI SDK flattens extra_body keys to HTTP top level)
        http_body = {k: v for k, v in payload.items() if k != "extra_body"}
        http_body.update(extra)
      

        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": item.name,
                        "description": item.description,
                        "parameters": item.input_schema,
                    },
                }
                for item in tools
            ]
            payload["tool_choice"] = "auto"

        response = self.client.chat.completions.create(**payload)
        content_chunks: list[str] = []
        reasoning_chunks: list[str] = []
        raw_chunks: list[dict[str, Any]] = []
        tool_call_states: dict[int, dict[str, Any]] = {}
        content_started = False
        reasoning_started = False
        inline_think_state: dict[str, Any] = {"buffer": "", "inside": False}

        def emit_reasoning(piece: str) -> None:
            nonlocal reasoning_started
            if not piece:
                return
            reasoning_chunks.append(piece)
            if stream_handler is not None:
                if not reasoning_started:
                    stream_handler("reasoning_start", {})
                    reasoning_started = True
                stream_handler("reasoning_delta", {"delta": piece})

        def emit_content(piece: str) -> None:
            nonlocal content_started
            if not piece:
                return
            content_chunks.append(piece)
            if stream_handler is not None:
                if not content_started:
                    stream_handler("content_start", {})
                    content_started = True
                stream_handler("content_delta", {"delta": piece})

        def finish_streams(finish_reason: str) -> None:
            nonlocal reasoning_started, content_started
            inline_reasoning, visible_content = self._demux_inline_thinking("", inline_think_state, final=True)
            for piece in inline_reasoning:
                emit_reasoning(piece)
            for piece in visible_content:
                emit_content(piece)
            if stream_handler is not None:
                if reasoning_started:
                    stream_handler("reasoning_done", {})
                    reasoning_started = False
                if content_started:
                    stream_handler("content_done", {"finish_reason": finish_reason})
                    content_started = False

        try:
            for chunk in response:
                self._raise_if_cancelled(cancel_checker, response=response)
                chunk_raw = chunk.model_dump(mode="json") if hasattr(chunk, "model_dump") else {}
                raw_chunks.append(chunk_raw)
                delta, finish_reason = self._extract_delta_and_finish_reason(chunk, chunk_raw)
                raw_delta = self._extract_raw_delta(chunk_raw)

                reasoning_delta = self._normalize_content(getattr(delta, "reasoning_content", None))
                if not reasoning_delta and isinstance(delta, dict):
                    reasoning_delta = self._normalize_content(delta.get("reasoning"))
                if not reasoning_delta and raw_delta:
                    reasoning_delta = self._normalize_content(
                        raw_delta.get("reasoning_content")
                        or raw_delta.get("reasoning")
                        or raw_delta.get("reasoning_delta")
                        or raw_delta.get("thinking")
                    )
                if not reasoning_delta:
                    reasoning_delta = self._normalize_content(getattr(delta, "reasoning", None))
                if reasoning_delta:
                    emit_reasoning(self._strip_think_output_delta(reasoning_delta))

                content_delta = self._normalize_content(getattr(delta, "content", None))
                if not content_delta and isinstance(delta, dict):
                    content_delta = self._normalize_content(delta.get("content"))
                if not content_delta and raw_delta:
                    content_delta = self._normalize_content(raw_delta.get("content"))
                if content_delta:
                    inline_reasoning, visible_content = self._demux_inline_thinking(
                        content_delta, inline_think_state
                    )
                    for piece in inline_reasoning:
                        emit_reasoning(piece)
                    for piece in visible_content:
                        emit_content(piece)

                tool_delta = getattr(delta, "tool_calls", None)
                if tool_delta is None and isinstance(delta, dict):
                    tool_delta = delta.get("tool_calls")
                if tool_delta is None and raw_delta:
                    tool_delta = raw_delta.get("tool_calls")
                self._merge_tool_call_deltas(tool_call_states, tool_delta)

                self._raise_if_cancelled(cancel_checker, response=response)

                if finish_reason:
                    finish_streams(str(finish_reason))
        except LLMCancelledError:
            finish_streams("cancelled")
            raise

        # Some OpenAI-compatible servers omit finish_reason on their last chunk.
        # Flush any split <think> tag and close the UI streams deterministically.
        if reasoning_started or content_started or inline_think_state.get("buffer"):
            finish_streams("stop")

        content = self._strip_think_output("".join(content_chunks))
        reasoning = "".join(reasoning_chunks).strip()
        raw = {"chunks": raw_chunks}

        tool_calls = self._finalize_tool_calls(tool_call_states)

        if not tool_calls and content:
            parsed_tool_calls, cleaned_content = self._extract_tool_calls_from_content(content)
            if parsed_tool_calls:
                tool_calls = parsed_tool_calls
                content = cleaned_content.strip()

        return LLMResponse(
            content=content,
            reasoning=reasoning,
            tool_calls=tool_calls,
            raw=raw,
        )

    @staticmethod
    def _close_stream(response: Any) -> None:
        close = getattr(response, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

    @classmethod
    def _raise_if_cancelled(cls, cancel_checker: LLMCancelChecker | None, *, response: Any | None = None) -> None:
        if cancel_checker is not None and cancel_checker():
            if response is not None:
                cls._close_stream(response)
            raise LLMCancelledError("llm_generation_cancelled")

    def _apply_generation_options(self, payload: dict[str, Any], options: dict[str, Any]) -> None:
        top_p = self._optional_float_option(options, "top_p")
        if top_p is not None:
            payload["top_p"] = top_p

        presence_penalty = self._optional_float_option(options, "presence_penalty")
        if presence_penalty is not None:
            payload["presence_penalty"] = presence_penalty

        extra_body: dict[str, Any] = {}
        top_k = self._optional_int_option(options, "top_k")
        if top_k is not None:
            extra_body["top_k"] = top_k

        min_p = self._optional_float_option(options, "min_p")
        if min_p is not None:
            extra_body["min_p"] = min_p

        repetition_penalty = self._optional_float_option(options, "repetition_penalty")
        if repetition_penalty is not None:
            extra_body["repetition_penalty"] = repetition_penalty

        thinking_token_budget = self._optional_int_option(options, "thinking_token_budget")
        if thinking_token_budget is not None:
            extra_body["thinking_token_budget"] = thinking_token_budget

        if self._is_qwen_model(self.settings.model):
            chat_template_kwargs: dict[str, Any] = {}
            enable_thinking = self._optional_bool_option(options, "enable_thinking")
            if enable_thinking is None:
                enable_thinking = True
            chat_template_kwargs["enable_thinking"] = enable_thinking

            preserve_thinking = self._optional_bool_option(options, "preserve_thinking")
            if preserve_thinking is not None:
                chat_template_kwargs["preserve_thinking"] = preserve_thinking

            # reasoning_effort = options.get("reasoning_effort")
            # if reasoning_effort and reasoning_effort in ("low", "medium", "high"):
            #     chat_template_kwargs["reasoning_effort"] = reasoning_effort

            extra_body["chat_template_kwargs"] = chat_template_kwargs

        if extra_body:
            payload["extra_body"] = extra_body

    @staticmethod
    def _float_option(options: dict[str, Any], key: str, default: float) -> float:
        value = options.get(key)
        if value is None or value == "":
            return default
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @classmethod
    def _optional_float_option(cls, options: dict[str, Any], key: str) -> float | None:
        if key not in options or options.get(key) in {None, ""}:
            return None
        return cls._float_option(options, key, 0.0)

    @staticmethod
    def _optional_int_option(options: dict[str, Any], key: str) -> int | None:
        if key not in options or options.get(key) in {None, ""}:
            return None
        try:
            return int(options[key])
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _optional_bool_option(options: dict[str, Any], key: str) -> bool | None:
        if key not in options or options.get(key) is None:
            return None
        value = options[key]
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"1", "true", "yes", "on"}:
                return True
            if lowered in {"0", "false", "no", "off"}:
                return False
        return bool(value)


    @staticmethod
    def _extract_delta_and_finish_reason(chunk: Any, chunk_raw: dict[str, Any]) -> tuple[Any, str | None]:
        choices = getattr(chunk, "choices", None) or chunk_raw.get("choices") or []
        if not choices:
            return {}, None
        choice = choices[0]
        delta = getattr(choice, "delta", None)
        finish_reason = getattr(choice, "finish_reason", None)
        if isinstance(choice, dict):
            delta = choice.get("delta", delta)
            finish_reason = choice.get("finish_reason", finish_reason)
        return delta or {}, finish_reason

    @staticmethod
    def _extract_raw_delta(chunk_raw: dict[str, Any]) -> dict[str, Any]:
        choices = chunk_raw.get("choices") or []
        if not choices:
            return {}
        choice = choices[0]
        if not isinstance(choice, dict):
            return {}
        delta = choice.get("delta")
        return delta if isinstance(delta, dict) else {}

    @classmethod
    def _merge_tool_call_deltas(cls, states: dict[int, dict[str, Any]], tool_calls: Any) -> None:
        if not tool_calls:
            return
        for idx, item in enumerate(tool_calls):
            index = cls._tool_call_index(item, idx)
            state = states.setdefault(index, {"id": "", "name": "", "arguments": []})
            item_id = cls._tool_call_id(item)
            if item_id:
                state["id"] = item_id
            name = cls._tool_call_name(item)
            if name:
                state["name"] = name
            arguments_delta = cls._tool_call_arguments(item)
            if arguments_delta:
                state["arguments"].append(arguments_delta)

    @staticmethod
    def _tool_call_index(item: Any, fallback: int) -> int:
        if isinstance(item, dict):
            value = item.get("index", fallback)
        else:
            value = getattr(item, "index", fallback)
        try:
            return int(value)
        except Exception:
            return fallback

    @staticmethod
    def _tool_call_id(item: Any) -> str:
        if isinstance(item, dict):
            return str(item.get("id") or "")
        return str(getattr(item, "id", "") or "")

    @staticmethod
    def _tool_call_name(item: Any) -> str:
        function = item.get("function") if isinstance(item, dict) else getattr(item, "function", None)
        if isinstance(function, dict):
            return str(function.get("name") or "")
        return str(getattr(function, "name", "") or "")

    @staticmethod
    def _tool_call_arguments(item: Any) -> str:
        function = item.get("function") if isinstance(item, dict) else getattr(item, "function", None)
        if isinstance(function, dict):
            return str(function.get("arguments") or "")
        return str(getattr(function, "arguments", "") or "")

    def _finalize_tool_calls(self, states: dict[int, dict[str, Any]]) -> list[LLMToolCall]:
        tool_calls: list[LLMToolCall] = []
        for _, state in sorted(states.items(), key=lambda item: item[0]):
            raw_arguments = "".join(state.get("arguments") or []).strip() or "{}"
            try:
                arguments = json.loads(raw_arguments)
            except json.JSONDecodeError:
                arguments = {"raw_arguments": raw_arguments}
            tool_calls.append(
                LLMToolCall(
                    id=str(state.get("id") or f"stream_tool_call_{next(self._tool_call_counter):04d}"),
                    name=str(state.get("name") or ""),
                    arguments=arguments if isinstance(arguments, dict) else {"raw_arguments": arguments},
                )
            )
        return [item for item in tool_calls if item.name]

    def _extract_structured_tool_calls(self, message: Any) -> list[LLMToolCall]:
        tool_calls: list[LLMToolCall] = []
        for item in getattr(message, "tool_calls", None) or []:
            raw_arguments = item.function.arguments or "{}"
            try:
                arguments = json.loads(raw_arguments)
            except json.JSONDecodeError:
                arguments = {"raw_arguments": raw_arguments}
            tool_calls.append(
                LLMToolCall(
                    id=item.id,
                    name=item.function.name,
                    arguments=arguments,
                )
            )
        return tool_calls

    def _extract_tool_calls_from_content(self, content: str) -> tuple[list[LLMToolCall], str]:
        tool_calls: list[LLMToolCall] = []
        cleaned_content = content

        pattern = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", flags=re.S | re.I)
        matches = list(pattern.finditer(content))

        if matches:
            for match in matches:
                raw_block = match.group(1).strip()
                try:
                    payload = json.loads(raw_block)
                except json.JSONDecodeError:
                    continue

                name = str(payload.get("name", "")).strip()
                arguments = payload.get("arguments", {})
                if not name:
                    continue
                if not isinstance(arguments, dict):
                    arguments = {"raw_arguments": arguments}

                tool_calls.append(
                    LLMToolCall(
                        id=f"text_tool_call_{next(self._tool_call_counter):04d}",
                        name=name,
                        arguments=arguments,
                    )
                )

            cleaned_content = pattern.sub("", cleaned_content).strip()
            return tool_calls, cleaned_content

        # 兜底1：尝试把 markdown json code fence 去掉
        stripped = content.strip()
        fence_match = re.match(r"^```(?:json)?\s*(.*?)\s*```$", stripped, flags=re.S | re.I)
        if fence_match:
            stripped = fence_match.group(1).strip()

        # 兜底2：尝试把整个 content 当作一个 JSON tool call
        try:
            payload = json.loads(stripped)
            name = str(payload.get("name", "")).strip()
            arguments = payload.get("arguments", {})
            if name:
                if not isinstance(arguments, dict):
                    arguments = {"raw_arguments": arguments}
                return [
                    LLMToolCall(
                        id=f"text_tool_call_{next(self._tool_call_counter):04d}",
                        name=name,
                        arguments=arguments,
                    )
                ], ""
        except Exception:
            pass

        return [], content



    @staticmethod
    def _is_qwen_model(model_name: str | None) -> bool:
        if not model_name:
            return False
        return "qwen" in model_name.lower()

    @staticmethod
    def _normalize_content(content: Any) -> str:
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(item.get("text", ""))
            return "".join(parts)
        return str(content)



    @staticmethod
    def _pending_tag_prefix(buffer: str, tag: str) -> int:
        """Length of a trailing fragment that may be the start of *tag*."""
        maximum = min(len(buffer), len(tag) - 1)
        lowered = buffer.lower()
        target = tag.lower()
        for size in range(maximum, 0, -1):
            if lowered[-size:] == target[:size]:
                return size
        return 0

    @classmethod
    def _demux_inline_thinking(
        cls,
        delta: str,
        state: dict[str, Any],
        *,
        final: bool = False,
    ) -> tuple[list[str], list[str]]:
        """Split providers that stream ``<think>`` inside the content channel.

        Tags may be split across arbitrary chunks.  The method keeps only the
        shortest possible tag prefix buffered, so both reasoning and final JSON
        remain genuinely live while never being mixed together.
        """
        state["buffer"] = str(state.get("buffer") or "") + str(delta or "")
        inside = bool(state.get("inside"))
        reasoning: list[str] = []
        content: list[str] = []
        open_tag = "<think>"
        close_tag = "</think>"

        while state["buffer"]:
            buffer = str(state["buffer"])
            lowered = buffer.lower()
            tag = close_tag if inside else open_tag
            index = lowered.find(tag)
            if index >= 0:
                piece = buffer[:index]
                if piece:
                    (reasoning if inside else content).append(piece)
                state["buffer"] = buffer[index + len(tag) :]
                inside = not inside
                state["inside"] = inside
                continue

            if final:
                (reasoning if inside else content).append(buffer)
                state["buffer"] = ""
                break

            hold = cls._pending_tag_prefix(buffer, tag)
            emit = buffer[:-hold] if hold else buffer
            if emit:
                (reasoning if inside else content).append(emit)
            state["buffer"] = buffer[-hold:] if hold else ""
            break

        state["inside"] = inside
        return reasoning, content

    @staticmethod
    def _strip_think_output_delta(content: str) -> str:
        if not content:
            return ""
        return re.sub(r"</?think>", "", content, flags=re.I)

    @staticmethod
    def _strip_think_output(content: str) -> str:
        if not content:
            return ""

        # 去掉...</think>思考过程
        content = re.sub(r"^.*?</think>", "", content, flags=re.S | re.I)

        return content.strip()

    @classmethod
    def _extract_reasoning(cls, *, message: Any, raw: dict[str, Any]) -> str:
        candidates = [
            getattr(message, "reasoning_content", None),
            getattr(message, "reasoning", None),
        ]
        raw_message = {}
        if raw.get("choices"):
            raw_message = raw["choices"][0].get("message", {})
        candidates.extend(
            [
                raw_message.get("reasoning_content"),
                raw_message.get("reasoning"),
            ]
        )
        for item in candidates:
            text = cls._normalize_content(item).strip()
            if text:
                return text
        return ""


class LocalWorkflowLLMClient:
    """A tiny deterministic fallback so the end-to-end workflow can run without cloud LLM access."""

    def __init__(self) -> None:
        self._counter = count(1)

    def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[ToolDefinition],
        temperature: float,
        generation_options: LLMGenerationOptions | None = None,
        stream_handler: LLMStreamHandler | None = None,
        cancel_checker: LLMCancelChecker | None = None,
    ) -> LLMResponse:
        if cancel_checker is not None and cancel_checker():
            raise LLMCancelledError("llm_generation_cancelled")
        user_messages = [self._normalize_user_message_content(item.get("content", "")) for item in messages if item.get("role") == "user"]
        trigger_source = next((item for item in user_messages if "触发上下文" in item), user_messages[0] if user_messages else "")
        live_user_content = user_messages[-1] if user_messages else ""
        trigger = self._extract_trigger(trigger_source)
        tool_messages = [item for item in messages if item.get("role") == "tool"]
        tool_results = [self._safe_json(str(item.get("content", ""))) for item in tool_messages]

        if trigger.get("事件触发"):
            return self._handle_event(trigger, tool_results)
        if trigger.get("聊天触发"):
            return self._handle_chat(trigger, tool_results, live_user_content or trigger_source)
        return LLMResponse(content="当前没有新的异常任务，平台已保持待命。", reasoning="未识别到触发上下文。")

    def _normalize_user_message_content(self, content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if not isinstance(item, dict):
                    parts.append(str(item))
                    continue
                item_type = str(item.get("type") or "")
                if item_type == "text":
                    parts.append(str(item.get("text") or ""))
                elif item_type == "image_url":
                    parts.append("[image]")
            return "\n".join(part for part in parts if part)
        return str(content)

    def _handle_event(self, trigger: dict[str, Any], tool_results: list[dict[str, Any]]) -> LLMResponse:
        payload = dict(trigger.get("事件触发") or {}).get("payload") or {}
        signal_id = str(payload.get("signal_id", "unknown_signal"))
        fingerprint = str(payload.get("fingerprint", "unknown"))
        features = dict(payload.get("features") or {})

        deep_result = self._find_tool_result(tool_results, lambda data: "analysis_score" in data and data.get("signal_id") == signal_id)
        case_result = self._find_tool_result(tool_results, lambda data: "case_id" in data and data.get("signal_id") == signal_id)

        if deep_result is None:
            return LLMResponse(
                content="已收到可疑信号，先读取工作区，再补充一次聚焦复核并执行深度分析。",
                reasoning="异常事件需要先完成补充观测和深度分析，随后再落 Case。",
                tool_calls=[
                    self._tool_call("query_recent_observations", {"limit": 6}),
                    self._tool_call("request_focused_collection", {"signal_id": signal_id, "sample_hint": 256}),
                    self._tool_call("request_deep_analysis", {"signal_id": signal_id}),
                ],
            )

        if case_result is None:
            abnormal = str(deep_result.get("verdict", "")).strip() not in {"倾向正常", "likely_benign"}
            status = "investigating" if abnormal else "closed"
            note = (
                f"本地自动复核完成：{deep_result.get('summary', '')}".strip()
                or f"信号 {signal_id} 已完成自动复核。"
            )
            return LLMResponse(
                content="深度分析已返回，正在把结论回写到 Case。",
                reasoning="需要把分析结论持久化到 Case，便于前端显示与后续人工补充。",
                tool_calls=[
                    self._tool_call(
                        "update_case",
                        {
                            "signal_id": signal_id,
                            "risk_level": str(deep_result.get("risk_level", "medium")),
                            "status": status,
                            "hypothesis": str(deep_result.get("hypothesis", "") or ""),
                            "note": note,
                            "next_actions": [
                                "等待人工复核补充",
                                "如再次触发则继续保留时频图和原始 NPZ 证据",
                            ],
                        },
                    )
                ],
            )

        final_result = str(deep_result.get("verdict", "待人工确认"))
        risk = str(deep_result.get("risk_level", "medium"))
        score = deep_result.get("analysis_score")
        summary = str(deep_result.get("summary", "") or "")
        concise = f"信号 {signal_id} 已完成自动研判：结论 {final_result}，风险 {risk}"
        if score is not None:
            concise += f"，评分 {score}"
        concise += f"，指纹 {fingerprint}。"
        if summary:
            concise += f" {summary}"
        if not features:
            concise += " 当前已将原始 NPZ 和解析结果统一写入主平台持久化状态。"
        return LLMResponse(content=concise, reasoning="工具链已经完成自动复核，可输出面向 UI 的简洁摘要。")

    def _handle_chat(self, trigger: dict[str, Any], tool_results: list[dict[str, Any]], user_content: str) -> LLMResponse:
        message = str((trigger.get("聊天触发") or {}).get("content", "") or "")
        signal_id_match = re.search(r"(sig_[0-9a-zA-Z_\-]+|real_[0-9a-zA-Z_\-]+)", message)
        signal_id = signal_id_match.group(1) if signal_id_match else None
        lowered = message.lower()

        autonomous_result = self._find_tool_result(tool_results, lambda data: "execution_result" in data and ("generated_code" in data or "task_plan" in data))
        if autonomous_result is None and any(keyword in message for keyword in ["USRP", "usrp", "频谱", "扫频", "全频段", "采集", "背景频谱", "正常频谱"]):
            return LLMResponse(
                content="已识别为复杂 USRP/频谱采集任务，正在调用代码生成型自主采集工具。",
                reasoning="用户要求通过智能体自主完成采集，需调用 run_autonomous_usrp_task 生成并执行受控采集函数，当前版本直接使用 USRP WebSocket FFT 数据。",
                tool_calls=[self._tool_call("run_autonomous_usrp_task", {"task_description": message})],
            )
        if autonomous_result is not None:
            execution = autonomous_result.get("execution_result") or {}
            output_file = execution.get("output_file", "")
            return LLMResponse(
                content=f"代码生成型自主采集任务已完成。输出文件：{output_file or '见工具执行结果'}；频点数：{execution.get('freq_count', '-') }；重复次数：{execution.get('repeat_count', '-') }。前端已展示检索知识、生成代码、安全检查、WebSocket FFT 接收和执行过程。",
                reasoning="自主采集工具已经返回执行结果，整理为用户可读摘要。",
            )

        feedback_result = self._find_tool_result(tool_results, lambda data: data.get("signal_id") == signal_id and "verdict" in data)
        if signal_id and feedback_result is None and any(keyword in lowered for keyword in ["误报", "确认", "排除", "dismiss", "confirm"]):
            verdict = "confirmed" if any(keyword in lowered for keyword in ["确认", "confirm"]) else "dismissed"
            return LLMResponse(
                content="已识别为正式操作员反馈，正在写入任务时间线。",
                reasoning="聊天中出现明确的人工判定，需要记录为正式反馈事件。",
                tool_calls=[self._tool_call("record_operator_feedback", {"signal_id": signal_id, "verdict": verdict, "note": message})],
            )

        if "状态" in message or "进度" in message or "最近" in message:
            return LLMResponse(
                content="我已接入主平台的任务态。当前聊天、Case、实时观测和 NPZ 解析结果都统一落在 SQLite 中；若出现告警，系统会自动升级为事件并进入智能体复核流程。",
                reasoning="给出基于当前平台设计的简要状态说明即可。",
            )

        return LLMResponse(
            content="已收到。你可以继续询问最近的可疑信号、某个 signal_id 的结论，或直接在聊天中给出人工确认/误报反馈。",
            reasoning="普通操作员聊天，返回简短引导。",
        )

    def _tool_call(self, name: str, arguments: dict[str, Any]) -> LLMToolCall:
        return LLMToolCall(id=f"local_tool_{next(self._counter):04d}", name=name, arguments=arguments)

    @staticmethod
    def _extract_trigger(user_content: str) -> dict[str, Any]:
        marker = "触发上下文："
        workspace_marker = "\n\n工作区快照："
        start = user_content.find(marker)
        if start < 0:
            return {}
        start += len(marker)
        end = user_content.find(workspace_marker, start)
        payload = user_content[start:end if end >= 0 else None].strip()
        try:
            return json.loads(payload)
        except Exception:
            return {}

    @staticmethod
    def _safe_json(content: str) -> dict[str, Any]:
        try:
            parsed = json.loads(content)
        except Exception:
            return {}
        data = parsed.get("data")
        return dict(data) if isinstance(data, dict) else {}

    @staticmethod
    def _find_tool_result(tool_results: list[dict[str, Any]], predicate) -> dict[str, Any] | None:
        for item in reversed(tool_results):
            if item and predicate(item):
                return item
        return None


class ScriptedLLMClient:
    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = deque(responses)
        self.requests: list[dict[str, Any]] = []

    def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[ToolDefinition],
        temperature: float,
        generation_options: LLMGenerationOptions | None = None,
        stream_handler: LLMStreamHandler | None = None,
        cancel_checker: LLMCancelChecker | None = None,
    ) -> LLMResponse:
        if cancel_checker is not None and cancel_checker():
            raise LLMCancelledError("llm_generation_cancelled")
        self.requests.append(
            {
                "messages": messages,
                "tools": [item.name for item in tools],
                "temperature": temperature,
                "generation_options": dict(generation_options or {}),
            }
        )
        if not self._responses:
            raise RuntimeError("ScriptedLLMClient has no more scripted responses.")
        return self._responses.popleft()
