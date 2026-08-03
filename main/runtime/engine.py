from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from deepem.agent.llm import LLMCancelledError
from deepem.agent.profiles import GENERAL_QA_AGENT, PLACE_DETECTION_AGENT, TASK_CHAT_AGENT
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
        persist_assistant_message: bool = True,
        cancel_checker: CancelChecker | None = None,
        chat_mode: ChatMode = ChatMode.WORKSPACE,
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
            persist_assistant_message=persist_assistant_message,
            cancel_checker=cancel_checker,
            chat_mode=chat_mode,
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
        persist_assistant_message: bool = True,
        cancel_checker: CancelChecker | None = None,
        chat_mode: ChatMode = ChatMode.WORKSPACE,
    ) -> Run:
        chat_mode = ChatMode(chat_mode)
        nl2sql_options = nl2sql_options or NL2SQLSessionConfig()
        llm_options = effective_llm_options(chat_mode, llm_options)
        task = self.context.task_repo.get(task_id)
        profile = self._select_profile(trigger_kind, chat_mode)
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
        try:
            if cancel_checker and cancel_checker():
                return self._abort_run(run=run, task=task, event_handler=event_handler, reason="cancelled_before_execution")
            if trigger_kind == RunTriggerKind.CHAT and trigger_message and nl2sql_options.force_enabled:
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
            while run.step_count < run.step_budget:
                if cancel_checker and cancel_checker():
                    return self._abort_run(run=run, task=task, event_handler=event_handler, reason="cancelled_before_step")
                messages = self._build_messages(
                    profile=profile,
                    task=task,
                    run=run,
                    trigger_event=trigger_event,
                    trigger_message=trigger_message,
                    conversation_id=conversation.id,
                    transcript_messages=transcript_messages,
                    nl2sql_options=nl2sql_options,
                    chat_mode=chat_mode,
                )
                tool_specs = self.context.tool_registry.specs(profile.allowed_tools)
                self._emit(
                    event_handler,
                    "execution_policy",
                    {
                        "run_id": run.id,
                        **build_execution_policy(
                            chat_mode=chat_mode,
                            available_tool_count=len(tool_specs),
                            effective_options=llm_options,
                        ).to_event(),
                    },
                )
                self._debug_log(
                    run_id=run.id,
                    stage="llm_request",
                    payload={
                        "step_index": run.step_count + 1,
                        "messages": messages,
                        "tools": tool_specs,
                        "temperature": profile.temperature,
                        "llm_options": llm_options,
                        "trigger_event": trigger_event,
                        "trigger_message": trigger_message,
                    },
                )
                llm_stream_state = {"content_started": False}

                def llm_stream_handler(event_type: str, payload: dict[str, Any]) -> None:
                    if event_type == "reasoning_start":
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


                response = self.context.llm_client.complete(
                    messages=messages,
                    tools=tool_specs,
                    temperature=profile.temperature,
                    generation_options=llm_options,
                    stream_handler=llm_stream_handler,
                    cancel_checker=cancel_checker,
                )
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
                self.context.run_repo.save(run)
                if chat_mode is ChatMode.GENERAL and response.tool_calls:
                    response.tool_calls = []
                self._record_reasoning_part(
                    task_id=task_id,
                    run_id=run.id,
                    content=response.reasoning,
                    trigger_event=trigger_event,
                    trigger_message=trigger_message,
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

                assistant_reply = (response.content or "").strip()
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
                    )
                    self.context.chat_repo.append(assistant_message)
                    conversation.updated_at = assistant_message.created_at
                    self.context.conversation_repo.save(conversation)
                run.status = RunStatus.COMPLETED
                run.ended_at = utc_now()
                run.stop_reason = "assistant_response"
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

    def _select_profile(self, trigger_kind: RunTriggerKind, chat_mode: ChatMode = ChatMode.WORKSPACE):
        chat_mode = ChatMode(chat_mode)
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
            "extract_baseline_spectrum_peaks",
            "compare_spectrum_with_baseline",
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
        content = json.dumps(
            {
                "status": tool_result.status,
                "data": tool_result.data,
                "error": tool_result.error,
                "emitted_event_ids": tool_result.emitted_event_ids,
            },
            ensure_ascii=False,
        )
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
        chat_mode: ChatMode = ChatMode.WORKSPACE,
    ) -> list[dict[str, Any]]:
        if chat_mode is ChatMode.GENERAL:
            return self._build_general_messages(profile=profile, trigger_message=trigger_message, conversation_id=conversation_id)
        state = self._session_scoped_state(self.context.state_repo.get(task.id), conversation_id)
        cases = [item for item in self.context.case_repo.list_by_task(task.id) if item.conversation_id == conversation_id]
        recent_messages = self.context.chat_repo.list_by_conversation(conversation_id, limit=8)
        recent_events = [item for item in self.context.event_repo.list_by_task(task.id) if item.conversation_id == conversation_id][-8:]
        recent_parts = [item for item in self.context.part_repo.list_recent_by_task(task.id) if item.conversation_id == conversation_id][-8:]
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
        assembled.extend(transcript_messages)
        self._debug_log(
            run_id=run.id,
            stage="prompt_built",
            payload={
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
                "transcript_messages": transcript_messages,
                "assembled_messages": assembled,
            },
        )
        return assembled

    def _build_general_messages(self, *, profile, trigger_message: ChatMessage | None, conversation_id: str) -> list[dict[str, Any]]:
        history = self.context.chat_repo.list_by_conversation(conversation_id)
        prior_messages = [item for item in history if trigger_message is None or item.id != trigger_message.id]
        assembled: list[dict[str, Any]] = [{"role": "system", "content": profile.system_prompt}]
        assembled.extend(project_general_history(prior_messages))
        live_user_message = self._build_live_user_message(trigger_message)
        if live_user_message is not None:
            assembled.append(live_user_message)
        return assembled

    def _build_live_user_message(self, trigger_message: ChatMessage | None) -> dict[str, Any] | None:
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
            attachment_notes.append(self._format_attachment_note(label=label, asset_id=asset_id, upload_kind=upload_kind))

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
