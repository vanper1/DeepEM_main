from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from deepem.agent.profiles import AgentProfile
from deepem.nl2sql_config import NL2SQLSessionConfig
from deepem.protocol import CaseRecord, ChatMessage, Event, Part, Run, RunTriggerKind, StateSnapshot, Task
from deepem.state.repositories import KnowledgeBase


class PromptBuilder:
    _DROP_KEYS = {
        "latest_spectrogram",
        "recent_spectrogram",
        "raw_file",
        "file_name",
        "files",
        "session_dir",
        "uploads_root",
        "image",
        "matrix",
        "iq",
        "samples",
        "raw_samples",
        "raw_payload",
        "plot",
    }
    _DROP_SUBSTRINGS = ("spectrogram", "raw_file", "full_path", "file_path", "filepath")
    _MAX_LIST_ITEMS = 6
    _MAX_STRING_LENGTH = 280
    _MAX_OBJECT_KEYS = 16

    def build(
        self,
        *,
        profile: AgentProfile,
        task: Task,
        run: Run,
        trigger_event: Event | None,
        trigger_message: ChatMessage | None,
        state: StateSnapshot,
        cases: list[CaseRecord],
        knowledge_base: KnowledgeBase,
        recent_events: list[Event],
        recent_parts: list[Part],
        recent_messages: list[ChatMessage],
        nl2sql_options: NL2SQLSessionConfig | None = None,
    ) -> list[dict[str, Any]]:
        options = nl2sql_options or NL2SQLSessionConfig()
        return [
            {"role": "system", "content": self._system_prompt(profile=profile, task=task, nl2sql_options=options)},
            {
                "role": "user",
                "content": self._user_prompt(
                    task=task,
                    run=run,
                    trigger_event=trigger_event,
                    trigger_message=trigger_message,
                    state=state,
                    cases=cases,
                    knowledge_base=knowledge_base,
                    recent_events=recent_events,
                    recent_parts=recent_parts,
                    recent_messages=recent_messages,
                    nl2sql_options=options,
                ),
            },
        ]

    # def _system_prompt(self, *, profile: AgentProfile, task: Task, nl2sql_options: NL2SQLSessionConfig) -> str:
    #     config_note = (
    #         "\nNL2SQL 会话配置："
    #         f"强制开启={str(nl2sql_options.force_enabled).lower()}，"
    #         f"自动选表={str(nl2sql_options.auto_select_tables).lower()}，"
    #         f"手动选表={nl2sql_options.manual_selected_tables or ['<全部表>']}，"
    #         f"数据库路径={nl2sql_options.db_path or '<默认数据库>'}。"
    #         "若调用 query_local_database，必须遵守该配置。"
    #     )
    #     return (
    #         f"{profile.system_prompt}{config_note}\n\n"
    #         f"任务类型：{task.task_type}\n"
    #         "没有工作区证据或工具结果时，不要声称信号一定异常。"
    #     )

    def _system_prompt(self, *, profile: AgentProfile, task: Task, nl2sql_options: NL2SQLSessionConfig) -> str:
        
        return (
            f"{profile.system_prompt}\n\n"
            f"任务类型：{task.task_type}\n"
            "没有工作区证据或工具结果时，不要声称信号一定异常。"
        )        

    def _user_prompt(
        self,
        *,
        task: Task,
        run: Run,
        trigger_event: Event | None,
        trigger_message: ChatMessage | None,
        state: StateSnapshot,
        cases: list[CaseRecord],
        knowledge_base: KnowledgeBase,
        recent_events: list[Event],
        recent_parts: list[Part],
        recent_messages: list[ChatMessage],
        nl2sql_options: NL2SQLSessionConfig,
    ) -> str:
        workspace = {
            "任务": {
                "id": task.id,
                "目标": task.target,
                "状态": task.status,
            },
            "状态快照": {
                "场所": state.place_id,
                "元数据": self._sanitize_data(state.metadata),
                "活跃信号": {
                    signal_id: {
                        "指纹": item.fingerprint,
                        "分类": item.classification,
                        "是否承载信息": item.carries_information,
                        "疑似设备类型": item.suspected_device_type,
                        "特征": self._sanitize_data(item.metadata),
                    }
                    for signal_id, item in state.active_signals.items()
                },
            },
            "Case列表": [
                {
                    "case_id": case.id,
                    "signal_id": case.signal_id,
                    "status": case.status,
                    "risk_level": case.risk_level,
                    "hypothesis": case.hypothesis,
                    "next_actions": case.next_actions,
                    "notes": case.notes[-4:],
                }
                for case in cases
            ],
            "场所知识": self._sanitize_data(self._describe_place(knowledge_base=knowledge_base, state=state)),
            "近期事件": [
                {
                    "seq": item.seq,
                    "type": item.event_type,
                    "source": item.source,
                    "payload": self._sanitize_data(item.payload),
                }
                for item in recent_events
            ],
            "近期运行片段": [
                {
                    "kind": item.kind,
                    "content": self._clip_text(item.content, limit=220),
                }
                for item in recent_parts
            ],
            "近期对话": [
                {
                    "role": item.role,
                    "content": self._clip_text(item.content, limit=220),
                    "attachments": self._summarize_attachments(item.attachments),
                }
                for item in recent_messages
            ],
            "NL2SQL会话配置": nl2sql_options.to_prompt_payload(),
        }
        if run.trigger_kind == RunTriggerKind.BOOTSTRAP:
            instruction = "请检查当前工作区，并给出简洁的启动建议。"
        elif run.trigger_kind == RunTriggerKind.EVENT:
            instruction = (
                "系统已将一条可疑事件加入复核队列。请判断是否需要聚焦复采，然后执行深度分析并更新 Case。"
                "最终需要给出一段适合展示在 UI 时间线中的简洁运营摘要。"
            )
        else:
            instruction = "请清晰回答操作员问题，并严格基于工作区事实。如果对方给出正式反馈，请通过工具记录。"
        trigger = {
            "事件触发": {
                "type": trigger_event.event_type,
                "source": trigger_event.source,
                "payload": self._sanitize_data(trigger_event.payload),
            }
            if trigger_event
            else None,
            "聊天触发": {
                "role": trigger_message.role,
                "content": self._clip_text(trigger_message.content, limit=320),
                "attachments": self._summarize_attachments(trigger_message.attachments),
            }
            if trigger_message
            else None,
        }
        return (
            f"{instruction}\n\n"
            f"触发上下文：\n{json.dumps(trigger, ensure_ascii=False, indent=2, default=str)}\n\n"
            f"工作区快照：\n{json.dumps(workspace, ensure_ascii=False, indent=2, default=str)}"
        )

    def _sanitize_data(self, value: Any, *, depth: int = 0) -> Any:
        if value is None:
            return None
        if depth >= 5:
            return "<omitted>"
        if isinstance(value, str):
            return self._clip_text(value)
        if isinstance(value, (int, float, bool)):
            return value
        if isinstance(value, Mapping):
            items: dict[str, Any] = {}
            hidden_count = 0
            for key, item in list(value.items())[: self._MAX_OBJECT_KEYS * 2]:
                key_str = str(key)
                key_lower = key_str.lower()
                if key_lower in self._DROP_KEYS or any(token in key_lower for token in self._DROP_SUBSTRINGS):
                    hidden_count += 1
                    continue
                sanitized = self._sanitize_data(item, depth=depth + 1)
                if sanitized in (None, {}, [], ""):
                    continue
                items[key_str] = sanitized
                if len(items) >= self._MAX_OBJECT_KEYS:
                    break
            if hidden_count:
                items["_omitted_fields"] = hidden_count
            return items
        if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
            result = [self._sanitize_data(item, depth=depth + 1) for item in list(value)[: self._MAX_LIST_ITEMS]]
            if len(value) > self._MAX_LIST_ITEMS:
                result.append(f"... 其余 {len(value) - self._MAX_LIST_ITEMS} 项已省略")
            return result
        return self._clip_text(str(value))

    def _describe_place(self, *, knowledge_base: KnowledgeBase, state: StateSnapshot) -> dict[str, object]:
        payload = dict(knowledge_base.describe_place(state.place_id))
        baseline_meta = state.metadata.get("place_baseline")
        if isinstance(baseline_meta, dict) and baseline_meta.get("initialized"):
            fingerprints = [
                str(item).strip()
                for item in baseline_meta.get("fingerprints", [])
                if str(item).strip()
            ]
            payload["baseline_fingerprints"] = fingerprints
            payload["expected_signal_count"] = len(fingerprints)
            payload["baseline_initialized"] = True
            payload["baseline_source"] = baseline_meta.get("source", "manual_ui")
            payload["baseline_updated_at"] = baseline_meta.get("updated_at", "")
        else:
            payload["candidate_baseline_fingerprints"] = list(payload.get("baseline_fingerprints", []))
            payload["baseline_fingerprints"] = []
            payload["expected_signal_count"] = 0
            payload["baseline_initialized"] = False
            payload["baseline_note"] = "场所基线尚未由操作员手动初始化；如需使用，请先在 UI 选项中初始化或编辑。"
        return payload

    def _clip_text(self, text: str, *, limit: int | None = None) -> str:
        text = " ".join(str(text).split())
        limit = limit or self._MAX_STRING_LENGTH
        if len(text) <= limit:
            return text
        return text[: limit - 3] + "..."

    def _summarize_attachments(self, attachments: list[Any] | None) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for item in list(attachments or [])[: self._MAX_LIST_ITEMS]:
            metadata = dict(getattr(item, "metadata", {}) or {})
            items.append(
                {
                    "kind": getattr(item, "kind", None),
                    "label": getattr(item, "label", None),
                    "uri": getattr(item, "uri", None),
                    "asset_id": metadata.get("asset_id"),
                    "upload_kind": metadata.get("upload_kind"),
                    "mime_type": metadata.get("mime_type"),
                    "database_id": metadata.get("database_id"),
                    "preview_asset_id": metadata.get("preview_asset_id"),
                }
            )
        return items
