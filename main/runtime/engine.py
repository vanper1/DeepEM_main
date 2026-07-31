from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Any

from deepem.agent.llm import LLMCancelledError, is_context_length_error
from deepem.agent.profiles import GENERAL_QA_AGENT, PLACE_DETECTION_AGENT, TASK_CHAT_AGENT
from deepem.agent.context_budget import ContextBudgetEstimator, PromptCompositionEstimator
from deepem.agent.context_compressor import AGGRESSIVE_CONTEXT_COMPRESSION, NORMAL_CONTEXT_COMPRESSION, ContextCompressionOptions, ConversationContextCompressor
from deepem.agent.context_reducer import (
    ContextReductionPolicy,
    HistoryUnit,
    ReducerSummaryState,
    build_history_units,
    flatten_history_units,
    history_units_fingerprint,
    is_ordered_coverage_prefix,
    select_head_and_tail_units,
    select_tail_units,
)
from deepem.agent.tool_result_compressor import ToolResultCompressor, ToolTranscriptCompressor
from deepem.nl2sql_config import NL2SQLSessionConfig
from deepem.protocol import (
    ChatMessage,
    ChatRole,
    Event,
    Part,
    PartKind,
    Run,
    RunStatus,
    RunTriggerKind,
    TaskStatus,
    ToolCall,
    ToolCallStatus,
    ToolResult,
    new_id,
    utc_now,
)
from deepem.runtime.context import RuntimeContext
from deepem.runtime.chat_mode import ChatMode, build_execution_policy, effective_llm_options, project_general_history
from deepem.tools.base import ToolContext
from deepem.upload_processing import encode_image_as_data_url

StreamHandler = Callable[[str, dict[str, Any]], None]
CancelChecker = Callable[[], bool]


@dataclass(slots=True)
class RunEngine:
    context: RuntimeContext
    _emitted_context_steps: set[tuple[str, str]] | None = None
    _emitted_context_budget_rank: dict[str, int] | None = None

    def run_bootstrap(self, task_id: str) -> Run:
        return self._execute(task_id=task_id, trigger_kind=RunTriggerKind.BOOTSTRAP, trigger_event=None, trigger_message=None)

    def run_event(self, task_id: str, event_id: str) -> Run:
        event = self.context.event_repo.get(event_id)
        return self._execute(task_id=task_id, trigger_kind=RunTriggerKind.EVENT, trigger_event=event, trigger_message=None)

    def run_event_with_cancel(self, task_id: str, event_id: str, *, cancel_checker: CancelChecker | None = None) -> Run:
        event = self.context.event_repo.get(event_id)
        return self._execute(task_id=task_id, trigger_kind=RunTriggerKind.EVENT, trigger_event=event, trigger_message=None, cancel_checker=cancel_checker)

    def run_chat(
        self,
        task_id: str,
        message_id: str,
        *,
        conversation_id: str | None = None,
        event_handler: StreamHandler | None = None,
        nl2sql_options: NL2SQLSessionConfig | None = None,
        llm_options: Mapping[str, Any] | None = None,
        chat_mode: ChatMode | str = ChatMode.WORKSPACE,
        persist_assistant_message: bool = True,
        cancel_checker: CancelChecker | None = None,
    ) -> ChatMessage | None:
        conversation = self.context.conversation_repo.get(conversation_id) if conversation_id else self.context.conversation_repo.get_by_task(task_id)
        messages = self.context.chat_repo.list_by_conversation(conversation.id)
        trigger_message = next(item for item in reversed(messages) if item.id == message_id)
        run = self._execute(
            task_id=task_id,
            trigger_kind=RunTriggerKind.CHAT,
            trigger_event=None,
            trigger_message=trigger_message,
            conversation_id=conversation.id,
            event_handler=event_handler,
            nl2sql_options=nl2sql_options or NL2SQLSessionConfig(),
            llm_options=llm_options,
            chat_mode=chat_mode,
            persist_assistant_message=persist_assistant_message,
            cancel_checker=cancel_checker,
        )
        if run.summary is None:
            return None
        if not persist_assistant_message:
            return ChatMessage(
                id=new_id("msg"),
                conversation_id=conversation.id,
                task_id=task_id,
                role=ChatRole.ASSISTANT,
                content=run.summary,
                run_id=run.id,
                created_at=utc_now(),
                metadata={"chat_mode": ChatMode(chat_mode).value},
            )
        messages = self.context.chat_repo.list_by_conversation(conversation.id)
        for item in reversed(messages):
            if item.role == ChatRole.ASSISTANT and item.run_id == run.id:
                return item
        return None

    def _execute(
        self,
        *,
        task_id: str,
        trigger_kind: RunTriggerKind,
        trigger_event: Event | None,
        trigger_message: ChatMessage | None,
        conversation_id: str | None = None,
        event_handler: StreamHandler | None = None,
        nl2sql_options: NL2SQLSessionConfig | None = None,
        llm_options: Mapping[str, Any] | None = None,
        chat_mode: ChatMode | str = ChatMode.WORKSPACE,
        persist_assistant_message: bool = True,
        cancel_checker: CancelChecker | None = None,
    ) -> Run:
        nl2sql_options = nl2sql_options or NL2SQLSessionConfig()
        chat_mode = ChatMode(chat_mode)
        llm_options = effective_llm_options(chat_mode, llm_options)
        task = self.context.task_repo.get(task_id)
        profile = self._select_profile(trigger_kind, chat_mode=chat_mode)
        conversation = self.context.conversation_repo.get(conversation_id) if conversation_id else self.context.conversation_repo.get_by_task(task_id)
        run = Run(
            id=new_id("run"),
            task_id=task_id,
            trigger_kind=trigger_kind,
            trigger_event_id=trigger_event.id if trigger_event else None,
            trigger_message_id=trigger_message.id if trigger_message else None,
            agent_profile=profile.name,
            status=RunStatus.RUNNING,
            step_budget=profile.step_budget,
            step_count=0,
            started_at=utc_now(),
            conversation_id=conversation.id,
        )
        self.context.run_repo.create(run)
        tool_specs = self.context.tool_registry.specs(profile.allowed_tools)
        history_message_count = 0
        if chat_mode is ChatMode.GENERAL and trigger_message is not None:
            conversation_messages = self.context.chat_repo.list_by_conversation(conversation.id)
            trigger_index = next(
                (index for index, message in enumerate(conversation_messages) if message.id == trigger_message.id),
                len(conversation_messages),
            )
            history_message_count = len(project_general_history(conversation_messages[:trigger_index]))
        execution_policy = build_execution_policy(
            chat_mode=chat_mode,
            available_tool_count=len(tool_specs),
            effective_options=llm_options,
            history_message_count=history_message_count,
        )
        policy_payload = {"run_id": run.id, **execution_policy.to_event()}
        self._emit(event_handler, "execution_policy", policy_payload)
        self._debug_log(run_id=run.id, stage="execution_policy", payload=policy_payload)
        self._debug_log(
            run_id=run.id,
            stage="run_started",
            payload={
                "task": task,
                "run": run,
                "trigger_kind": trigger_kind,
                "trigger_event": trigger_event,
                "trigger_message": trigger_message,
                "profile": {
                    "name": profile.name,
                    "temperature": profile.temperature,
                    "step_budget": profile.step_budget,
                    "allowed_tools": list(profile.allowed_tools),
                },
                "nl2sql_options": nl2sql_options.to_prompt_payload(),
                "llm_options": llm_options,
            },
        )
        if trigger_event:
            observation_part = Part(
                id=new_id("part"),
                task_id=task_id,
                run_id=run.id,
                kind=PartKind.OBSERVATION,
                content=f"已消费事件：{trigger_event.event_type}",
                created_at=utc_now(),
                event_id=trigger_event.id,
                metadata={"event_payload": trigger_event.payload},
                conversation_id=run.conversation_id,
            )
            self.context.part_repo.append(observation_part)
            self._emit(
                event_handler,
                "observation",
                {
                    "run_id": run.id,
                    "text": f"已接收事件：{trigger_event.event_type}",
                },
            )

        transcript_messages: list[dict[str, Any]] = []
        assistant_reply: str | None = None
        empty_response_repair_attempted = False
        pending_empty_response_repair = False
        normal_step_count = run.step_count
        try:
            if cancel_checker and cancel_checker():
                return self._abort_run(run=run, task=task, event_handler=event_handler, reason="cancelled_before_execution")
            if (
                chat_mode is ChatMode.WORKSPACE
                and trigger_kind == RunTriggerKind.CHAT
                and trigger_message
                and nl2sql_options.force_enabled
            ):
                forced_invocation_id = f"forced_nl2sql_{run.id}"
                transcript_messages.append(
                    {
                        "role": "assistant",
                        "content": "根据当前会话配置，先强制执行一次 NL2SQL 查询工具。",
                        "tool_calls": [
                            {
                                "id": forced_invocation_id,
                                "type": "function",
                                "function": {
                                    "name": "query_local_database",
                                    "arguments": json.dumps({"question": trigger_message.content}, ensure_ascii=False),
                                },
                            }
                        ],
                    }
                )
                transcript_messages.append(
                    self._execute_tool_call(
                        task=task,
                        run=run,
                        trigger_event=trigger_event,
                        trigger_message=trigger_message,
                        invocation_id=forced_invocation_id,
                        tool_name="query_local_database",
                        arguments={"question": trigger_message.content},
                        event_handler=event_handler,
                        nl2sql_options=nl2sql_options,
                        cancel_checker=cancel_checker,
                    )
                )
            while normal_step_count < run.step_budget or pending_empty_response_repair:
                if cancel_checker and cancel_checker():
                    return self._abort_run(run=run, task=task, event_handler=event_handler, reason="cancelled_before_step")
                repair_request = pending_empty_response_repair
                pending_empty_response_repair = False
                repair_messages: list[dict[str, Any]] = []
                if repair_request:
                    repair_messages.append(
                        {
                            "role": "system",
                            "content": (
                                "上一轮没有生成可见答案或正式工具调用。请重新处理当前用户请求，并且只返回以下一种结果："
                                "通过结构化 function calling 发出正式工具调用，或在 content 中直接输出最终答案。"
                                "不要输出 XML、伪工具标签、调用示例或思考过程。"
                            ),
                        }
                    )
                messages = self._build_messages(
                    profile=profile,
                    task=task,
                    run=run,
                    trigger_event=trigger_event,
                    trigger_message=trigger_message,
                    conversation_id=conversation.id,
                    transcript_messages=transcript_messages,
                    nl2sql_options=nl2sql_options,
                    event_handler=event_handler,
                    ephemeral_messages=repair_messages,
                    tool_specs=tool_specs,
                    chat_mode=chat_mode,
                )
                active_llm_options = llm_options
                if repair_request:
                    active_llm_options = dict(llm_options)
                    active_llm_options.update({"enable_thinking": False, "preserve_thinking": False})
                llm_stream_state = {"content_started": False, "reasoning_started": False}

                def llm_stream_handler(event_type: str, payload: dict[str, Any]) -> None:
                    if event_type == "reasoning_start":
                        llm_stream_state["reasoning_started"] = True
                        self._emit(event_handler, "reasoning_start", {"run_id": run.id})
                        # 兼容旧前端或旧事件映射：同时补发一个空 reasoning 事件壳，不带文本时前端应忽略。
                        self._emit(event_handler, "assistant_reasoning_start", {"run_id": run.id})
                        return
                    if event_type == "reasoning_delta":
                        delta = self._user_facing_reasoning(str(payload.get("delta") or ""))
                        if delta:
                            reasoning_payload = {"run_id": run.id, "delta": delta, "text": delta}
                            self._emit(event_handler, "reasoning_delta", reasoning_payload)
                        return
                    if event_type == "reasoning_done":
                        self._emit(event_handler, "reasoning_done", {"run_id": run.id})
                        self._emit(event_handler, "assistant_reasoning_done", {"run_id": run.id})
                        return
                    if event_type == "content_start":
                        llm_stream_state["content_started"] = True
                        self._emit(event_handler, "final_answer_start", {"run_id": run.id, "provisional": True})
                        return
                    if event_type == "content_delta":
                        delta = str(payload.get("delta") or "")
                        if delta:
                            self._emit(event_handler, "final_answer_delta", {"run_id": run.id, "delta": delta, "provisional": True})
                        return


                response = self._complete_with_context_retry(
                    profile=profile,
                    task=task,
                    run=run,
                    trigger_event=trigger_event,
                    trigger_message=trigger_message,
                    conversation_id=conversation.id,
                    transcript_messages=transcript_messages,
                    nl2sql_options=nl2sql_options,
                    event_handler=event_handler,
                    cancel_checker=cancel_checker,
                    llm_options=active_llm_options,
                    messages=messages,
                    tool_specs=tool_specs,
                    stream_handler=llm_stream_handler,
                    llm_stream_state=llm_stream_state,
                    ephemeral_messages=repair_messages,
                    chat_mode=chat_mode,
                )
                if chat_mode is ChatMode.GENERAL and response.tool_calls:
                    response = replace(response, tool_calls=[])
                self._debug_log(
                    run_id=run.id,
                    stage="llm_response",
                    payload={
                        "step_index": run.step_count + 1,
                        "content": response.content,
                        "reasoning": response.reasoning,
                        "tool_calls": response.tool_calls,
                        "raw": response.raw,
                    },
                )
                run.step_count += 1
                if not repair_request:
                    normal_step_count += 1
                self.context.run_repo.save(run)
                empty_response = not response.tool_calls and not (response.content or "").strip()
                if not empty_response:
                    self._record_reasoning_part(
                        task_id=task_id,
                        run_id=run.id,
                        content=response.reasoning,
                        trigger_event=trigger_event,
                        trigger_message=trigger_message,
                    )

                if empty_response:
                    if not empty_response_repair_attempted:
                        empty_response_repair_attempted = True
                        pending_empty_response_repair = True
                        repair_payload = {"run_id": run.id, "status": "scheduled", "attempt": 1}
                        self._emit(event_handler, "empty_response_repair", repair_payload)
                        self._debug_log(run_id=run.id, stage="empty_response_repair_scheduled", payload=repair_payload)
                        continue
                    assistant_reply = "模型未生成有效回复，请重试。"
                    self._debug_log(
                        run_id=run.id,
                        stage="empty_response_after_repair",
                        payload={"run_id": run.id, "attempt": 1},
                    )
                elif repair_request:
                    self._debug_log(
                        run_id=run.id,
                        stage="empty_response_repair_succeeded",
                        payload={
                            "run_id": run.id,
                            "result_type": "tool_call" if response.tool_calls else "content",
                        },
                    )
                if response.reasoning.strip() and not response.raw.get("chunks"):
                    self._emit(
                        event_handler,
                        "reasoning",
                        {
                            "run_id": run.id,
                            "text": self._user_facing_reasoning(response.reasoning),
                        },
                    )

                if cancel_checker and cancel_checker():
                    return self._abort_run(run=run, task=task, event_handler=event_handler, reason="cancelled_after_llm")

                if response.tool_calls:
                    if llm_stream_state.get("content_started"):
                        self._emit(event_handler, "final_answer_discard", {"run_id": run.id})
                    assistant_message = {"role": "assistant", "content": response.content or "", "tool_calls": []}
                    for item in response.tool_calls:
                        assistant_message["tool_calls"].append(
                            {
                                "id": item.id,
                                "type": "function",
                                "function": {
                                    "name": item.name,
                                    "arguments": json.dumps(item.arguments, ensure_ascii=False),
                                },
                            }
                        )
                    transcript_messages.append(assistant_message)
                    self._record_text_part(
                        task_id=task_id,
                        run_id=run.id,
                        content=response.content,
                        trigger_event=trigger_event,
                        trigger_message=trigger_message,
                    )
                    for tool_invocation in response.tool_calls:
                        if cancel_checker and cancel_checker():
                            return self._abort_run(run=run, task=task, event_handler=event_handler, reason="cancelled_before_tool")
                        tool_message = self._execute_tool_call(
                            task=task,
                            run=run,
                            trigger_event=trigger_event,
                            trigger_message=trigger_message,
                            invocation_id=tool_invocation.id,
                            tool_name=tool_invocation.name,
                            arguments=tool_invocation.arguments,
                            event_handler=event_handler,
                            nl2sql_options=nl2sql_options,
                            cancel_checker=cancel_checker,
                        )
                        transcript_messages.append(tool_message)
                    continue

                assistant_reply = assistant_reply or (response.content or "").strip()
                if assistant_reply and not llm_stream_state.get("content_started"):
                    self._emit(event_handler, "final_answer_start", {"run_id": run.id, "provisional": False})
                    self._emit(event_handler, "final_answer_delta", {"run_id": run.id, "delta": assistant_reply, "provisional": False})
                if cancel_checker and cancel_checker():
                    return self._abort_run(run=run, task=task, event_handler=event_handler, reason="cancelled_before_reply")
                self._record_text_part(
                    task_id=task_id,
                    run_id=run.id,
                    content=assistant_reply,
                    trigger_event=trigger_event,
                    trigger_message=trigger_message,
                )
                if assistant_reply and trigger_kind == RunTriggerKind.CHAT and persist_assistant_message:
                    assistant_message = ChatMessage(
                        id=new_id("msg"),
                        conversation_id=conversation.id,
                        task_id=task_id,
                        role=ChatRole.ASSISTANT,
                        content=assistant_reply,
                        run_id=run.id,
                        created_at=utc_now(),
                        metadata={"chat_mode": chat_mode.value},
                    )
                    self.context.chat_repo.append(assistant_message)
                    conversation.updated_at = assistant_message.created_at
                    self.context.conversation_repo.save(conversation)
                run.status = RunStatus.COMPLETED
                run.ended_at = utc_now()
                run.stop_reason = "empty_response_after_repair" if empty_response else "assistant_response"
                run.summary = assistant_reply
                self.context.run_repo.save(run)
                task.status = TaskStatus.RUNNING
                self.context.task_repo.save(task)
                self._debug_log(
                    run_id=run.id,
                    stage="run_completed",
                    payload={"run": run, "task": task, "assistant_reply": assistant_reply},
                )
                return run

            run.status = RunStatus.COMPLETED
            run.ended_at = utc_now()
            run.stop_reason = "step_budget_exhausted"
            run.summary = assistant_reply
            self.context.run_repo.save(run)
            task.status = TaskStatus.RUNNING
            self.context.task_repo.save(task)
            self._debug_log(
                run_id=run.id,
                stage="run_completed",
                payload={"run": run, "task": task, "assistant_reply": assistant_reply, "stop_reason": "step_budget_exhausted"},
            )
            return run
        except LLMCancelledError as exc:
            return self._abort_run(run=run, task=task, event_handler=event_handler, reason=str(exc) or "llm_generation_cancelled")
        except Exception as exc:
            run.status = RunStatus.FAILED
            run.ended_at = utc_now()
            run.stop_reason = str(exc)
            self.context.run_repo.save(run)
            task.status = TaskStatus.FAILED
            self.context.task_repo.save(task)
            self._debug_log(
                run_id=run.id,
                stage="run_failed",
                payload={"run": run, "task": task, "error": str(exc)},
            )
            self._emit(event_handler, "error", {"run_id": run.id, "message": str(exc)})
            raise

    def _abort_run(self, *, run: Run, task, event_handler: StreamHandler | None, reason: str) -> Run:
        run.status = RunStatus.ABORTED
        run.ended_at = utc_now()
        run.stop_reason = reason
        run.summary = None
        self.context.run_repo.save(run)
        task.status = TaskStatus.RUNNING
        self.context.task_repo.save(task)
        self._debug_log(
            run_id=run.id,
            stage="run_aborted",
            payload={"run": run, "task": task, "reason": reason},
        )
        return run

    def _complete_with_context_retry(
        self,
        *,
        profile,
        task,
        run: Run,
        trigger_event: Event | None,
        trigger_message: ChatMessage | None,
        conversation_id: str,
        transcript_messages: list[dict[str, Any]],
        nl2sql_options: NL2SQLSessionConfig,
        event_handler: StreamHandler | None,
        cancel_checker: CancelChecker | None,
        llm_options: Mapping[str, Any],
        messages: list[dict[str, Any]],
        tool_specs,
        stream_handler: StreamHandler,
        llm_stream_state: dict[str, bool],
        ephemeral_messages: Sequence[dict[str, Any]] = (),
        chat_mode: ChatMode = ChatMode.WORKSPACE,
    ):
        step_index = run.step_count + 1
        self._log_llm_request(
            run=run,
            step_index=step_index,
            messages=messages,
            tool_specs=tool_specs,
            temperature=profile.temperature,
            llm_options=llm_options,
            trigger_event=trigger_event,
            trigger_message=trigger_message,
        )
        try:
            return self.context.llm_client.complete(
                messages=messages,
                tools=tool_specs,
                temperature=profile.temperature,
                generation_options=llm_options,
                stream_handler=stream_handler,
                cancel_checker=cancel_checker,
            )
        except LLMCancelledError:
            raise
        except Exception as exc:
            if llm_stream_state.get("content_started") or llm_stream_state.get("reasoning_started") or not is_context_length_error(exc):
                raise

        self._emit_context_step_once(
            event_handler,
            "context_retry",
            {
                "run_id": run.id,
                "reason": "context_length_exceeded",
                "retry_mode": "aggressive",
                "attempt": 1,
            },
            run_id=run.id,
        )
        retry_messages = self._build_messages(
            profile=profile,
            task=task,
            run=run,
            trigger_event=trigger_event,
            trigger_message=trigger_message,
            conversation_id=conversation_id,
            transcript_messages=transcript_messages,
            nl2sql_options=nl2sql_options,
            event_handler=event_handler,
            force_aggressive_context=True,
            context_retry_attempt=1,
            ephemeral_messages=ephemeral_messages,
            tool_specs=tool_specs,
            chat_mode=chat_mode,
        )
        self._log_llm_request(
            run=run,
            step_index=step_index,
            messages=retry_messages,
            tool_specs=tool_specs,
            temperature=profile.temperature,
            llm_options=llm_options,
            trigger_event=trigger_event,
            trigger_message=trigger_message,
            context_retry_attempt=1,
            context_retry_reason="context_length_exceeded",
        )
        return self.context.llm_client.complete(
            messages=retry_messages,
            tools=tool_specs,
            temperature=profile.temperature,
            generation_options=llm_options,
            stream_handler=stream_handler,
            cancel_checker=cancel_checker,
        )

    def _log_llm_request(
        self,
        *,
        run: Run,
        step_index: int,
        messages: list[dict[str, Any]],
        tool_specs,
        temperature: float,
        llm_options: Mapping[str, Any],
        trigger_event: Event | None,
        trigger_message: ChatMessage | None,
        context_retry_attempt: int | None = None,
        context_retry_reason: str | None = None,
    ) -> None:
        payload = {
            "step_index": step_index,
            "messages": messages,
            "tools": tool_specs,
            "temperature": temperature,
            "llm_options": llm_options,
            "trigger_event": trigger_event,
            "trigger_message": trigger_message,
        }
        if context_retry_attempt is not None:
            payload["context_retry_attempt"] = context_retry_attempt
            payload["context_retry_reason"] = context_retry_reason
        self._debug_log(run_id=run.id, stage="llm_request", payload=payload)

    def _select_profile(self, trigger_kind: RunTriggerKind, *, chat_mode: ChatMode = ChatMode.WORKSPACE):
        if trigger_kind == RunTriggerKind.CHAT:
            if chat_mode is ChatMode.GENERAL:
                return self.context.profiles.get(GENERAL_QA_AGENT.name, GENERAL_QA_AGENT)
            return self.context.profiles.get(TASK_CHAT_AGENT.name, TASK_CHAT_AGENT)
        return self.context.profiles.get(PLACE_DETECTION_AGENT.name, PLACE_DETECTION_AGENT)

    def _execute_tool_call(
        self,
        *,
        task,
        run: Run,
        trigger_event: Event | None,
        trigger_message: ChatMessage | None,
        invocation_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        event_handler: StreamHandler | None = None,
        nl2sql_options: NL2SQLSessionConfig | None = None,
        cancel_checker: CancelChecker | None = None,
    ) -> dict[str, Any]:
        if cancel_checker and cancel_checker():
            raise LLMCancelledError("cancelled_before_tool")
        nl2sql_options = nl2sql_options or NL2SQLSessionConfig()
        tool_call = ToolCall(
            id=new_id("tool"),
            task_id=task.id,
            run_id=run.id,
            tool_name=tool_name,
            input=arguments,
            status=ToolCallStatus.RUNNING,
            started_at=utc_now(),
            conversation_id=run.conversation_id,
        )
        self.context.tool_call_repo.create(tool_call)
        self.context.part_repo.append(
            Part(
                id=new_id("part"),
                task_id=task.id,
                run_id=run.id,
                kind=PartKind.TOOL_CALL,
                content=f"{tool_name}({json.dumps(arguments, ensure_ascii=False)})",
                created_at=utc_now(),
                event_id=trigger_event.id if trigger_event else None,
                message_id=trigger_message.id if trigger_message else None,
                tool_call_id=tool_call.id,
                conversation_id=run.conversation_id,
            )
        )
        self._emit(
            event_handler,
            "tool_call",
            {
                "run_id": run.id,
                "tool_name": tool_name,
                "arguments": self._compact_data(arguments),
                "text": f"调用工具：{tool_name} → 参数 {self._compact_json(arguments)}",
            },
        )

        self._debug_log(
            run_id=run.id,
            stage="tool_call_started",
            payload={
                "task": task,
                "run": run,
                "trigger_event": trigger_event,
                "trigger_message": trigger_message,
                "invocation_id": invocation_id,
                "tool_call": tool_call,
                "arguments": arguments,
            },
        )

        try:
            outcome = self.context.tool_registry.execute(
                tool_name,
                arguments,
                ToolContext(
                    task=task,
                    run=run,
                    trigger_event=trigger_event,
                    trigger_message=trigger_message,
                    state_repo=self.context.state_repo,
                    case_repo=self.context.case_repo,
                    knowledge_base=self.context.knowledge_base,
                    device_registry=self.context.device_registry,
                    tool_call_repo=self.context.tool_call_repo,
                    llm_client=self.context.llm_client,
                    stream_handler=event_handler,
                    nl2sql_options=nl2sql_options,
                    asset_manager=self.context.asset_manager,
                    document_index=self.context.document_index,
                    database_catalog=self.context.database_catalog,
                    cancel_checker=cancel_checker,
                ),
            )
            tool_result = ToolResult(
                status=outcome.result.status,
                data=dict(outcome.result.data),
                attachments=list(outcome.result.attachments),
                emitted_event_ids=[],
                error=outcome.result.error,
                metadata=dict(outcome.result.metadata),
            )
            self._debug_log(
                run_id=run.id,
                stage="tool_call_outcome",
                payload={
                    "invocation_id": invocation_id,
                    "tool_name": tool_name,
                    "arguments": arguments,
                    "result": outcome.result,
                    "event_drafts": outcome.event_drafts,
                },
            )
            for draft in outcome.event_drafts:
                if draft.idempotency_key and self.context.event_repo.exists_by_idempotency_key(task.id, draft.idempotency_key):
                    existing = next(
                        item for item in reversed(self.context.event_repo.list_by_task(task.id)) if item.idempotency_key == draft.idempotency_key
                    )
                    tool_result.emitted_event_ids.append(existing.id)
                    continue
                event = Event(
                    id=new_id("evt"),
                    task_id=task.id,
                    seq=self.context.event_repo.next_seq(task.id),
                    event_type=draft.event_type,
                    source=draft.source,
                    payload=draft.payload,
                    occurred_at=draft.occurred_at,
                    recorded_at=utc_now(),
                    causation_tool_call_id=tool_call.id,
                    idempotency_key=draft.idempotency_key,
                    evidence_refs=list(draft.evidence_refs),
                    conversation_id=run.conversation_id,
                )
                self.context.event_repo.append(event)
                self.context.projector.apply(event)
                tool_result.emitted_event_ids.append(event.id)
        except Exception as exc:
            self._debug_log(
                run_id=run.id,
                stage="tool_call_exception",
                payload={
                    "invocation_id": invocation_id,
                    "tool_name": tool_name,
                    "arguments": arguments,
                    "error": str(exc),
                },
            )
            tool_result = ToolResult(status="error", error=str(exc))
            self.context.part_repo.append(
                Part(
                    id=new_id("part"),
                    task_id=task.id,
                    run_id=run.id,
                    kind=PartKind.OBSERVATION,
                    content=f"工具 {tool_name} 执行失败：{exc}",
                    created_at=utc_now(),
                    event_id=trigger_event.id if trigger_event else None,
                    message_id=trigger_message.id if trigger_message else None,
                    tool_call_id=tool_call.id,
                    metadata={"tool_name": tool_name, "arguments": arguments},
                    conversation_id=run.conversation_id,
                )
            )

        tool_call.status = ToolCallStatus.ERROR if tool_result.error else ToolCallStatus.COMPLETED
        tool_call.ended_at = utc_now()
        tool_call.result = tool_result
        self.context.tool_call_repo.save(tool_call)
        compact_result = ToolResultCompressor().compress(
            tool_name=tool_name,
            arguments=arguments,
            tool_result=tool_result,
            tool_call_id=tool_call.id,
            preserve_retrieved_detail=(tool_name == "retrieve_tool_result_detail"),
        )
        self._debug_log(
            run_id=run.id,
            stage="tool_call_finished",
            payload={
                "invocation_id": invocation_id,
                "tool_name": tool_name,
                "arguments": arguments,
                "tool_call": tool_call,
                "tool_result": tool_result,
            },
        )
        self._emit(
            event_handler,
            "observation",
            {
                "run_id": run.id,
                "tool_name": tool_name,
                "status": tool_result.status,
                "data": self._compact_data(tool_result.data),
                "compression": compact_result.get("compression"),
                "text": self._tool_result_summary(tool_name=tool_name, tool_result=tool_result),
            },
        )
        display_tool_names = {
            "query_local_database",
            "query_uploaded_documents",
            "retrieve_usrp_api_knowledge",
            "generate_usrp_task_code",
            "execute_usrp_task_code",
            "run_autonomous_usrp_task",
        }
        if (tool_name in display_tool_names or tool_result.metadata.get("display_in_chat")) and not tool_result.error:
            self._emit(
                event_handler,
                "tool_result",
                {
                    "run_id": run.id,
                    "tool_name": tool_name,
                    "status": tool_result.status,
                    "data": tool_result.data,
                    "compression": compact_result.get("compression"),
                },
            )
        if tool_name == "update_case":
            self._emit(
                event_handler,
                "case_update",
                {
                    "run_id": run.id,
                    "signal_id": str(arguments.get("signal_id", "-")),
                    "status": str(arguments.get("status") or tool_result.data.get("status") or "-"),
                    "risk_level": str(arguments.get("risk_level", "-")),
                    "text": self._case_update_summary(arguments=arguments, tool_result=tool_result),
                },
            )
        content = json.dumps(compact_result, ensure_ascii=False)
        return {"role": "tool", "tool_call_id": invocation_id, "content": content}

    def _build_messages(
        self,
        *,
        profile,
        task,
        run: Run,
        trigger_event: Event | None,
        trigger_message: ChatMessage | None,
        conversation_id: str,
        transcript_messages: list[dict[str, Any]],
        nl2sql_options: NL2SQLSessionConfig | None = None,
        event_handler: StreamHandler | None = None,
        force_aggressive_context: bool = False,
        context_retry_attempt: int | None = None,
        ephemeral_messages: Sequence[dict[str, Any]] = (),
        tool_specs=None,
        chat_mode: ChatMode = ChatMode.WORKSPACE,
    ) -> list[dict[str, Any]]:
        budget_estimator = ContextBudgetEstimator()
        if chat_mode is ChatMode.GENERAL:
            return self._build_general_messages(
                profile=profile,
                run=run,
                trigger_message=trigger_message,
                conversation_id=conversation_id,
                transcript_messages=transcript_messages,
                event_handler=event_handler,
                context_retry_attempt=context_retry_attempt,
                ephemeral_messages=ephemeral_messages,
                budget_estimator=budget_estimator,
            )
        state = self._session_scoped_state(self.context.state_repo.get(task.id), conversation_id)
        cases = [item for item in self.context.case_repo.list_by_task(task.id) if item.conversation_id == conversation_id]
        all_chat_messages = self.context.chat_repo.list_by_conversation(conversation_id)
        recent_events = [item for item in self.context.event_repo.list_by_task(task.id) if item.conversation_id == conversation_id][-8:]
        recent_parts = [item for item in self.context.part_repo.list_recent_by_task(task.id) if item.conversation_id == conversation_id][-8:]
        historical_tool_messages = self._historical_tool_transcript_messages(
            task_id=task.id,
            conversation_id=conversation_id,
            current_run_id=run.id,
        )

        if str(os.getenv("DEEPEM_CONTEXT_REDUCER_V2") or "").strip() == "1":
            return self._build_messages_reducer_v2(
                profile=profile,
                task=task,
                run=run,
                trigger_event=trigger_event,
                trigger_message=trigger_message,
                conversation_id=conversation_id,
                transcript_messages=transcript_messages,
                nl2sql_options=nl2sql_options,
                event_handler=event_handler,
                force_aggressive_context=force_aggressive_context,
                context_retry_attempt=context_retry_attempt,
                ephemeral_messages=ephemeral_messages,
                state=state,
                cases=cases,
                all_chat_messages=all_chat_messages,
                recent_events=recent_events,
                recent_parts=recent_parts,
                tool_specs=tool_specs,
                budget_estimator=budget_estimator,
            )

        def assemble_with_options(options: ContextCompressionOptions, *, transcript_messages_for_prompt: list[dict[str, Any]]) -> dict[str, Any]:
            keep_recent_messages = options.keep_recent_messages
            recent_messages = self.context.chat_repo.list_by_conversation(conversation_id, limit=keep_recent_messages)
            older_messages = all_chat_messages[: max(0, len(all_chat_messages) - keep_recent_messages)]
            base_messages = self.context.prompt_builder.build(
                profile=profile,
                task=task,
                run=run,
                trigger_event=trigger_event,
                trigger_message=trigger_message,
                state=state,
                cases=cases,
                knowledge_base=self.context.knowledge_base,
                recent_events=recent_events,
                recent_parts=recent_parts,
                recent_messages=recent_messages,
                nl2sql_options=nl2sql_options or NL2SQLSessionConfig(),
            )
            live_user_message = self._build_live_user_message(trigger_message)
            assembled = [*base_messages]
            if live_user_message is not None:
                assembled.append(live_user_message)
            assembled.extend(transcript_messages_for_prompt)
            cached_summary_message = self._cached_context_summary_message(task=task, conversation_id=conversation_id)
            compressor_older_messages = (
                [cached_summary_message, *older_messages] if cached_summary_message is not None else older_messages
            )
            compressor_transcript_messages = [*historical_tool_messages, *transcript_messages_for_prompt]
            context_summary_message, context_compression_metrics = self._context_compressor(options).build_summary_message(
                older_messages=compressor_older_messages,
                transcript_messages=compressor_transcript_messages,
                assembled_messages=assembled,
            )
            if context_summary_message is not None and assembled:
                assembled = [assembled[0], context_summary_message, *assembled[1:]]
            return {
                "assembled": assembled,
                "base_messages": base_messages,
                "context_summary_message": context_summary_message,
                "live_user_message": live_user_message,
                "recent_messages": recent_messages,
                "transcript_messages": transcript_messages_for_prompt,
                "context_compression": context_compression_metrics,
            }

        normal_tool_transcript_metrics = ToolTranscriptCompressor().compress_messages(transcript_messages, aggressive=False)[1]
        normal_build = assemble_with_options(NORMAL_CONTEXT_COMPRESSION, transcript_messages_for_prompt=transcript_messages)
        normal_context_budget = budget_estimator.estimate(normal_build["assembled"])
        compression_mode = "normal"
        selected_build = normal_build
        context_budget_metrics = normal_context_budget
        tool_transcript_metrics = normal_tool_transcript_metrics
        aggressive_context_budget = None

        if force_aggressive_context or normal_context_budget.get("status") == "danger":
            aggressive_transcript, aggressive_tool_transcript_metrics = ToolTranscriptCompressor().compress_messages(
                transcript_messages,
                aggressive=True,
            )
            aggressive_build = assemble_with_options(
                AGGRESSIVE_CONTEXT_COMPRESSION,
                transcript_messages_for_prompt=aggressive_transcript,
            )
            aggressive_context_budget = budget_estimator.estimate(aggressive_build["assembled"])
            compression_mode = "aggressive"
            selected_build = aggressive_build
            context_budget_metrics = aggressive_context_budget
            tool_transcript_metrics = aggressive_tool_transcript_metrics
            normal_chars = int(normal_context_budget.get("estimated_chars") or 0)
            aggressive_chars = int(aggressive_context_budget.get("estimated_chars") or 0)
            if not force_aggressive_context and aggressive_chars < normal_chars:
                self._emit_context_step_once(
                    event_handler,
                    "context_aggressive_compression",
                    {
                        "run_id": run.id,
                        "mode": "aggressive",
                        "reason": "context_budget_danger",
                        "before_chars": normal_chars,
                        "after_chars": aggressive_chars,
                        "before_tokens": normal_context_budget.get("estimated_tokens"),
                        "after_tokens": aggressive_context_budget.get("estimated_tokens"),
                        "usable_input_tokens": aggressive_context_budget.get("usable_input_tokens"),
                        "before_token_usage_ratio": normal_context_budget.get("token_usage_ratio"),
                        "after_token_usage_ratio": aggressive_context_budget.get("token_usage_ratio"),
                        "before_status": normal_context_budget.get("status"),
                        "after_status": aggressive_context_budget.get("status"),
                        "recent_messages_before": NORMAL_CONTEXT_COMPRESSION.keep_recent_messages,
                        "recent_messages_after": AGGRESSIVE_CONTEXT_COMPRESSION.keep_recent_messages,
                    },
                    run_id=run.id,
                )

        assembled = selected_build["assembled"]
        if ephemeral_messages:
            assembled.extend(deepcopy(list(ephemeral_messages)))
        base_messages = selected_build["base_messages"]
        context_summary_message = selected_build["context_summary_message"]
        live_user_message = selected_build["live_user_message"]
        recent_messages = selected_build["recent_messages"]
        selected_transcript_messages = selected_build["transcript_messages"]
        context_compression_metrics = selected_build["context_compression"]
        self._remember_context_summary(conversation_id, context_summary_message)
        if context_compression_metrics.get("applied"):
            assembled_chars_before = int(context_compression_metrics.get("assembled_chars_before") or 0)
            assembled_chars_after = len(json.dumps(assembled, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
            context_compression_metrics = {
                **context_compression_metrics,
                "assembled_chars_after": assembled_chars_after,
                "compression_ratio": round(assembled_chars_after / assembled_chars_before, 6) if assembled_chars_before else 1.0,
            }
        if context_compression_metrics.get("applied"):
            self._emit_context_step_once(
                event_handler,
                "context_compression",
                {
                    "run_id": run.id,
                    "applied": True,
                    "method": context_compression_metrics.get("method"),
                    "older_message_count": context_compression_metrics.get("older_message_count"),
                    "older_messages_chars": context_compression_metrics.get("older_messages_chars"),
                    "assembled_chars_before": context_compression_metrics.get("assembled_chars_before"),
                    "assembled_chars_after": context_compression_metrics.get("assembled_chars_after"),
                    "compression_ratio": context_compression_metrics.get("compression_ratio"),
                    "summary_chars": context_compression_metrics.get("summary_chars"),
                    "summary_compression_ratio": context_compression_metrics.get("summary_compression_ratio"),
                    "tool_result_count": context_compression_metrics.get("tool_result_count"),
                    "semantic_enabled": context_compression_metrics.get("semantic_enabled"),
                    "semantic_attempted": context_compression_metrics.get("semantic_attempted"),
                    "semantic_applied": context_compression_metrics.get("semantic_applied"),
                    "semantic_fallback_reason": context_compression_metrics.get("semantic_fallback_reason"),
                    "semantic_input_original_chars": context_compression_metrics.get("semantic_input_original_chars"),
                    "semantic_input_chars": context_compression_metrics.get("semantic_input_chars"),
                    "semantic_input_trimmed": context_compression_metrics.get("semantic_input_trimmed"),
                    "semantic_latency_ms": context_compression_metrics.get("semantic_latency_ms"),
                    "semantic_input_message_count": context_compression_metrics.get("semantic_input_message_count"),
                    "semantic_input_dropped_message_count": context_compression_metrics.get("semantic_input_dropped_message_count"),
                    "semantic_tool_constraint_count": context_compression_metrics.get("semantic_tool_constraint_count"),
                    "schema_version": context_compression_metrics.get("schema_version"),
                },
                run_id=run.id,
            )
        if context_budget_metrics.get("status") in {"warning", "danger"}:
            self._emit_context_step_once(
                event_handler,
                "context_budget",
                {
                    "run_id": run.id,
                    **context_budget_metrics,
                },
                run_id=run.id,
            )
        composition_base_messages = list(base_messages)
        if context_summary_message is not None:
            composition_base_messages.append(context_summary_message)
        if live_user_message is not None:
            composition_base_messages.append(live_user_message)
        composition_base_messages.extend(ephemeral_messages)
        prompt_composition = PromptCompositionEstimator(budget_estimator=budget_estimator).estimate(
            base_messages=composition_base_messages,
            transcript_messages=selected_transcript_messages,
            assembled_messages=assembled,
        )
        self._emit_context_step_once(
            event_handler,
            "prompt_composition",
            {
                "run_id": run.id,
                **prompt_composition,
            },
            run_id=run.id,
        )
        debug_payload = {
            "profile": getattr(profile, "name", None),
            "task": task,
            "run": run,
            "trigger_event": trigger_event,
            "trigger_message": trigger_message,
            "workspace_snapshot": {
                "state": state,
                "cases": cases,
                "recent_events": recent_events,
                "recent_parts": recent_parts,
                "recent_messages": recent_messages,
            },
            "base_messages": base_messages,
            "live_user_message": live_user_message,
            "transcript_messages": selected_transcript_messages,
            "compression_mode": compression_mode,
            "context_compression": context_compression_metrics,
            "tool_transcript_compression": tool_transcript_metrics,
            "context_budget": context_budget_metrics,
            "normal_context_budget": normal_context_budget,
            "prompt_composition": prompt_composition,
            "assembled_messages": assembled,
        }
        if force_aggressive_context:
            debug_payload["force_aggressive_context"] = True
            debug_payload["context_retry_attempt"] = context_retry_attempt
            debug_payload["context_retry_reason"] = "context_length_exceeded"
        if aggressive_context_budget is not None:
            debug_payload["aggressive_context_budget"] = aggressive_context_budget
        self._debug_log(
            run_id=run.id,
            stage="prompt_built",
            payload=debug_payload,
        )
        return assembled

    def _build_general_messages(
        self,
        *,
        profile,
        run: Run,
        trigger_message: ChatMessage | None,
        conversation_id: str,
        transcript_messages: list[dict[str, Any]],
        event_handler: StreamHandler | None,
        context_retry_attempt: int | None,
        ephemeral_messages: Sequence[dict[str, Any]],
        budget_estimator: ContextBudgetEstimator,
    ) -> list[dict[str, Any]]:
        all_messages = self.context.chat_repo.list_by_conversation(conversation_id)
        trigger_index = next(
            (index for index, message in enumerate(all_messages) if trigger_message is not None and message.id == trigger_message.id),
            0,
        )
        projected_history = project_general_history(all_messages[:trigger_index])

        base_messages: list[dict[str, Any]] = [{
            "role": "system",
            "content": (
                f"{profile.system_prompt}\n\n"
                "历史 assistant 内容可能来自过去的工作区分析，不代表当前实时设备、Case、告警或采集状态。"
            ),
        }]
        base_messages.extend(projected_history)
        live_user_message = self._build_live_user_message(
            trigger_message,
            include_workspace_attachment_refs=False,
        )
        if live_user_message is not None:
            base_messages.append(live_user_message)
        base_messages.extend(deepcopy(list(ephemeral_messages)))
        assembled = [*base_messages]

        context_budget = budget_estimator.estimate(assembled, tools=[])
        prompt_composition = PromptCompositionEstimator(budget_estimator=budget_estimator).estimate(
            base_messages=base_messages,
            transcript_messages=[],
            assembled_messages=assembled,
            tools=[],
        )
        self._emit(event_handler, "context_budget", {"run_id": run.id, **context_budget})
        self._emit_context_step_once(
            event_handler,
            "prompt_composition",
            {"run_id": run.id, **prompt_composition},
            run_id=run.id,
        )
        debug_payload = {
            "profile": getattr(profile, "name", None),
            "base_messages": base_messages,
            "live_user_message": live_user_message,
            "transcript_messages": [],
            "compression_mode": "general",
            "context_compression": {"applied": False, "method": "general_shared_history_projection"},
            "history_message_count": len(projected_history),
            "tool_transcript_compression": {"applied": False, "tool_message_count": 0},
            "context_budget": context_budget,
            "normal_context_budget": context_budget,
            "prompt_composition": prompt_composition,
            "assembled_messages": assembled,
        }
        if context_retry_attempt is not None:
            debug_payload.update(
                {
                    "force_aggressive_context": True,
                    "context_retry_attempt": context_retry_attempt,
                    "context_retry_reason": "context_length_exceeded",
                }
            )
        self._debug_log(run_id=run.id, stage="prompt_built", payload=debug_payload)
        return assembled

    def _build_messages_reducer_v2(
        self,
        *,
        profile,
        task,
        run: Run,
        trigger_event: Event | None,
        trigger_message: ChatMessage | None,
        conversation_id: str,
        transcript_messages: list[dict[str, Any]],
        nl2sql_options: NL2SQLSessionConfig | None,
        event_handler: StreamHandler | None,
        force_aggressive_context: bool,
        context_retry_attempt: int | None,
        ephemeral_messages: Sequence[dict[str, Any]],
        state,
        cases,
        all_chat_messages: list[ChatMessage],
        recent_events,
        recent_parts,
        tool_specs=None,
        budget_estimator: ContextBudgetEstimator,
    ) -> list[dict[str, Any]]:
        recent_messages = all_chat_messages[-NORMAL_CONTEXT_COMPRESSION.keep_recent_messages :]
        base_messages = self.context.prompt_builder.build(
            profile=profile,
            task=task,
            run=run,
            trigger_event=trigger_event,
            trigger_message=trigger_message,
            state=state,
            cases=cases,
            knowledge_base=self.context.knowledge_base,
            recent_events=recent_events,
            recent_parts=recent_parts,
            recent_messages=recent_messages,
            nl2sql_options=nl2sql_options or NL2SQLSessionConfig(),
        )
        history_units, history_tool_metrics = self._raw_history_units(
            all_chat_messages,
            trigger_message=trigger_message,
            conversation_id=conversation_id,
        )
        history_unit_order = {unit.unit_id: index for index, unit in enumerate(history_units)}
        policy = ContextReductionPolicy()
        full_history_messages = flatten_history_units(history_units)
        live_user_message = self._build_live_user_message(trigger_message)
        candidate = [*base_messages, *full_history_messages]
        if live_user_message is not None:
            candidate.append(live_user_message)
        candidate.extend(transcript_messages)
        candidate.extend(deepcopy(list(ephemeral_messages)))

        if tool_specs is None:
            tool_specs = self.context.tool_registry.specs(profile.allowed_tools)
        before_budget = budget_estimator.estimate(candidate, tools=tool_specs)
        ratio = before_budget.get("token_usage_ratio")
        if ratio is None:
            ratio = before_budget.get("usage_ratio") or 0.0
        reducer_stage = "aggressive" if force_aggressive_context else policy.stage_for_ratio(float(ratio))
        if reducer_stage == "normal_summary":
            summarized_units, kept_units = select_tail_units(
                history_units,
                target_message_count=policy.normal_tail_message_target,
            )
        elif reducer_stage == "aggressive":
            summarized_units, kept_units = select_head_and_tail_units(
                history_units,
                target_message_count=policy.aggressive_tail_message_target,
            )
        else:
            summarized_units, kept_units = [], history_units
        initial_protected_unit_ids = (
            {unit.unit_id for unit in kept_units}
            if reducer_stage in {"normal_summary", "aggressive"}
            else set()
        )
        protected_unit_summary_fallback = False
        protected_unit_summary_fallback_count = 0
        selected_transcript_messages, tool_transcript_metrics = ToolTranscriptCompressor().compress_messages(
            transcript_messages,
            aggressive=reducer_stage in {"tool_compaction", "aggressive"},
        )
        summary_options = (
            AGGRESSIVE_CONTEXT_COMPRESSION
            if reducer_stage == "aggressive"
            else NORMAL_CONTEXT_COMPRESSION
        )
        if reducer_stage in {"normal_summary", "aggressive"}:
            # Reserve room for the one summary message before calling a compressor.
            # This is local token estimation only; semantic mode still makes at most
            # one LLM summary request after the unit set has been finalized.
            planning_summary_message = {
                "role": "user",
                "content": (
                    "较早历史上下文摘要（由系统压缩生成）:\n"
                    "__deepem_summary_reserve__\n"
                    + ("文" * summary_options.max_summary_chars)
                ),
            }
            while kept_units:
                planning_messages = [*base_messages]
                planning_messages.insert(1 if base_messages else 0, planning_summary_message)
                planning_messages.extend(flatten_history_units(kept_units))
                if live_user_message is not None:
                    planning_messages.append(live_user_message)
                planning_messages.extend(selected_transcript_messages)
                planning_messages.extend(deepcopy(list(ephemeral_messages)))
                planning_budget = budget_estimator.estimate(planning_messages, tools=tool_specs)
                planning_ratio = planning_budget.get("token_usage_ratio")
                if planning_ratio is None:
                    planning_ratio = planning_budget.get("usage_ratio") or 0.0
                if float(planning_ratio) < policy.aggressive_ratio:
                    break
                summarized_units.append(kept_units.pop(0))
                summarized_units.sort(key=lambda unit: history_unit_order[unit.unit_id])
                protected_unit_summary_fallback = True
                protected_unit_summary_fallback_count += 1
        protected_unit_ids = (
            {unit.unit_id for unit in kept_units}
            if reducer_stage in {"normal_summary", "aggressive"}
            else set()
        )
        history_messages = flatten_history_units(kept_units)
        summary_message = None
        pending_summary_state = None
        summary_cache_status = "disabled"
        summary_incremental_unit_count = 0
        summary_source_fingerprint_changed = False
        summary_mode = "aggressive" if reducer_stage == "aggressive" else "normal"
        semantic_cache_enabled = str(os.getenv("DEEPEM_CONTEXT_SEMANTIC_COMPRESSION") or "").strip() == "1"
        context_compression = {
            "applied": False,
            "method": "rule_based_minimal",
            "older_message_count": 0,
            "older_messages_chars": 0,
            "assembled_chars_before": len(json.dumps(candidate, ensure_ascii=False, separators=(",", ":"))),
            "summary_chars": 0,
            "summary_compression_ratio": 0.0,
        }
        if reducer_stage in {"normal_summary", "aggressive"} and summarized_units:
            target_unit_ids = tuple(unit.unit_id for unit in summarized_units)
            target_source_ids = tuple(
                dict.fromkeys(source_id for unit in summarized_units for source_id in unit.source_message_ids)
            )
            target_fingerprint = history_units_fingerprint(summarized_units)
            cache_key = (conversation_id, summary_mode)
            cached_state = self.context.reducer_summary_cache.get(cache_key) if semantic_cache_enabled else None
            units_for_summary = summarized_units
            previous_summary_message = None
            if cached_state is None:
                summary_cache_status = "miss" if semantic_cache_enabled else "disabled"
            elif (
                cached_state.state_schema_version == 1
                and cached_state.covered_unit_ids == target_unit_ids
                and cached_state.source_fingerprint == target_fingerprint
            ):
                summary_cache_status = "hit"
                summary_message = {"role": "user", "content": cached_state.content}
                context_compression = {
                    **context_compression,
                    "applied": True,
                    "method": cached_state.summary_method,
                    "older_message_count": len(target_source_ids),
                    "older_messages_chars": len(json.dumps(summarized_units, ensure_ascii=False, default=str)),
                    "summary_chars": len(cached_state.content),
                    "semantic_enabled": True,
                    "semantic_attempted": False,
                    "semantic_applied": cached_state.summary_method == "llm_semantic_structured_v1",
                }
            else:
                cached_count = len(cached_state.covered_unit_ids)
                prefix_units = summarized_units[:cached_count]
                prefix_fingerprint = history_units_fingerprint(prefix_units)
                if (
                    cached_state.state_schema_version == 1
                    and is_ordered_coverage_prefix(cached_state.covered_unit_ids, target_unit_ids)
                    and cached_state.source_fingerprint == prefix_fingerprint
                ):
                    summary_cache_status = "incremental"
                    units_for_summary = summarized_units[cached_count:]
                    summary_incremental_unit_count = len(units_for_summary)
                    previous_summary_message = ChatMessage(
                        id=new_id("msg"),
                        conversation_id=conversation_id,
                        task_id=task.id,
                        role=ChatRole.ASSISTANT,
                        content=cached_state.content,
                        run_id=None,
                        created_at=utc_now(),
                        metadata={"synthetic_context_summary": True},
                    )
                else:
                    summary_cache_status = "rebuild"
                    summary_source_fingerprint_changed = (
                        cached_state.covered_unit_ids == target_unit_ids
                        and cached_state.source_fingerprint != target_fingerprint
                    )
            summarized_ids = {
                source_id for unit in units_for_summary for source_id in unit.source_message_ids
            }
            older_messages = [item for item in all_chat_messages if item.id in summarized_ids]
            if previous_summary_message is not None:
                older_messages = [previous_summary_message, *older_messages]
            summarized_tool_messages = [
                message
                for unit in units_for_summary
                for message in unit.messages
                if message.get("role") == "tool"
            ]
            summary_candidate = [*base_messages, *history_messages]
            if live_user_message is not None:
                summary_candidate.append(live_user_message)
            summary_candidate.extend(selected_transcript_messages)
            if summary_message is None:
                compressor_transcript = (
                    summarized_tool_messages
                    if semantic_cache_enabled
                    else [*selected_transcript_messages, *summarized_tool_messages]
                )
                summary_message, context_compression = self._context_compressor(summary_options).build_summary_message(
                    older_messages=older_messages,
                    transcript_messages=compressor_transcript,
                    assembled_messages=summary_candidate,
                )
                cacheable_semantic_summary = (
                    semantic_cache_enabled
                    and context_compression.get("semantic_applied") is True
                )
                if semantic_cache_enabled and summary_message is not None and not cacheable_semantic_summary:
                    summary_cache_status = "rejected"
                context_compression = {
                    **context_compression,
                    "summary_cache_status": summary_cache_status,
                    "summary_cache_hit": False,
                    "summary_incremental_unit_count": summary_incremental_unit_count,
                    "summary_covered_unit_count": len(summarized_units),
                    "summary_source_fingerprint_changed": summary_source_fingerprint_changed,
                }
                if summary_message is not None and cacheable_semantic_summary:
                    pending_summary_state = ReducerSummaryState(
                        content=str(summary_message.get("content") or ""),
                        covered_unit_ids=target_unit_ids,
                        covered_source_message_ids=target_source_ids,
                        source_fingerprint=target_fingerprint,
                        compression_mode=summary_mode,
                        summary_method=str(context_compression.get("method") or "rule_based_minimal"),
                    )
            else:
                context_compression = {
                    **context_compression,
                    "summary_cache_status": summary_cache_status,
                    "summary_cache_hit": True,
                    "summary_incremental_unit_count": 0,
                    "summary_covered_unit_count": len(summarized_units),
                    "summary_source_fingerprint_changed": False,
                }
            if summary_message is None:
                if semantic_cache_enabled:
                    summary_cache_status = "rejected"
                    context_compression = {
                        **context_compression,
                        "summary_cache_status": summary_cache_status,
                        "summary_cache_hit": False,
                    }
                summarized_units = []
                kept_units = history_units
                history_messages = full_history_messages
                protected_unit_ids = initial_protected_unit_ids
        if summary_message is not None and base_messages:
            assembled = [base_messages[0], summary_message, *base_messages[1:]]
        else:
            assembled = [*base_messages]
        assembled.extend(history_messages)
        if live_user_message is not None:
            assembled.append(live_user_message)
        assembled.extend(selected_transcript_messages)
        assembled.extend(deepcopy(list(ephemeral_messages)))
        after_budget = budget_estimator.estimate(assembled, tools=tool_specs)
        before_size = before_budget.get("estimated_tokens")
        after_size = after_budget.get("estimated_tokens")
        if before_size is None or after_size is None:
            before_size = before_budget.get("estimated_chars")
            after_size = after_budget.get("estimated_chars")
        if summary_message is not None and int(after_size or 0) > int(before_size or 0):
            summary_message = None
            pending_summary_state = None
            if semantic_cache_enabled:
                summary_cache_status = "rejected"
            summarized_units = []
            kept_units = history_units
            history_messages = full_history_messages
            protected_unit_ids = initial_protected_unit_ids
            assembled = [*base_messages, *history_messages]
            if live_user_message is not None:
                assembled.append(live_user_message)
            assembled.extend(selected_transcript_messages)
            assembled.extend(deepcopy(list(ephemeral_messages)))
            after_budget = budget_estimator.estimate(assembled, tools=tool_specs)
            context_compression = {
                **context_compression,
                "applied": False,
                "skipped_reason": "summary_not_smaller",
                "summary_cache_status": summary_cache_status,
                "summary_cache_hit": False,
            }
        if pending_summary_state is not None and summary_message is not None:
            self.context.reducer_summary_cache[(conversation_id, summary_mode)] = pending_summary_state
        dropped_units: list[HistoryUnit] = []
        while (after_budget.get("token_usage_ratio") or after_budget.get("usage_ratio") or 0) > policy.aggressive_ratio:
            drop_index = next(
                (index for index, unit in enumerate(kept_units) if unit.unit_id not in protected_unit_ids),
                None,
            )
            if drop_index is None:
                break
            dropped_units.append(kept_units.pop(drop_index))
            history_messages = flatten_history_units(kept_units)
            if summary_message is not None and base_messages:
                assembled = [base_messages[0], summary_message, *base_messages[1:]]
            else:
                assembled = [*base_messages]
            assembled.extend(history_messages)
            if live_user_message is not None:
                assembled.append(live_user_message)
            assembled.extend(selected_transcript_messages)
            assembled.extend(deepcopy(list(ephemeral_messages)))
            after_budget = budget_estimator.estimate(assembled, tools=tool_specs)
        context_reduction = {
            "reducer_stage": reducer_stage,
            "before_reduction_context_budget": before_budget,
            "after_reduction_context_budget": after_budget,
            "before_reduction_token_usage_ratio": before_budget.get("token_usage_ratio"),
            "after_reduction_token_usage_ratio": after_budget.get("token_usage_ratio"),
            "history_units_total": len(history_units),
            "history_units_kept": len(kept_units),
            "history_units_summarized": len(summarized_units) if summary_message is not None else 0,
            "history_units_dropped": len(dropped_units),
            "protected_unit_summary_fallback": protected_unit_summary_fallback,
            "protected_unit_summary_fallback_count": protected_unit_summary_fallback_count,
            "summary_cache_status": summary_cache_status,
            "summary_cache_hit": summary_cache_status == "hit",
            "summary_covered_unit_count": len(summarized_units) if summary_message is not None else 0,
            "summary_incremental_unit_count": summary_incremental_unit_count,
            "summary_source_fingerprint_changed": summary_source_fingerprint_changed,
            "tool_pairs_total": history_tool_metrics["tool_pairs_total"],
            "tool_pairs_compacted": tool_transcript_metrics.get("compacted_tool_message_count", 0),
            "tool_pairs_invalid": history_tool_metrics["tool_pairs_invalid"],
            "hard_limit_satisfied": (after_budget.get("token_usage_ratio") or after_budget.get("usage_ratio") or 0) <= 0.90,
            "decision_method": "token" if before_budget.get("token_usage_ratio") is not None else "chars",
        }
        if context_compression.get("applied"):
            assembled_chars_before = int(context_compression.get("assembled_chars_before") or 0)
            assembled_chars_after = len(json.dumps(assembled, ensure_ascii=False, separators=(",", ":")))
            context_compression = {
                **context_compression,
                "assembled_chars_after": assembled_chars_after,
                "compression_ratio": round(assembled_chars_after / assembled_chars_before, 6)
                if assembled_chars_before
                else 1.0,
            }
            self._emit_context_step_once(
                event_handler,
                "context_compression",
                {"run_id": run.id, **context_compression},
                run_id=run.id,
            )
        self._emit_context_step_once(
            event_handler,
            "context_reduction",
            {"run_id": run.id, **context_reduction},
            run_id=run.id,
        )
        if after_budget.get("status") in {"warning", "danger"}:
            self._emit_context_step_once(
                event_handler,
                "context_budget",
                {"run_id": run.id, **after_budget},
                run_id=run.id,
            )
        composition_base = [*base_messages, *history_messages]
        if live_user_message is not None:
            composition_base.append(live_user_message)
        composition_base.extend(ephemeral_messages)
        prompt_composition = PromptCompositionEstimator(budget_estimator=budget_estimator).estimate(
            base_messages=composition_base,
            transcript_messages=selected_transcript_messages,
            assembled_messages=assembled,
            tools=tool_specs,
        )
        context_budget = after_budget
        debug_payload = {
                "profile": getattr(profile, "name", None),
                "task": task,
                "run": run,
                "trigger_event": trigger_event,
                "trigger_message": trigger_message,
                "workspace_snapshot": {
                    "state": state,
                    "cases": cases,
                    "recent_events": recent_events,
                    "recent_parts": recent_parts,
                    "recent_messages": recent_messages,
                },
                "base_messages": base_messages,
                "live_user_message": live_user_message,
                "transcript_messages": selected_transcript_messages,
                "compression_mode": "aggressive" if reducer_stage == "aggressive" else "normal",
                "context_compression": context_compression,
                "context_reduction": context_reduction,
                "tool_transcript_compression": tool_transcript_metrics,
                "context_budget": context_budget,
                "normal_context_budget": before_budget,
                "prompt_composition": prompt_composition,
                "assembled_messages": assembled,
            }
        if force_aggressive_context:
            debug_payload.update(
                {
                    "force_aggressive_context": True,
                    "context_retry_attempt": context_retry_attempt,
                    "context_retry_reason": "context_length_exceeded",
                }
            )
        self._debug_log(
            run_id=run.id,
            stage="prompt_built",
            payload=debug_payload,
        )
        return assembled

    def _raw_history_units(
        self,
        chat_messages: Sequence[ChatMessage],
        *,
        trigger_message: ChatMessage | None,
        conversation_id: str,
    ) -> tuple[list[HistoryUnit], dict[str, int]]:
        raw_messages: list[dict[str, Any]] = []
        tool_pairs_total = 0
        tool_pairs_invalid = 0
        processed_tool_runs: set[str] = set()
        trigger_id = trigger_message.id if trigger_message is not None else None
        for message in chat_messages:
            if message.id == trigger_id:
                continue
            role = "user" if message.role == ChatRole.OPERATOR else "assistant"
            content = (message.content or "").strip()
            attachment_notes = [
                self._format_attachment_note(
                    label=str(attachment.label or attachment.uri or "附件"),
                    asset_id=str((attachment.metadata or {}).get("asset_id") or "").strip(),
                    upload_kind=str((attachment.metadata or {}).get("upload_kind") or "").strip(),
                )
                for attachment in list(message.attachments or [])
            ]
            if attachment_notes:
                note = f"历史附件：{'；'.join(attachment_notes)}"
                content = f"{content}\n\n{note}" if content else note
            if role == "assistant" and message.run_id and message.run_id not in processed_tool_runs:
                processed_tool_runs.add(message.run_id)
                try:
                    run_tool_calls = self.context.tool_call_repo.list_by_run(message.run_id)
                except Exception:
                    run_tool_calls = []
                valid_calls: list[tuple[ToolCall, dict[str, Any]]] = []
                invalid_group = False
                for tool_call in run_tool_calls:
                    if tool_call.conversation_id not in (None, conversation_id) or tool_call.result is None:
                        tool_pairs_invalid += 1
                        invalid_group = True
                        continue
                    try:
                        compact_result = ToolResultCompressor().compress(
                            tool_name=tool_call.tool_name,
                            arguments=tool_call.input,
                            tool_result=tool_call.result,
                            tool_call_id=tool_call.id,
                        )
                        compact_result.pop("compressed_at", None)
                        compact_result.pop("compression", None)
                    except Exception:
                        tool_pairs_invalid += 1
                        invalid_group = True
                        continue
                    valid_calls.append((tool_call, compact_result))
                if valid_calls and not invalid_group:
                    raw_messages.append(
                        {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": tool_call.id,
                                    "type": "function",
                                    "function": {
                                        "name": tool_call.tool_name,
                                        "arguments": json.dumps(tool_call.input, ensure_ascii=False),
                                    },
                                }
                                for tool_call, _ in valid_calls
                            ],
                            "source_message_id": message.id,
                        }
                    )
                    for tool_call, compact_result in valid_calls:
                        raw_messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tool_call.id,
                                "content": json.dumps(compact_result, ensure_ascii=False),
                                "source_message_id": message.id,
                            }
                        )
                    tool_pairs_total += len(valid_calls)
            raw_messages.append({"role": role, "content": content, "source_message_id": message.id})
        return build_history_units(raw_messages), {
            "tool_pairs_total": tool_pairs_total,
            "tool_pairs_invalid": tool_pairs_invalid,
        }

    def _context_compressor(self, options: ContextCompressionOptions) -> ConversationContextCompressor:
        semantic_enabled = str(os.getenv("DEEPEM_CONTEXT_SEMANTIC_COMPRESSION") or "").strip() == "1"
        if semantic_enabled and not options.semantic_enabled:
            options = replace(options, semantic_enabled=True)
        compressor_llm_client = self._compression_llm_client() if options.semantic_enabled else self.context.llm_client
        return ConversationContextCompressor(
            options,
            llm_client=self.context.llm_client,
            compressor_llm_client=compressor_llm_client,
        )

    def _compression_llm_client(self) -> Any:
        client = self.context.llm_client
        settings = getattr(client, "settings", None)
        if settings is None:
            return client
        try:
            return client.__class__(settings)
        except Exception:
            return client

    def _cached_context_summary_message(self, *, task, conversation_id: str) -> ChatMessage | None:
        content = self.context.context_summary_cache.get(conversation_id)
        if not content:
            return None
        return ChatMessage(
            id=new_id("msg"),
            conversation_id=conversation_id,
            task_id=task.id,
            role=ChatRole.ASSISTANT,
            content=content,
            run_id=None,
            created_at=utc_now(),
            metadata={"synthetic_context_summary": True},
        )

    def _remember_context_summary(self, conversation_id: str, context_summary_message: dict[str, Any] | None) -> None:
        if context_summary_message is None:
            return
        content = context_summary_message.get("content")
        if isinstance(content, str) and content.strip():
            self.context.context_summary_cache[conversation_id] = content

    def _historical_tool_transcript_messages(
        self,
        *,
        task_id: str,
        conversation_id: str,
        current_run_id: str,
        max_tool_results: int = 8,
    ) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        try:
            runs = self.context.run_repo.list_by_task(task_id, limit=12)
        except Exception:
            return messages
        for historical_run in runs:
            if historical_run.id == current_run_id or historical_run.conversation_id != conversation_id:
                continue
            try:
                tool_calls = self.context.tool_call_repo.list_by_run(historical_run.id)
            except Exception:
                continue
            for tool_call in tool_calls:
                if tool_call.conversation_id not in (None, conversation_id):
                    continue
                if tool_call.result is None:
                    continue
                try:
                    compact_result = ToolResultCompressor().compress(
                        tool_name=tool_call.tool_name,
                        arguments=tool_call.input,
                        tool_result=tool_call.result,
                        tool_call_id=tool_call.id,
                    )
                except Exception:
                    continue
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": json.dumps(compact_result, ensure_ascii=False),
                    }
                )
        return messages[-max_tool_results:]

    def _build_live_user_message(
        self,
        trigger_message: ChatMessage | None,
        *,
        include_workspace_attachment_refs: bool = True,
    ) -> dict[str, Any] | None:
        if trigger_message is None:
            return None

        text = (trigger_message.content or "").strip()
        asset_manager = self.context.asset_manager
        image_parts: list[dict[str, Any]] = []
        attachment_notes: list[str] = []

        for attachment in list(trigger_message.attachments or []):
            metadata = dict(attachment.metadata or {})
            label = str(attachment.label or attachment.uri or "附件")
            asset_id = str(metadata.get("asset_id") or "").strip()
            upload_kind = str(metadata.get("upload_kind") or "").strip()
            if upload_kind == "image" and asset_id and asset_manager is not None:
                try:
                    asset = asset_manager.get(asset_id)
                    image_parts.append(
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": encode_image_as_data_url(asset.mime_type, asset_manager.read_bytes(asset_id)),
                            },
                        }
                    )
                    continue
                except Exception:
                    pass
            if include_workspace_attachment_refs:
                attachment_notes.append(self._format_attachment_note(label=label, asset_id=asset_id, upload_kind=upload_kind))
            else:
                continue

        if image_parts:
            prompt_text = text or "请结合当前上传图片回答。"
            if attachment_notes:
                prompt_text = f"{prompt_text}\n\n可通过工具访问的附件：{'；'.join(attachment_notes)}"
            return {"role": "user", "content": [{"type": "text", "text": prompt_text}, *image_parts]}

        if attachment_notes:
            note = f"当前附件：{'；'.join(attachment_notes)}"
            text = f"{text}\n\n{note}" if text else f"请结合当前上传文件回答。\n\n{note}"
        return {"role": "user", "content": text or "请回答当前问题。"}

    @staticmethod
    def _format_attachment_note(*, label: str, asset_id: str, upload_kind: str) -> str:
        parts = [label]
        if asset_id:
            parts.append(f"id={asset_id}")
        if upload_kind:
            parts.append(f"type={upload_kind}")
        return " ".join(parts)


    def _session_scoped_state(self, state, conversation_id: str):
        scoped = deepcopy(state)
        scoped.active_signals = {
            key: value
            for key, value in scoped.active_signals.items()
            if getattr(value, "conversation_id", None) == conversation_id
        }
        metadata = dict(scoped.metadata or {})
        # Keep global/platform metadata, but hide collector session snapshots
        # produced by other chat sessions when a prompt is built.
        collector = metadata.get("collector")
        if isinstance(collector, dict):
            sessions = dict(collector.get("sessions") or {})
            scoped_sessions = {
                key: value
                for key, value in sessions.items()
                if isinstance(value, dict) and value.get("conversation_id") == conversation_id
            }
            collector = dict(collector)
            collector["sessions"] = scoped_sessions
            if isinstance(collector.get("active_session"), dict) and collector["active_session"].get("conversation_id") != conversation_id:
                collector["active_session"] = None
                collector["current_session_id"] = None
            metadata["collector"] = collector
        scoped.metadata = metadata
        return scoped

    def _record_reasoning_part(self, *, task_id: str, run_id: str, content: str, trigger_event: Event | None, trigger_message: ChatMessage | None) -> None:
        reasoning = content.strip()
        if not reasoning:
            return
        run = self.context.run_repo.get(run_id)
        part = Part(
            id=new_id("part"),
            task_id=task_id,
            run_id=run_id,
            kind=PartKind.REASONING,
            content=reasoning,
            created_at=utc_now(),
            event_id=trigger_event.id if trigger_event else None,
            message_id=trigger_message.id if trigger_message else None,
            conversation_id=run.conversation_id,
        )
        self.context.part_repo.append(part)
        self._debug_log(run_id=run_id, stage="reasoning_part_recorded", payload={"part": part})

    def _record_text_part(self, *, task_id: str, run_id: str, content: str | None, trigger_event: Event | None, trigger_message: ChatMessage | None) -> None:
        text = (content or "").strip()
        if not text:
            return
        run = self.context.run_repo.get(run_id)
        part = Part(
            id=new_id("part"),
            task_id=task_id,
            run_id=run_id,
            kind=PartKind.TEXT,
            content=text,
            created_at=utc_now(),
            event_id=trigger_event.id if trigger_event else None,
            message_id=trigger_message.id if trigger_message else None,
            conversation_id=run.conversation_id,
        )
        self.context.part_repo.append(part)
        self._debug_log(run_id=run_id, stage="text_part_recorded", payload={"part": part})

    def _emit(self, event_handler: StreamHandler | None, event_type: str, payload: dict[str, Any]) -> None:
        if event_handler is None:
            return
        event_handler(event_type, payload)

    def _emit_context_step_once(self, event_handler: StreamHandler | None, event_type: str, payload: dict[str, Any], *, run_id: str) -> None:
        if event_type == "context_budget":
            if self._emitted_context_budget_rank is None:
                self._emitted_context_budget_rank = {}
            rank = {"ok": 0, "warning": 1, "danger": 2}.get(str(payload.get("status") or ""), 0)
            if rank <= self._emitted_context_budget_rank.get(run_id, 0):
                return
            self._emitted_context_budget_rank[run_id] = rank
            self._emit(event_handler, event_type, payload)
            return
        if self._emitted_context_steps is None:
            self._emitted_context_steps = set()
        key = (run_id, event_type)
        if key in self._emitted_context_steps:
            return
        self._emitted_context_steps.add(key)
        self._emit(event_handler, event_type, payload)

    def _user_facing_reasoning(self, text: str) -> str:
        compact = self._compact_text(text, limit=220)
        for token in ("\n", "<think>", "</think>"):
            compact = compact.replace(token, " ")
        return compact.strip() 

    def _tool_result_summary(self, *, tool_name: str, tool_result: ToolResult) -> str:
        if tool_result.error:
            return f"工具返回：{tool_name} 执行失败，原因：{self._compact_text(tool_result.error, limit=180)}"
        preview = self._compact_json(tool_result.data, limit=220)
        return f"工具返回：{preview or (tool_name + ' 执行成功')}"

    def _case_update_summary(self, *, arguments: dict[str, Any], tool_result: ToolResult) -> str:
        signal_id = str(arguments.get("signal_id") or tool_result.data.get("signal_id") or "-")
        status = str(arguments.get("status") or tool_result.data.get("status") or "-")
        risk_level = str(arguments.get("risk_level") or "-")
        return f"更新病例状态：{signal_id} → {status} / 风险 {risk_level}"

    def _compact_data(self, value: Any, *, depth: int = 0) -> Any:
        if value is None:
            return None
        if depth >= 3:
            return "<omitted>"
        if isinstance(value, str):
            return self._compact_text(value)
        if isinstance(value, (int, float, bool)):
            return value
        if isinstance(value, Mapping):
            items: dict[str, Any] = {}
            for index, (key, item) in enumerate(value.items()):
                key_str = str(key)
                lowered = key_str.lower()
                if any(token in lowered for token in ("spectrogram", "raw_file", "file_name", "path", "matrix", "image", "payload")):
                    continue
                items[key_str] = self._compact_data(item, depth=depth + 1)
                if index >= 7:
                    items["..."] = "已省略其余字段"
                    break
            return items
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            result = [self._compact_data(item, depth=depth + 1) for item in list(value)[:4]]
            if len(value) > 4:
                result.append("... 已省略其余项目")
            return result
        return self._compact_text(str(value))

    def _compact_json(self, value: Any, *, limit: int = 180) -> str:
        try:
            text = json.dumps(self._compact_data(value), ensure_ascii=False)
        except Exception:
            text = str(value)
        return self._compact_text(text, limit=limit)

    def _compact_text(self, text: str, *, limit: int = 160) -> str:
        normalized = " ".join(str(text).split())
        if len(normalized) <= limit:
            return normalized
        return normalized[: limit - 3] + "..."

    def _debug_log(self, *, run_id: str, stage: str, payload: dict[str, Any]) -> None:
        logger = getattr(self.context, "debug_logger", None)
        if logger is None:
            return
        logger.safe_log(run_id=run_id, stage=stage, payload=payload)
