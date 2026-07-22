from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from docx import Document

from deepem.agent.profiles import CAPTURE_AGENT
from deepem.nl2sql_config import NL2SQLSessionConfig
from deepem.protocol import (
    ChatMessage,
    ChatRole,
    Run,
    RunStatus,
    RunTriggerKind,
    new_id,
    utc_now,
)
from deepem.tools import ToolRegistry, register_builtin_tools
from deepem.tools.autonomous_usrp import AUTONOMOUS_OUTPUT_ROOT
from deepem.tools.base import ToolContext
from deepem.upload_processing import docx_to_markdown
from .capture_planner import (
    CaptureLLMPlanner,
    CapturePlanningUnavailable,
    CapturePlanValidationError,
)


CAPTURE_AGENT_ROOT = Path(__file__).resolve().parent / "data" / "capture_agent"
TASKS_DIR = CAPTURE_AGENT_ROOT / "tasks"
TEMPLATES_DIR = CAPTURE_AGENT_ROOT / "templates"
REPORTS_DIR = CAPTURE_AGENT_ROOT / "reports"

TERMINAL_TASK_STATES = {"completed", "failed", "cancelled"}
TERMINAL_NODE_STATES = {"completed", "failed", "cancelled", "skipped"}

_MISSING = object()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_name(value: str, fallback: str = "capture-template.docx") -> str:
    clean = Path(value or fallback).name
    clean = re.sub(r"[^0-9A-Za-z_.\-\u4e00-\u9fff]+", "_", clean).strip("._")
    return clean or fallback


def _deepcopy_json(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _freq_value(number: str, unit: str | None) -> float:
    """Return a frequency value normalized to MHz for all platform/planner fields."""
    value = float(number)
    unit_text = (unit or "mhz").lower()
    if unit_text in {"g", "ghz"}:
        return value * 1e3
    if unit_text in {"m", "mhz"}:
        return value
    if unit_text in {"k", "khz"}:
        return value / 1e3
    if unit_text in {"h", "hz"}:
        return value / 1e6
    return value


def _human_frequency(value: float | int | None) -> str:
    if value is None:
        return "-"
    number = float(value)
    return f"{number:g} MHz"


def _device_mhz_value(value: Any) -> float:
    number = float(value)
    return number / 1e6 if abs(number) > 100000 else number


class CapturePlanBuilder:
    """Explicit-parameter parsing and safety helpers around the real-LLM planner.

    This helper never supplies collection defaults. It extracts only values that
    are actually stated by the operator or a template, verifies the complete
    parameter set designed by the LLM, and enforces hard platform limits.
    """

    REQUIRED_CONSTRAINT_KEYS: tuple[str, ...] = (
        "room_name",
        "mode",
        "freq_start_mhz",
        "freq_stop_mhz",
        "freq_step_mhz",
        "dwell_time_sec",
        "repeat_count",
        "aggregation",
        "sample_rate",
        "bandwidth",
        "gain",
        "antenna",
        "expected_fft_frames_per_capture",
    )

    @classmethod
    def extract_constraints(cls, instruction: str, template_text: str = "") -> dict[str, Any]:
        source = f"{template_text}\n{instruction}".strip()
        result: dict[str, Any] = {}

        room_match = re.search(
            r"([\u4e00-\u9fffA-Za-z0-9_-]{1,32})\s*(会议室|房间|实验室|教室|机房|区域|room)",
            source,
            flags=re.I,
        )
        if room_match:
            prefix = re.sub(r"^(请|帮我|采集|检测|检查|扫描|对|在)", "", room_match.group(1)).strip()
            result["room_name"] = (prefix + room_match.group(2)).strip() or room_match.group(0).replace(" ", "")

        if any(keyword in source.lower() for keyword in ["背景", "background", "底噪", "空场"]):
            result["mode"] = "background"
        elif any(keyword in source.lower() for keyword in ["正常", "normal", "基线"]):
            result["mode"] = "normal"

        range_patterns = [
            r"([0-9]+(?:\.[0-9]+)?)\s*(GHz|G|MHz|M|kHz|K|Hz)\s*(?:-|~|到|至|—|–)\s*([0-9]+(?:\.[0-9]+)?)\s*(GHz|G|MHz|M|kHz|K|Hz)",
            r"(?:起始频率|start(?:_frequency)?)\s*[:：=]?\s*([0-9]+(?:\.[0-9]+)?)\s*(GHz|G|MHz|M|kHz|K|Hz).*?(?:终止频率|结束频率|stop(?:_frequency)?)\s*[:：=]?\s*([0-9]+(?:\.[0-9]+)?)\s*(GHz|G|MHz|M|kHz|K|Hz)",
        ]
        for pattern in range_patterns:
            match = re.search(pattern, source, flags=re.I | re.S)
            if match:
                result["freq_start_mhz"] = _freq_value(match.group(1), match.group(2))
                result["freq_stop_mhz"] = _freq_value(match.group(3), match.group(4))
                break

        single_freq = re.search(
            r"(?:中心频率|频点|frequency|freq)\s*[:：=]?\s*([0-9]+(?:\.[0-9]+)?)\s*(GHz|G|MHz|M|kHz|K|Hz)",
            source,
            flags=re.I,
        )
        if single_freq and not any(re.search(pattern, source, flags=re.I | re.S) for pattern in range_patterns):
            value = _freq_value(single_freq.group(1), single_freq.group(2))
            result["freq_start_mhz"] = value
            result["freq_stop_mhz"] = value

        mappings = [
            ("freq_step_mhz", r"(?:步长|步进|step)[^0-9]{0,16}([0-9]+(?:\.[0-9]+)?)\s*(GHz|G|MHz|M|kHz|K|Hz)", "freq"),
            ("sample_rate", r"(?:采样率|sample[_ ]?rate)[^0-9]{0,16}([0-9]+(?:\.[0-9]+)?)\s*(GHz|G|MHz|M|kHz|K|Hz)", "freq"),
            ("bandwidth", r"(?:带宽|bandwidth)[^0-9]{0,16}([0-9]+(?:\.[0-9]+)?)\s*(GHz|G|MHz|M|kHz|K|Hz)", "freq"),
            ("gain", r"(?:增益|gain)[^0-9]{0,16}([0-9]+(?:\.[0-9]+)?)\s*(?:dB)?", "number"),
        ]
        for key, pattern, kind in mappings:
            match = re.search(pattern, source, flags=re.I)
            if match:
                result[key] = _freq_value(match.group(1), match.group(2)) if kind == "freq" else float(match.group(1))

        dwell_match = re.search(
            r"(?:扫描时间|采集时间|驻留|dwell|duration)[^0-9]{0,16}([0-9]+(?:\.[0-9]+)?)\s*(ms|毫秒|s|秒)",
            source,
            flags=re.I,
        )
        if dwell_match:
            result["dwell_time_sec"] = float(dwell_match.group(1)) / 1000 if dwell_match.group(2).lower() in {"ms", "毫秒"} else float(dwell_match.group(1))

        repeat_match = re.search(r"(?:扫描次数|采集次数|重复|repeat)[^0-9]{0,16}([0-9]+)\s*(?:次)?", source, flags=re.I)
        if repeat_match:
            result["repeat_count"] = int(repeat_match.group(1))

        aggregation_match = re.search(
            r"(?:聚合方式|聚合|aggregation)\s*[:：=]?\s*(mean|median|均值|平均值|中位数)",
            source,
            flags=re.I,
        )
        if aggregation_match:
            aggregation_text = aggregation_match.group(1).lower()
            result["aggregation"] = "median" if aggregation_text in {"median", "中位数"} else "mean"

        fft_frames_match = re.search(
            r"(?:FFT\s*帧数|期望FFT帧数|expected[_ ]?fft[_ ]?frames(?:[_ ]?per[_ ]?capture)?)[^0-9]{0,16}([0-9]+)",
            source,
            flags=re.I,
        )
        if fft_frames_match:
            result["expected_fft_frames_per_capture"] = int(fft_frames_match.group(1))

        antenna_match = re.search(r"(?:天线|antenna)\s*[:：=]?\s*(TX/RX|RX2)", source, flags=re.I)
        if antenna_match:
            result["antenna"] = antenna_match.group(1).upper()

        for key in (
            "freq_start_mhz",
            "freq_stop_mhz",
            "freq_step_mhz",
            "dwell_time_sec",
            "sample_rate",
            "bandwidth",
            "gain",
        ):
            if key in result:
                result[key] = float(result[key])
        for key in ("repeat_count", "expected_fft_frames_per_capture"):
            if key in result:
                result[key] = int(result[key])
        return result

    @classmethod
    def normalize_constraints(cls, constraints: dict[str, Any]) -> dict[str, Any]:
        missing = [
            key
            for key in cls.REQUIRED_CONSTRAINT_KEYS
            if key not in constraints or constraints.get(key) is None or constraints.get(key) == ""
        ]
        if missing:
            raise ValueError(
                "采集参数不完整，禁止使用默认值补齐；请让意图分析器根据用户意图设计缺失字段："
                + "、".join(missing)
            )
        result = {key: constraints[key] for key in cls.REQUIRED_CONSTRAINT_KEYS}
        result["room_name"] = str(result["room_name"]).strip()
        result["mode"] = str(result["mode"]).strip()
        result["aggregation"] = str(result["aggregation"]).strip().lower()
        result["antenna"] = str(result["antenna"]).strip().upper()
        for key in (
            "freq_start_mhz",
            "freq_stop_mhz",
            "freq_step_mhz",
            "dwell_time_sec",
            "sample_rate",
            "bandwidth",
            "gain",
        ):
            result[key] = float(result[key])
        for key in ("repeat_count", "expected_fft_frames_per_capture"):
            result[key] = int(result[key])
        if not result["room_name"] or not result["mode"] or not result["antenna"]:
            raise ValueError("room_name、mode、antenna 必须由用户、模板或意图设计明确给出")
        if result["aggregation"] not in {"mean", "median"}:
            raise ValueError("aggregation 必须为 mean 或 median")
        return result

    @classmethod
    def validate_constraints(cls, constraints: dict[str, Any]) -> dict[str, Any]:
        normalized = cls.normalize_constraints(constraints)
        start = float(normalized["freq_start_mhz"])
        stop = float(normalized["freq_stop_mhz"])
        step = float(normalized["freq_step_mhz"])
        repeats = int(normalized["repeat_count"])
        dwell = float(normalized["dwell_time_sec"])
        if start <= 0 or stop <= 0 or stop < start:
            raise ValueError("频率范围无效：终止频率必须大于等于起始频率")
        if step <= 0:
            raise ValueError("频率步长必须大于 0")
        if repeats < 1 or repeats > 50:
            raise ValueError("重复次数必须在 1 到 50 之间")
        if dwell <= 0 or dwell > 60:
            raise ValueError("单频点驻留时间必须大于 0 且不超过 60 秒")
        count = int(math.floor((stop - start) / step)) + 1
        if count > 50000:
            raise ValueError(f"频点数量为 {count}，超过安全上限 50000；请增大步长或缩小频率范围")
        if float(normalized["bandwidth"]) > float(normalized["sample_rate"]):
            raise ValueError("带宽不能大于采样率")
        return {
            "frequency_count": count,
            "estimated_capture_seconds": round(count * repeats * dwell, 3),
            "range_text": f"{_human_frequency(start)} - {_human_frequency(stop)}",
            "step_text": _human_frequency(step),
        }

    @classmethod
    def build_bootstrap_nodes(cls) -> list[dict[str, Any]]:
        specs = [
            ("template_analysis", "解析采集模板", [], "解析 DOCX 的标题、段落和表格，构建发送给真实 LLM 的模板上下文。", None, "template_analysis"),
            ("instruction_analysis", "LLM 理解采集意图", ["template_analysis"], "真实 LLM 融合模板、用户指令和规则提示，输出经过 JSON Schema 校验的结构化意图。", None, "llm_intent_analysis"),
            ("plan_generation", "生成固定采集流程", ["instruction_analysis"], "调用标准化采集工具生成待确认参数与 plan_id，并构造固定的 B-tool 执行流程。", "prepare_spectrum_collection", "fixed_tool_plan_generation"),
            ("approval", "等待用户批准", ["plan_generation"], "用户批准前禁止执行真实采集；批准后锁定参数、plan_id 与固定工具流程。", None, "approval_gate"),
        ]
        now = utc_now_iso()
        return [
            {
                "id": node_id,
                "title": title,
                "description": description,
                "executor": executor,
                "dependencies": dependencies,
                "allowed_tool": tool,
                "allowed_tools": [tool] if tool else [],
                "status": "pending",
                "progress": 0,
                "attempts": 0,
                "started_at": None,
                "ended_at": None,
                "updated_at": now,
                "summary": "等待执行",
                "success_criteria": cls.success_criteria(node_id),
                "expected_evidence": [],
                "tool_input_overrides": {},
                "risk_level": "low",
                "planner_origin": "system_bootstrap",
                "inputs": {},
                "outputs": {},
                "logs": [],
                "notes": "",
            }
            for node_id, title, dependencies, description, tool, executor in specs
        ]

    @classmethod
    def build_nodes(cls) -> list[dict[str, Any]]:
        """Backward-compatible alias for bootstrap planning nodes."""
        return cls.build_bootstrap_nodes()

    @staticmethod
    def success_criteria(node_id: str) -> list[str]:
        criteria = {
            "template_analysis": ["全部选定 DOCX 已解析，或明确记录未上传模板"],
            "instruction_analysis": ["真实 LLM 返回符合 Schema 的采集意图", "参数来源、假设和未决问题可追踪"],
            "plan_generation": ["prepare_spectrum_collection 返回 plan_id", "采集参数已完成工具校验", "固定工具流程已生成并等待用户确认"],
            "approval": ["用户显式批准当前计划版本", "批准时锁定计划指纹"],
            "device_scan": ["已查询可用设备列表", "已选择空闲 USRP 设备或给出明确失败原因"],
            "collection_execution": ["execute_spectrum_collection 返回成功", "生成标准化 .npz 输出文件"],
            "output_file_check": ["输出文件存在", "输出文件扩展名为 .npz", "输出文件大小大于 0"],
            "execution_summary": ["形成采集任务执行摘要", "任务 JSON 与 Markdown 报告均已落盘"],
        }
        return criteria.get(node_id, [])


class CaptureAgentService:
    def __init__(self, platform: Any, root: Path | None = None, llm_client: Any | None = None) -> None:
        self.platform = platform
        runtime_llm = llm_client
        if runtime_llm is None:
            try:
                runtime_llm = platform.app.runtime.llm_client
            except Exception:
                runtime_llm = None
        self.planner = CaptureLLMPlanner(runtime_llm)
        self._tool_definitions = self._resolve_capture_tool_definitions()
        self.tool_catalog = {
            name: {
                "name": definition.name,
                "description": definition.description,
                "input_schema": definition.input_schema,
            }
            for name, definition in self._tool_definitions.items()
        }
        self.root = Path(root or CAPTURE_AGENT_ROOT)
        self.tasks_dir = self.root / "tasks"
        self.templates_dir = self.root / "templates"
        self.reports_dir = self.root / "reports"
        for directory in (self.root, self.tasks_dir, self.templates_dir, self.reports_dir):
            directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._workers: dict[str, threading.Thread] = {}
        self._restart_pending: set[str] = set()
        self._pause_events: dict[str, threading.Event] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._recover_interrupted_tasks()

    def runtime_info(self) -> dict[str, Any]:
        info = self.planner.runtime_info()
        return {**info, "tool_count": len(self.tool_catalog), "planning_mode": "fixed_spectrum_collection_tool_flow"}

    def _resolve_capture_tool_definitions(self) -> dict[str, Any]:
        registry = None
        try:
            registry = self.platform.app.runtime.tool_registry
        except Exception:
            registry = register_builtin_tools(ToolRegistry())
        definitions: dict[str, Any] = {}
        for name in CAPTURE_AGENT.allowed_tools:
            try:
                definitions[name] = registry.get(name)
            except Exception:
                continue
        if not definitions:
            fallback = register_builtin_tools(ToolRegistry())
            for name in CAPTURE_AGENT.allowed_tools:
                try:
                    definitions[name] = fallback.get(name)
                except Exception:
                    continue
        return definitions

    # ---------- template library ----------
    def list_templates(self) -> list[dict[str, Any]]:
        items = []
        with self._lock:
            for path in sorted(self.templates_dir.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    continue
                items.append(self._public_template(payload))
        return items

    def upload_template(self, filename: str, content: bytes) -> dict[str, Any]:
        safe_filename = _safe_name(filename)
        if Path(safe_filename).suffix.lower() != ".docx":
            raise ValueError("采集模板初版仅支持 .docx 文件")
        if not content:
            raise ValueError("上传文件为空")
        template_id = f"tpl_{uuid4().hex[:12]}"
        template_dir = self.templates_dir / template_id
        template_dir.mkdir(parents=True, exist_ok=False)
        original_path = template_dir / safe_filename
        original_path.write_bytes(content)
        try:
            document = Document(str(original_path))
            markdown = docx_to_markdown(document)
            headings = []
            tables = []
            for paragraph in document.paragraphs:
                text = paragraph.text.strip()
                if text and str(paragraph.style.name or "").lower().startswith("heading"):
                    headings.append(text)
            for table_index, table in enumerate(document.tables, start=1):
                rows = [[cell.text.strip() for cell in row.cells] for row in table.rows]
                tables.append({"index": table_index, "rows": rows})
        except Exception as exc:
            shutil.rmtree(template_dir, ignore_errors=True)
            raise ValueError(f"DOCX 解析失败：{exc}") from exc
        now = utc_now_iso()
        payload = {
            "id": template_id,
            "name": Path(safe_filename).stem,
            "file_name": safe_filename,
            "file_path": str(original_path),
            "size_bytes": len(content),
            "markdown": markdown,
            "text_preview": markdown[:1200],
            "headings": headings[:100],
            "tables": tables[:50],
            "created_at": now,
            "updated_at": now,
        }
        self._write_json(self.templates_dir / f"{template_id}.json", payload)
        return self._public_template(payload)

    def get_template(self, template_id: str) -> dict[str, Any]:
        path = self.templates_dir / f"{template_id}.json"
        if not path.exists():
            raise KeyError(template_id)
        return json.loads(path.read_text(encoding="utf-8"))

    def get_template_detail(self, template_id: str) -> dict[str, Any]:
        with self._lock:
            payload = self.get_template(template_id)
            public = self._public_template(payload)
            return {
                **public,
                "markdown": payload.get("markdown", ""),
                "text": payload.get("markdown", ""),
                "tables": payload.get("tables", []),
            }

    def rename_template(self, template_id: str, name: str) -> dict[str, Any]:
        display_name = str(name or "").strip()
        if not display_name:
            raise ValueError("模板名称不能为空")
        safe_filename = _safe_name(display_name if display_name.lower().endswith(".docx") else f"{display_name}.docx")
        with self._lock:
            payload = self.get_template(template_id)
            old_path = Path(str(payload.get("file_path") or ""))
            new_path = old_path.with_name(safe_filename) if old_path.name else self.templates_dir / template_id / safe_filename
            if old_path.exists() and old_path.resolve() != new_path.resolve():
                if new_path.exists():
                    new_path = new_path.with_name(f"{new_path.stem}_{uuid4().hex[:6]}{new_path.suffix}")
                    safe_filename = new_path.name
                old_path.rename(new_path)
                payload["file_path"] = str(new_path)
            payload["name"] = Path(safe_filename).stem
            payload["file_name"] = safe_filename
            payload["updated_at"] = utc_now_iso()
            self._write_json(self.templates_dir / f"{template_id}.json", payload)
            return self._public_template(payload)

    def delete_template(self, template_id: str) -> None:
        with self._lock:
            payload = self.get_template(template_id)
            json_path = self.templates_dir / f"{template_id}.json"
            template_dir = Path(str(payload.get("file_path") or "")).parent
            if json_path.exists():
                json_path.unlink()
            if template_dir.exists() and template_dir.parent.resolve() == self.templates_dir.resolve():
                shutil.rmtree(template_dir, ignore_errors=True)

    # ---------- task lifecycle ----------
    def list_tasks(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            payloads = self._all_task_payloads_locked()
            items = []
            for index, payload in enumerate(payloads, start=1):
                summary = self._task_summary(payload)
                summary.update(self._task_display_fields(payload, index))
                items.append(summary)
                if len(items) >= limit:
                    break
        return items

    def create_task(self, instruction: str, template_ids: list[str]) -> dict[str, Any]:
        self.planner.ensure_available()
        instruction = str(instruction or "").strip()
        if not instruction:
            raise ValueError("请输入采集任务指令")
        selected_templates = []
        for template_id in template_ids:
            selected_templates.append(self.get_template(template_id))
        task_id = f"capture_{uuid4().hex[:12]}"
        output_dir = AUTONOMOUS_OUTPUT_ROOT / task_id
        output_dir.mkdir(parents=True, exist_ok=True)
        now = utc_now_iso()
        task = {
            "id": task_id,
            "title": instruction[:48],
            "instruction": instruction,
            "template_ids": [item["id"] for item in selected_templates],
            "templates": [self._public_template(item) for item in selected_templates],
            "output_dir": str(output_dir.resolve()),
            "status": "planning",
            "pause_reason": None,
            "plan_version": 1,
            "plan_locked": False,
            "approved_at": None,
            "approved_by": None,
            "current_node_id": "template_analysis",
            "selected_node_id": "template_analysis",
            "progress": 0,
            "constraints": {},
            "constraint_sources": {},
            "validation": {},
            "intent_analysis": {},
            "plan_candidate": {},
            "plan_fingerprint": None,
            "approved_plan_fingerprint": None,
            "plan_history": [],
            "planner": self.planner.runtime_info(),
            "nodes": CapturePlanBuilder.build_bootstrap_nodes(),
            "messages": [
                {
                    "id": f"msg_{uuid4().hex[:10]}",
                    "role": "operator",
                    "content": instruction,
                    "created_at": now,
                    "template_ids": [item["id"] for item in selected_templates],
                }
            ],
            "events": [],
            "artifacts": [],
            "generated_code": "",
            "execution_result": {},
            "error": None,
            "created_at": now,
            "updated_at": now,
        }
        self._append_event_locked(task, "task_created", "已创建采集智能体任务，开始生成任务规划。", node_id="template_analysis")
        self._save_task_locked(task)
        self._start_worker(task_id, mode="planning")
        return self._public_task(task)

    def get_task(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            payload = self._load_task_locked(task_id)
            item = self._public_task(payload)
            index = self._task_index_locked(task_id)
            if index is not None:
                item.update(self._task_display_fields(payload, index))
            return item

    def rename_task(self, task_id: str, title: str) -> dict[str, Any]:
        clean_title = str(title or "").strip()
        if not clean_title:
            raise ValueError("任务名称不能为空")
        with self._lock:
            task = self._load_task_locked(task_id)
            task["title"] = clean_title[:120]
            task["updated_at"] = utc_now_iso()
            self._append_event_locked(task, "task_renamed", f"任务已重命名为：{task['title']}", node_id=task.get("current_node_id"))
            self._save_task_locked(task)
        return self.get_task(task_id)

    def delete_task(self, task_id: str) -> None:
        with self._lock:
            task = self._load_task_locked(task_id)
            self._cancel_events.setdefault(task_id, threading.Event()).set()
            output_dir = Path(str(task.get("output_dir") or ""))
            task_path = self.tasks_dir / f"{task_id}.json"
            if task_path.exists():
                task_path.unlink()
            if output_dir.exists() and output_dir.is_dir():
                try:
                    shutil.rmtree(output_dir, ignore_errors=True)
                except Exception:
                    pass
            self._workers.pop(task_id, None)
            self._pause_events.pop(task_id, None)

    def update_approval_parameters(self, task_id: str, parameters: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            task = self._load_task_locked(task_id)
            if task.get("status") != "awaiting_approval" or task.get("plan_locked"):
                raise ValueError("只有待批准且未锁定的任务可以修改采集参数")
            constraints = self._coerce_approval_constraints(dict(task.get("constraints") or {}), dict(parameters or {}))
            validation = CapturePlanBuilder.validate_constraints(constraints)
            task["constraints"] = constraints
            task["validation"] = validation
            sources = dict(task.get("constraint_sources") or {})
            for key in parameters:
                sources[key] = "operator_approval_edit"
            task["constraint_sources"] = sources
            task["updated_at"] = utc_now_iso()
            self._save_task_locked(task)

        prepare_result = self._prepare_spectrum_collection_plan(task_id, constraints)
        compiled_nodes = self._fixed_collection_nodes(prepare_result)
        with self._lock:
            current = self._load_task_locked(task_id)
            if current.get("status") != "awaiting_approval" or current.get("plan_locked"):
                raise ValueError("计划状态已变化，参数修改未应用")
            bootstrap_ids = {"template_analysis", "instruction_analysis", "plan_generation", "approval"}
            bootstrap_by_id = {node["id"]: node for node in current.get("nodes", []) if node.get("id") in bootstrap_ids}
            ordered_bootstrap = [bootstrap_by_id[node_id] for node_id in ("template_analysis", "instruction_analysis", "plan_generation", "approval") if node_id in bootstrap_by_id]
            proposal = dict(current.get("plan_candidate") or {})
            proposal["prepare_result"] = prepare_result
            proposal["plan_steps_text"] = self._fixed_flow_text(compiled_nodes)
            proposal["operator_parameter_edits"] = dict(parameters or {})
            current["constraints"] = constraints
            current["validation"] = validation
            current["nodes"] = ordered_bootstrap + compiled_nodes
            current["plan_candidate"] = proposal
            current["approved_plan_fingerprint"] = None
            current["plan_fingerprint"] = self._task_plan_fingerprint(current)
            history = current.setdefault("plan_history", [])
            if history:
                history[-1]["proposal"] = proposal
                history[-1]["compiled_nodes"] = [
                    {
                        "id": node.get("id"),
                        "title": node.get("title"),
                        "kind": node.get("kind"),
                        "executor": node.get("executor"),
                        "dependencies": node.get("dependencies", []),
                        "tool_name": node.get("tool_name"),
                        "tool_arguments": node.get("tool_arguments", {}),
                        "allowed_tools": node.get("allowed_tools", []),
                        "success_criteria": node.get("success_criteria", []),
                        "planner_origin": node.get("planner_origin"),
                    }
                    for node in compiled_nodes
                ]
                history[-1]["plan_fingerprint"] = current["plan_fingerprint"]
                history[-1]["operator_parameter_edits"] = dict(parameters or {})
            approval = self._node(current, "approval")
            approval.update({"status": "blocked", "progress": 0, "updated_at": utc_now_iso(), "summary": "参数已由用户修改，等待重新批准"})
            approval["logs"].append(self._log_entry("用户在审批界面修改了采集参数，已重新生成 plan_id 与固定执行流程。", "warning", parameters))
            current["current_node_id"] = "approval"
            current["updated_at"] = utc_now_iso()
            self._append_event_locked(current, "approval_parameters_updated", "审批参数已更新，固定采集流程已重新生成。", node_id="approval", data={"validation": validation})
            self._save_task_locked(current)
        return self.get_task(task_id)

    def get_task_internal(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            return _deepcopy_json(self._load_task_locked(task_id))

    def approve_task(self, task_id: str, approved_by: str = "operator") -> dict[str, Any]:
        with self._lock:
            task = self._load_task_locked(task_id)
            if task["status"] != "awaiting_approval":
                raise ValueError("当前任务不处于待批准状态")
            if self._plan_requires_device_access(task):
                conflict = self._find_conflicting_real_task_locked(task_id)
                if conflict is not None:
                    raise ValueError(f"真实设备当前已被任务 {conflict.get('id')} 占用或保留，请先完成/取消该任务")
            current_fingerprint = self._task_plan_fingerprint(task)
            expected_fingerprint = str(task.get("plan_fingerprint") or "")
            if not expected_fingerprint or current_fingerprint != expected_fingerprint:
                raise ValueError("计划结构在审批前发生变化，必须重新生成计划版本")
            node = self._node(task, "approval")
            node.update(
                {
                    "status": "completed",
                    "progress": 100,
                    "started_at": node.get("started_at") or utc_now_iso(),
                    "ended_at": utc_now_iso(),
                    "updated_at": utc_now_iso(),
                    "summary": f"固定工具流程 v{task['plan_version']} 已由用户批准并锁定指纹",
                    "outputs": {
                        "approved_by": approved_by,
                        "plan_version": task["plan_version"],
                        "plan_fingerprint": expected_fingerprint,
                    },
                }
            )
            node["logs"].append(self._log_entry("用户批准了 B 方案固定采集流程；执行器已锁定 plan_id、节点依赖、工具名称与工具参数。", "success"))
            task["status"] = "running"
            task["pause_reason"] = None
            task["plan_locked"] = True
            task["approved_at"] = utc_now_iso()
            task["approved_by"] = approved_by
            task["approved_plan_fingerprint"] = expected_fingerprint
            execution_nodes = self._execution_nodes_in_order(task)
            task["current_node_id"] = execution_nodes[0]["id"] if execution_nodes else "approval"
            self._append_event_locked(task, "plan_approved", "固定采集工具流程已批准并锁定，开始严格按依赖执行。", node_id="approval")
            self._save_task_locked(task)
        self._start_worker(task_id, mode="execution")
        return self.get_task(task_id)

    def pause_task(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            task = self._load_task_locked(task_id)
            if task["status"] not in {"running", "planning"}:
                raise ValueError("只有规划中或执行中的任务可以暂停")
            task["status"] = "paused"
            task["pause_reason"] = "manual"
            pause_event = self._pause_events.setdefault(task_id, threading.Event())
            pause_event.set()
            self._append_event_locked(task, "task_paused", "用户已暂停任务；当前工具调用完成后不会进入下一节点。", node_id=task.get("current_node_id"))
            self._save_task_locked(task)
        return self.get_task(task_id)

    def resume_task(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            task = self._load_task_locked(task_id)
            if task["status"] != "paused":
                raise ValueError("当前任务未暂停")
            if task.get("plan_locked") and self._plan_requires_device_access(task):
                conflict = self._find_conflicting_real_task_locked(task_id)
                if conflict is not None:
                    raise ValueError(f"真实设备当前已被任务 {conflict.get('id')} 占用或保留，请先完成/取消该任务")
            if task.get("pause_reason") == "node_failure" and task.get("current_node_id"):
                failed_node = self._node(task, str(task["current_node_id"]))
                if failed_node.get("status") == "failed":
                    failed_node.update(
                        {
                            "status": "pending",
                            "progress": 0,
                            "ended_at": None,
                            "updated_at": utc_now_iso(),
                            "summary": "失败节点已重置，等待继续执行",
                        }
                    )
                    failed_node["logs"].append(self._log_entry("用户继续任务，失败节点已重置为待执行。", "warning"))
            task["status"] = "planning" if not task.get("plan_locked") else "running"
            task["pause_reason"] = None
            self._pause_events.setdefault(task_id, threading.Event()).clear()
            self._append_event_locked(task, "task_resumed", "任务已继续，将从未完成节点恢复。", node_id=task.get("current_node_id"))
            self._save_task_locked(task)
        self._start_worker(task_id, mode="planning" if not task.get("plan_locked") else "execution")
        return self.get_task(task_id)

    def cancel_task(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            task = self._load_task_locked(task_id)
            if task["status"] in TERMINAL_TASK_STATES:
                return self._public_task(task)
            self._cancel_events.setdefault(task_id, threading.Event()).set()
            task["status"] = "cancelled"
            current = task.get("current_node_id")
            if current:
                node = self._node(task, current)
                if node["status"] not in TERMINAL_NODE_STATES:
                    node.update({"status": "cancelled", "ended_at": utc_now_iso(), "updated_at": utc_now_iso(), "summary": "用户取消任务"})
            self._append_event_locked(task, "task_cancelled", "用户已取消采集任务。", node_id=current)
            self._save_task_locked(task)
        return self.get_task(task_id)

    def add_message(self, task_id: str, content: str) -> dict[str, Any]:
        self.planner.ensure_available()
        content = str(content or "").strip()
        if not content:
            raise ValueError("消息不能为空")
        with self._lock:
            task = self._load_task_locked(task_id)
            allowed_revision_states = {"awaiting_approval", "failed", "completed", "cancelled"}
            if task.get("status") == "paused" and task.get("pause_reason") == "node_failure":
                allowed_revision_states.add("paused")
            if task["status"] not in allowed_revision_states:
                raise ValueError("任务正在执行；请先暂停或等待完成，再追加新指令")
            task["messages"].append(
                {"id": f"msg_{uuid4().hex[:10]}", "role": "operator", "content": content, "created_at": utc_now_iso(), "template_ids": []}
            )
            task["instruction"] = f"{task['instruction']}\n追加要求：{content}"
            task["plan_version"] = int(task.get("plan_version") or 1) + 1
            task["plan_locked"] = False
            task["approved_at"] = None
            task["approved_by"] = None
            task["status"] = "planning"
            task["pause_reason"] = None
            task["error"] = None
            task["generated_code"] = ""
            task["execution_result"] = {}
            task["approved_plan_fingerprint"] = None
            task["plan_fingerprint"] = None
            task["plan_candidate"] = {}
            task["intent_analysis"] = {}
            task["constraint_sources"] = {}
            task["nodes"] = CapturePlanBuilder.build_bootstrap_nodes()
            task["current_node_id"] = "template_analysis"
            self._cancel_events.setdefault(task_id, threading.Event()).clear()
            self._pause_events.setdefault(task_id, threading.Event()).clear()
            self._append_event_locked(task, "plan_revision_started", f"收到追加指令，开始生成计划版本 v{task['plan_version']}。", node_id="template_analysis")
            self._save_task_locked(task)
        self._start_worker(task_id, mode="planning")
        return self.get_task(task_id)

    def save_node_note(self, task_id: str, node_id: str, note: str) -> dict[str, Any]:
        with self._lock:
            task = self._load_task_locked(task_id)
            node = self._node(task, node_id)
            node["notes"] = str(note or "")
            node["updated_at"] = utc_now_iso()
            self._append_event_locked(task, "node_note_saved", f"已保存节点“{node['title']}”的人工备注。", node_id=node_id)
            self._save_task_locked(task)
        return self.get_task(task_id)

    def events_since(self, task_id: str, after_seq: int = 0) -> list[dict[str, Any]]:
        with self._lock:
            task = self._load_task_locked(task_id)
            return [item for item in task.get("events", []) if int(item.get("seq") or 0) > after_seq]


    def list_npz_artifacts(self, *, limit: int = 50000) -> list[dict[str, Any]]:
        """Return all known .npz capture products from capture-agent tasks and output folders."""
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        with self._lock:
            payloads = self._all_task_payloads_locked()
            task_index = {str(task.get("id") or ""): index for index, task in enumerate(payloads, start=1)}
            for task in reversed(payloads):
                task_id = str(task.get("id") or "")
                task_title = str(task.get("title") or task.get("instruction") or task_id)
                display = self._task_display_fields(task, task_index.get(task_id, 0) or 0).get("display_title") if task_id else task_title
                for artifact in task.get("artifacts", []) or []:
                    file_name = str(artifact.get("file_name") or "")
                    path = Path(str(artifact.get("path") or "")).expanduser().resolve()
                    if path.suffix.lower() != ".npz" or not path.exists() or not path.is_file():
                        continue
                    resolved = str(path)
                    if resolved in seen:
                        continue
                    seen.add(resolved)
                    items.append({
                        "id": f"artifact:{task_id}:{artifact.get('id')}",
                        "source": "capture_artifact",
                        "task_id": task_id,
                        "task_title": task_title,
                        "task_display_title": display or task_title,
                        "artifact_id": artifact.get("id"),
                        "file_name": path.name,
                        "path": resolved,
                        "size_bytes": path.stat().st_size,
                        "created_at": artifact.get("created_at") or task.get("created_at"),
                        "updated_at": artifact.get("updated_at") or task.get("updated_at"),
                    })
                    if len(items) >= limit:
                        return items
            scan_roots = [AUTONOMOUS_OUTPUT_ROOT, self.platform._uploads_root if hasattr(self.platform, "_uploads_root") else None]
            for root in scan_roots:
                if root is None:
                    continue
                root_path = Path(root).expanduser().resolve()
                if not root_path.exists():
                    continue
                for path in sorted(root_path.rglob("*.npz"), key=lambda item: item.stat().st_mtime if item.exists() else 0, reverse=True):
                    if not path.exists() or not path.is_file():
                        continue
                    resolved = str(path.resolve())
                    if resolved in seen:
                        continue
                    seen.add(resolved)
                    items.append({
                        "id": f"file:{hashlib.sha1(resolved.encode('utf-8')).hexdigest()[:16]}",
                        "source": "npz_file",
                        "task_id": "",
                        "task_title": "",
                        "task_display_title": "本地 NPZ 文件",
                        "artifact_id": "",
                        "file_name": path.name,
                        "path": resolved,
                        "size_bytes": path.stat().st_size,
                        "created_at": utc_now_iso(),
                        "updated_at": utc_now_iso(),
                    })
                    if len(items) >= limit:
                        return items
        return items

    def resolve_artifact(self, task_id: str, artifact_id: str) -> tuple[Path, str]:
        with self._lock:
            task = self._load_task_locked(task_id)
            artifact = next((item for item in task.get("artifacts", []) if item.get("id") == artifact_id), None)
            if artifact is None:
                raise KeyError(artifact_id)
            path = Path(str(artifact.get("path") or "")).resolve()
            if not path.exists() or not path.is_file():
                raise FileNotFoundError(path)
            return path, str(artifact.get("file_name") or path.name)

    # ---------- workers ----------
    def _start_worker(self, task_id: str, mode: str) -> None:
        with self._lock:
            active = self._workers.get(task_id)
            if active is not None and active.is_alive():
                if task_id not in self._restart_pending:
                    self._restart_pending.add(task_id)
                    waiter = threading.Thread(
                        target=self._restart_worker_after,
                        args=(task_id, active, mode),
                        daemon=True,
                        name=f"capture-agent-restart-{task_id[-6:]}",
                    )
                    waiter.start()
                return
            self._pause_events.setdefault(task_id, threading.Event())
            self._cancel_events.setdefault(task_id, threading.Event())
            thread = threading.Thread(target=self._worker, args=(task_id, mode), daemon=True, name=f"capture-agent-{task_id[-6:]}")
            self._workers[task_id] = thread
            thread.start()

    def _restart_worker_after(self, task_id: str, active: threading.Thread, requested_mode: str) -> None:
        active.join()
        should_restart = False
        mode = requested_mode
        with self._lock:
            self._restart_pending.discard(task_id)
            try:
                task = self._load_task_locked(task_id)
            except KeyError:
                return
            if task.get("status") in {"planning", "running"}:
                should_restart = True
                mode = "execution" if task.get("plan_locked") else "planning"
        if should_restart:
            self._start_worker(task_id, mode)

    def _worker(self, task_id: str, mode: str) -> None:
        try:
            if mode == "planning":
                self._run_planning(task_id)
            else:
                self._run_execution(task_id)
        except Exception as exc:
            with self._lock:
                try:
                    task = self._load_task_locked(task_id)
                except KeyError:
                    return
                if task.get("status") == "cancelled":
                    return
                current = task.get("current_node_id")
                if current:
                    node = self._node(task, current)
                    if node["status"] not in TERMINAL_NODE_STATES:
                        node.update(
                            {
                                "status": "failed",
                                "progress": node.get("progress", 0),
                                "ended_at": utc_now_iso(),
                                "updated_at": utc_now_iso(),
                                "summary": f"节点失败：{exc}",
                            }
                        )
                        node["logs"].append(self._log_entry(str(exc), "error"))
                task["status"] = "failed"
                task["error"] = str(exc)
                self._append_event_locked(task, "task_failed", f"任务失败：{exc}", node_id=current, level="error")
                self._save_task_locked(task)

    def _run_planning(self, task_id: str) -> None:
        planning_nodes = ["template_analysis", "instruction_analysis", "plan_generation"]
        for node_id in planning_nodes:
            if self._should_stop(task_id):
                return
            self._wait_if_paused(task_id)
            with self._lock:
                task = self._load_task_locked(task_id)
                if task["status"] == "cancelled":
                    return
                node = self._node(task, node_id)
                if node["status"] == "completed":
                    continue
                self._assert_dependencies(task, node)
                self._begin_node_locked(task, node, f"开始{node['title']}。")
                self._save_task_locked(task)
            if node_id == "template_analysis":
                output = self._analyze_templates(task_id)
                self._complete_node(task_id, node_id, "模板解析完成。", output)
            elif node_id == "instruction_analysis":
                output = self._analyze_instruction(task_id)
                self._complete_node(task_id, node_id, "采集约束提取完成。", output)
            else:
                output = self._generate_plan_summary(task_id)
                self._complete_node(task_id, node_id, "结构化任务规划已生成。", output)

        with self._lock:
            task = self._load_task_locked(task_id)
            if task["status"] == "cancelled":
                return
            self._preflight_plan_locked(task)
            approval = self._node(task, "approval")
            approval.update(
                {
                    "status": "blocked",
                    "progress": 0,
                    "updated_at": utc_now_iso(),
                    "summary": f"等待用户批准固定采集流程 v{task['plan_version']}",
                    "inputs": {"plan_version": task["plan_version"], "requires_explicit_approval": True},
                }
            )
            approval["logs"].append(self._log_entry("批准前，execute_spectrum_collection 被状态机阻止；prepare 阶段不会启动真实采集。", "warning"))
            task["status"] = "awaiting_approval"
            task["current_node_id"] = "approval"
            task["progress"] = self._task_progress(task)
            self._append_event_locked(task, "plan_ready", "采集参数与固定工具流程已生成，请检查后批准执行。", node_id="approval")
            self._save_task_locked(task)

    def _run_execution(self, task_id: str) -> None:
        while True:
            if self._should_stop(task_id):
                return
            self._wait_if_paused(task_id)
            with self._lock:
                task = self._load_task_locked(task_id)
                if task["status"] == "cancelled":
                    return
                if not task.get("plan_locked"):
                    raise RuntimeError("计划未锁定，执行器拒绝启动")
                self._assert_plan_integrity_locked(task)
                execution_nodes = self._execution_nodes_in_order(task)
                pending = [node for node in execution_nodes if node.get("status") not in TERMINAL_NODE_STATES]
                if not pending:
                    self._persist_task_bundle_locked(task)
                    task["status"] = "completed"
                    task["pause_reason"] = None
                    task["progress"] = 100
                    task["current_node_id"] = execution_nodes[-1]["id"] if execution_nodes else "approval"
                    task["messages"].append(
                        {
                            "id": f"msg_{uuid4().hex[:10]}",
                            "role": "assistant",
                            "content": "任务已按 B 方案固定采集工具流程完成，任务目录中的结果与审计文件已统一保存。",
                            "created_at": utc_now_iso(),
                            "template_ids": [],
                        }
                    )
                    self._append_event_locked(task, "task_completed", "固定采集工具流程的所有节点均已完成，统一任务目录已写入审计产物。", node_id=task["current_node_id"])
                    self._save_task_locked(task)
                    return

                ready = []
                for candidate in pending:
                    try:
                        self._assert_dependencies(task, candidate)
                        ready.append(candidate)
                    except RuntimeError:
                        continue
                if not ready:
                    blocked = ", ".join(node["id"] for node in pending)
                    raise RuntimeError(f"剩余节点没有可执行项，依赖状态异常：{blocked}")
                node = ready[0]
                node_id = node["id"]
                self._begin_node_locked(task, node, f"开始{node['title']}。")
                self._save_task_locked(task)

            max_attempts = max(1, min(3, int(node.get("max_attempts") or 2)))
            completed = False
            for attempt_index in range(max_attempts):
                try:
                    output, summary = self._execute_planned_node(task_id, node_id)
                    self._complete_node(task_id, node_id, summary, output)
                    completed = True
                    break
                except Exception as exc:
                    if self._should_stop(task_id):
                        return
                    with self._lock:
                        current = self._load_task_locked(task_id)
                        failed_node = self._node(current, node_id)
                        failed_node["logs"].append(self._log_entry(f"第 {attempt_index + 1} 次执行失败：{exc}", "error"))
                        failed_node["updated_at"] = utc_now_iso()
                        if attempt_index + 1 < max_attempts:
                            failed_node["status"] = "pending"
                            failed_node["progress"] = 0
                            failed_node["summary"] = f"节点执行失败，准备第 {attempt_index + 2} 次尝试"
                            self._append_event_locked(current, "node_retry", f"节点“{failed_node['title']}”执行失败，将重试：{exc}", node_id=node_id, level="warning")
                            self._save_task_locked(current)
                        else:
                            failed_node["status"] = "failed"
                            failed_node["ended_at"] = utc_now_iso()
                            failed_node["summary"] = f"达到最大尝试次数后仍失败：{exc}"
                            current["status"] = "paused"
                            current["pause_reason"] = "node_failure"
                            current["error"] = str(exc)
                            self._pause_events.setdefault(task_id, threading.Event()).set()
                            self._append_event_locked(current, "task_paused_on_failure", f"节点“{failed_node['title']}”失败，任务已暂停，可追加指令让 LLM 生成新计划版本：{exc}", node_id=node_id, level="error")
                            self._save_task_locked(current)
                    if attempt_index + 1 < max_attempts:
                        time.sleep(0.35)
                        with self._lock:
                            current = self._load_task_locked(task_id)
                            self._assert_plan_integrity_locked(current)
                            retry_node = self._node(current, node_id)
                            self._begin_node_locked(current, retry_node, f"重试{retry_node['title']}。")
                            self._save_task_locked(current)
                        continue
                    return
            if not completed:
                return

    def _execute_planned_node(self, task_id: str, node_id: str) -> tuple[dict[str, Any], str]:
        task = self.get_task_internal(task_id)
        node = self._node(task, node_id)
        executor = str(node.get("executor") or "")
        if executor == "device_scan":
            return self._execute_device_scan(task_id, node_id)
        if executor == "output_file_check":
            return self._execute_output_file_check(task_id, node_id)
        if executor == "execution_summary":
            return self._execute_execution_summary(task_id, node_id)
        kind = str(node.get("kind") or node.get("executor") or "")
        if kind == "tool_call":
            return self._execute_freeform_tool_node(task_id, node_id)
        if kind in {"llm_analysis", "llm_verification", "decision"}:
            return self._execute_freeform_llm_node(task_id, node_id)
        raise RuntimeError(f"节点 {node_id} 使用了不支持的 kind：{kind}")

    def _execute_freeform_llm_node(self, task_id: str, node_id: str) -> tuple[dict[str, Any], str]:
        task = self.get_task_internal(task_id)
        node = self._node(task, node_id)
        dependency_outputs = {
            dep: dict(self._node(task, dep).get("outputs") or {})
            for dep in node.get("dependencies", [])
            if dep != "approval"
        }
        task_context = {
            "id": task["id"],
            "instruction": task["instruction"],
            "constraints": task.get("constraints", {}),
            "intent_analysis": task.get("intent_analysis", {}),
            "plan_version": task.get("plan_version"),
            "completion_contract": (task.get("plan_candidate") or {}).get("completion_contract", []),
            "output_dir": task.get("output_dir"),
        }
        response = self.planner.execute_llm_node(
            task_context=task_context,
            node={
                "id": node.get("id"),
                "title": node.get("title"),
                "description": node.get("description"),
                "kind": node.get("kind"),
                "success_criteria": node.get("success_criteria", []),
                "expected_evidence": node.get("expected_evidence", []),
            },
            dependency_outputs=dependency_outputs,
            stream_handler=self._planner_stream_handler(task_id, node_id),
            cancel_checker=lambda: self._should_stop(task_id),
        )
        result = dict(response.get("result") or {})
        status = str(result.get("status") or "failed")
        if status == "blocked":
            raise RuntimeError(result.get("summary") or "LLM 节点被阻塞")
        if status != "completed":
            raise RuntimeError(result.get("summary") or "LLM 节点执行失败")
        if result.get("should_replan"):
            with self._lock:
                current = self._load_task_locked(task_id)
                self._append_event_locked(
                    current,
                    "llm_replan_suggested",
                    str(result.get("replan_reason") or "LLM 建议修订后续计划"),
                    node_id=node_id,
                    level="warning",
                )
                self._save_task_locked(current)
        return {**result, "raw_model_output": response.get("raw_model_output"), "model": response.get("model")}, str(result.get("summary") or "LLM 节点完成。")

    def _execute_freeform_tool_node(self, task_id: str, node_id: str) -> tuple[dict[str, Any], str]:
        task = self.get_task_internal(task_id)
        node = self._node(task, node_id)
        tool_name = str(node.get("tool_name") or node.get("allowed_tool") or "")
        if tool_name not in self._tool_definitions:
            raise RuntimeError(f"计划引用的工具不可用：{tool_name}")

        raw_args = self._inject_capture_tool_context(tool_name, dict(node.get("tool_arguments") or {}), task)
        args, resolution_notes = self._resolve_tool_arguments(tool_name, raw_args, task, node)
        output = self._execute_tool(task_id, node_id, tool_name, args)

        with self._lock:
            current = self._load_task_locked(task_id)
            current_node = self._node(current, node_id)
            if resolution_notes:
                current_node["logs"].append(
                    self._log_entry("已按已批准任务参数解析计划参数：" + "；".join(resolution_notes), "warning")
                )
            if tool_name == "generate_usrp_task_code" and output.get("code"):
                current["generated_code"] = str(output.get("code") or "")
            if tool_name == "run_autonomous_usrp_task" and output.get("generated_code"):
                current["generated_code"] = str(output.get("generated_code") or "")
            execution_result = output.get("execution_result") if isinstance(output.get("execution_result"), dict) else None
            if execution_result:
                current["execution_result"] = dict(execution_result)
            if tool_name == "execute_spectrum_collection":
                current["execution_result"] = dict(output)
                output_file = output.get("output_file")
                if output_file:
                    self._add_artifact_locked(current, Path(str(output_file)), "collection_result")
            for path in self._discover_output_files(output):
                self._add_artifact_locked(current, path, "tool_output")
            self._save_task_locked(current)

        summary = f"工具 {tool_name} 执行完成。"
        if tool_name == "execute_spectrum_collection":
            summary = (
                f"标准频谱采集完成：成功 {output.get('success_count', '-')} / "
                f"{output.get('freq_count', '-')} 个频点，输出 {Path(str(output.get('output_file') or '')).name or '-'}。"
            )
        if isinstance(output.get("execution_result"), dict):
            execution = output["execution_result"]
            summary = f"工具 {tool_name} 执行完成，状态：{execution.get('status', 'unknown')}。"
        return {
            "tool_name": tool_name,
            "resolved_arguments": args,
            "argument_resolution_notes": resolution_notes,
            "result": output,
            **output,
        }, summary

    def _inject_capture_tool_context(self, tool_name: str, args: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
        result = _deepcopy_json(args)
        if tool_name in {"generate_usrp_task_code", "execute_usrp_task_code", "run_autonomous_usrp_task"}:
            result.setdefault("task_description", self._tool_task_description(task))
            if not isinstance(result.get("task_plan"), dict):
                result["task_plan"] = self._autonomous_tool_plan(task)
        if tool_name == "execute_usrp_task_code" and not result.get("code"):
            result["code"] = str(task.get("generated_code") or "")
        return result

    def _resolve_tool_arguments(
        self,
        tool_name: str,
        raw_args: dict[str, Any],
        task: dict[str, Any],
        node: dict[str, Any],
    ) -> tuple[dict[str, Any], list[str]]:
        definition = self._tool_definitions[tool_name]
        schema = dict(definition.input_schema or {})
        properties = dict(schema.get("properties") or {})
        required = set(schema.get("required") or [])
        resolved: dict[str, Any] = {}
        notes: list[str] = []

        for key, value in raw_args.items():
            try:
                resolved[key] = self._resolve_plan_value(value, task)
            except RuntimeError as exc:
                inferred = self._infer_tool_argument(tool_name, key, task, node)
                if inferred is _MISSING:
                    if key in required:
                        raise RuntimeError(
                            f"工具 {tool_name} 的必填参数 {key} 无法解析：{exc}。"
                            "请检查该参数引用的前置节点输出，或让计划直接使用 constraints/runtime 占位符。"
                        ) from exc
                    notes.append(f"忽略无法解析的可选参数 {key}")
                    continue
                resolved[key] = inferred
                notes.append(f"参数 {key} 的占位符不可用，已从已批准任务上下文自动推断")

        for key in sorted(required - set(resolved)):
            inferred = self._infer_tool_argument(tool_name, key, task, node)
            if inferred is _MISSING:
                raise RuntimeError(f"工具 {tool_name} 缺少必填参数 {key}，且无法从任务上下文自动推断")
            resolved[key] = inferred
            notes.append(f"已从已批准任务参数解析必填字段 {key}")

        # Resolve optional fields only from approved task context or dependency
        # outputs; never from collection defaults.
        for key in properties:
            if key in resolved:
                continue
            inferred = self._infer_tool_argument(tool_name, key, task, node)
            if inferred is not _MISSING:
                resolved[key] = inferred

        # A model may create a phase-specific autonomous sub-plan.  Resolve it,
        # then clamp it to the operator-approved envelope before any real device
        # action.  This keeps flexible planning without allowing a later node to
        # silently widen frequency, duration, gain or repetition limits.
        if tool_name in {"generate_usrp_task_code", "execute_usrp_task_code", "run_autonomous_usrp_task"}:
            supplied_plan = resolved.get("task_plan")
            if not isinstance(supplied_plan, dict):
                supplied_plan = {}
            resolved["task_plan"] = self._merge_safe_autonomous_subplan(task, supplied_plan)

        self._assert_no_unresolved_placeholders(resolved, tool_name)
        return resolved, notes

    def _infer_tool_argument(
        self,
        tool_name: str,
        key: str,
        task: dict[str, Any],
        node: dict[str, Any],
    ) -> Any:
        constraints = dict(task.get("constraints") or {})
        if key in constraints:
            return _deepcopy_json(constraints[key])
        aliases = {
            "freq": "freq_start_mhz",
            "slice_duration": "dwell_time_sec",
        }
        if key in aliases and aliases[key] in constraints:
            return _deepcopy_json(constraints[aliases[key]])
        if key == "task_description":
            return self._tool_task_description(task)
        if key == "task_plan":
            return self._autonomous_tool_plan(task)
        if key == "code":
            code = str(task.get("generated_code") or "")
            return code if code else _MISSING
        if key == "query":
            return str(task.get("instruction") or "")
        if key == "top_k":
            return 6
        if key == "dev_id":
            value = self._find_dependency_value(task, node, {"dev_id", "device_id"})
            if value is not _MISSING:
                return value
            configured = os.getenv("DEEPEM_USRP_DEVICE_ID")
            return configured.strip() if configured and configured.strip() else _MISSING
        if key == "task_id":
            # Only accept a real device/task identifier emitted by a dependency.
            # The capture-agent task id is a different namespace and must never
            # be sent to query_usrp_task by accident.
            return self._find_dependency_value(task, node, {"task_id", "usrp_task_id"})
        if key == "output_dir":
            return task.get("output_dir") or _MISSING
        return _MISSING

    def _find_dependency_value(
        self,
        task: dict[str, Any],
        node: dict[str, Any],
        keys: set[str],
    ) -> Any:
        by_id = {item.get("id"): item for item in task.get("nodes", [])}
        queue_ids = list(reversed(node.get("dependencies") or []))
        seen_nodes: set[str] = set()
        while queue_ids:
            dep_id = queue_ids.pop(0)
            if dep_id == "approval" or dep_id in seen_nodes:
                continue
            seen_nodes.add(dep_id)
            dep = by_id.get(dep_id) or {}
            found = self._find_nested_key(dep.get("outputs") or {}, keys)
            if found is not _MISSING:
                return found
            queue_ids.extend(reversed(dep.get("dependencies") or []))
        return _MISSING

    @classmethod
    def _find_nested_key(cls, value: Any, keys: set[str]) -> Any:
        if isinstance(value, dict):
            for key in keys:
                candidate = value.get(key, _MISSING)
                if candidate is not _MISSING and candidate not in (None, ""):
                    return candidate
            preferred = ["selected_device", "device", "execution_result", "result", "data", "output", "outputs"]
            for key in preferred:
                if key in value:
                    found = cls._find_nested_key(value[key], keys)
                    if found is not _MISSING:
                        return found
            for child in value.values():
                found = cls._find_nested_key(child, keys)
                if found is not _MISSING:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = cls._find_nested_key(child, keys)
                if found is not _MISSING:
                    return found
        return _MISSING

    def _merge_safe_autonomous_subplan(self, task: dict[str, Any], supplied: dict[str, Any]) -> dict[str, Any]:
        """Allow task-specific collection phases without expanding the approved envelope."""
        base = self._autonomous_tool_plan(task)
        merged = {**base, **_deepcopy_json(supplied)}
        numeric_bounds = {
            "freq_start_mhz": (float(base["freq_start_mhz"]), None, "min"),
            "freq_stop_mhz": (None, float(base["freq_stop_mhz"]), "max"),
            "freq_step_mhz": (float(base["freq_step_mhz"]), None, "min"),
            "dwell_time_sec": (0.0, float(base["dwell_time_sec"]), "range"),
            "repeat_count": (1.0, float(base["repeat_count"]), "range"),
            "sample_rate": (0.0, float(base["sample_rate"]), "range"),
            "bandwidth": (0.0, float(base["bandwidth"]), "range"),
            "gain": (0.0, float(base["gain"]), "range"),
            "expected_fft_frames_per_capture": (1.0, float(base["expected_fft_frames_per_capture"]), "range"),
        }
        for key, (lower, upper, _) in numeric_bounds.items():
            try:
                value = float(merged[key])
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError(f"子计划参数 {key} 无效") from exc
            if lower is not None and value < lower:
                raise RuntimeError(f"子计划参数 {key}={value} 超出已批准下限 {lower}")
            if upper is not None and value > upper:
                raise RuntimeError(f"子计划参数 {key}={value} 超出已批准上限 {upper}")
        if float(merged["freq_stop_mhz"]) < float(merged["freq_start_mhz"]):
            raise RuntimeError("子计划终止频率不能低于起始频率")
        if float(merged["bandwidth"]) > float(merged["sample_rate"]):
            raise RuntimeError("子计划带宽不能大于采样率")
        estimated_count = int(math.floor((float(merged["freq_stop_mhz"]) - float(merged["freq_start_mhz"])) / float(merged["freq_step_mhz"]))) + 1
        if estimated_count > int(task.get("validation", {}).get("frequency_count") or 50000):
            raise RuntimeError("子计划频点数量超过已批准计划")
        merged["repeat_count"] = int(float(merged["repeat_count"]))
        merged["expected_fft_frames_per_capture"] = int(float(merged["expected_fft_frames_per_capture"]))
        for key in ("capture_agent_task_id", "output_dir", "strict_llm", "plan_version", "plan_fingerprint", "llm_plan", "task_type", "data_source", "antenna"):
            merged[key] = base.get(key)
        return merged

    _PLAN_PLACEHOLDER_RE = re.compile(r"\$\{([^{}]+)\}")

    def _resolve_plan_value(self, value: Any, task: dict[str, Any]) -> Any:
        if isinstance(value, dict):
            return {key: self._resolve_plan_value(item, task) for key, item in value.items()}
        if isinstance(value, list):
            return [self._resolve_plan_value(item, task) for item in value]
        if not isinstance(value, str):
            return value
        full = self._PLAN_PLACEHOLDER_RE.fullmatch(value.strip())
        if full:
            return _deepcopy_json(self._lookup_plan_reference(full.group(1), task))

        def interpolate(match: re.Match[str]) -> str:
            resolved = self._lookup_plan_reference(match.group(1), task)
            if resolved is None:
                return ""
            if isinstance(resolved, (dict, list)):
                return json.dumps(resolved, ensure_ascii=False, separators=(",", ":"), default=str)
            return str(resolved)

        result = self._PLAN_PLACEHOLDER_RE.sub(interpolate, value)
        if "${" in result:
            raise RuntimeError(f"参数中仍包含未闭合占位符：{result}")
        return result

    def _lookup_plan_reference(self, expression: str, task: dict[str, Any]) -> Any:
        normalized = self.planner._normalize_placeholder_expression(expression)
        value_expr, separator, default_expr = normalized.partition(":-")
        try:
            tokens = self._reference_tokens(value_expr)
            if not tokens:
                raise KeyError("empty")
            if tokens[0] == "runtime" and len(tokens) >= 2 and tokens[1] == "autonomous_task_plan":
                current: Any = self._autonomous_tool_plan(task)
                tokens = tokens[2:]
            else:
                roots: dict[str, Any] = {
                    "task": task,
                    "constraints": task.get("constraints", {}),
                    "generated_code": task.get("generated_code", ""),
                    "intent": task.get("intent_analysis", {}),
                    "nodes": {node["id"]: node for node in task.get("nodes", [])},
                    # Keep this root lazy: building the autonomous plan requires
                    # a fully initialized capture task and must not break unrelated
                    # placeholders such as nodes.* or constraints.*.
                    "runtime": {"output_dir": task.get("output_dir")},
                }
                if tokens[0] not in roots:
                    raise KeyError(tokens[0])
                current = roots[tokens[0]]
                tokens = tokens[1:]
            for token in tokens:
                current = self._traverse_reference(current, token)
            return current
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            if separator:
                return self._parse_placeholder_default(default_expr)
            available = self._reference_debug_keys(value_expr, task)
            suffix = f"；当前可用字段：{', '.join(available[:12])}" if available else ""
            raise RuntimeError(f"无法解析计划占位符：${{{expression}}}{suffix}") from exc

    @staticmethod
    def _reference_tokens(expression: str) -> list[str]:
        expr = str(expression or "").strip()
        tokens: list[str] = []
        buffer = ""
        index = 0
        while index < len(expr):
            char = expr[index]
            if char == ".":
                if buffer:
                    tokens.append(buffer)
                    buffer = ""
                index += 1
                continue
            if char == "[":
                if buffer:
                    tokens.append(buffer)
                    buffer = ""
                end = expr.find("]", index + 1)
                if end < 0:
                    raise ValueError("unclosed bracket")
                raw = expr[index + 1 : end].strip()
                if (raw.startswith("'") and raw.endswith("'")) or (raw.startswith('"') and raw.endswith('"')):
                    raw = raw[1:-1]
                if not raw:
                    raise ValueError("empty bracket")
                tokens.append(raw)
                index = end + 1
                continue
            buffer += char
            index += 1
        if buffer:
            tokens.append(buffer)
        return [token for token in tokens if token]

    @classmethod
    def _traverse_reference(cls, current: Any, token: str) -> Any:
        if isinstance(current, (list, tuple)):
            if re.fullmatch(r"-?\d+", token):
                return current[int(token)]
            # Allow selecting a field from the first list item that has it.
            for item in current:
                try:
                    return cls._traverse_reference(item, token)
                except (KeyError, IndexError, TypeError, ValueError):
                    continue
            raise KeyError(token)
        if not isinstance(current, dict):
            raise TypeError(token)
        aliases = {
            "output": "outputs",
            "data": "result",
        }
        candidates = [token]
        if token in aliases:
            candidates.append(aliases[token])
        for candidate in candidates:
            if candidate in current:
                return current[candidate]
        # Tool outputs are deliberately available both flat and under result. Be
        # tolerant of either shape so LLM plans do not fail on harmless wrappers.
        for wrapper in ("outputs", "output", "result", "data", "execution_result"):
            nested = current.get(wrapper, _MISSING)
            if nested is _MISSING or nested is current:
                continue
            try:
                return cls._traverse_reference(nested, token)
            except (KeyError, IndexError, TypeError, ValueError):
                continue
        raise KeyError(token)

    @staticmethod
    def _parse_placeholder_default(text: str) -> Any:
        value = str(text or "").strip()
        if value == "":
            return ""
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value

    def _reference_debug_keys(self, expression: str, task: dict[str, Any]) -> list[str]:
        try:
            tokens = self._reference_tokens(expression)
            if not tokens:
                return []
            roots = {
                "task": task,
                "constraints": task.get("constraints", {}),
                "generated_code": task.get("generated_code", ""),
                "intent": task.get("intent_analysis", {}),
                "nodes": {node["id"]: node for node in task.get("nodes", [])},
                "runtime": {"output_dir": task.get("output_dir")},
            }
            current: Any = roots.get(tokens[0], {})
            for token in tokens[1:-1]:
                current = self._traverse_reference(current, token)
            if isinstance(current, dict):
                return [str(key) for key in current.keys()]
            if isinstance(current, list):
                return [str(index) for index in range(min(len(current), 12))]
        except Exception:
            return []
        return []

    @classmethod
    def _assert_no_unresolved_placeholders(cls, value: Any, tool_name: str) -> None:
        if isinstance(value, dict):
            for child in value.values():
                cls._assert_no_unresolved_placeholders(child, tool_name)
        elif isinstance(value, list):
            for child in value:
                cls._assert_no_unresolved_placeholders(child, tool_name)
        elif isinstance(value, str) and ("${" in value or cls._PLAN_PLACEHOLDER_RE.search(value)):
            raise RuntimeError(f"工具 {tool_name} 参数仍包含未解析占位符：{value}")

    @staticmethod
    def _discover_output_files(value: Any) -> list[Path]:
        found: list[Path] = []
        def walk(item: Any) -> None:
            if isinstance(item, dict):
                for child in item.values():
                    walk(child)
            elif isinstance(item, list):
                for child in item:
                    walk(child)
            elif isinstance(item, str):
                candidate = Path(item)
                if candidate.is_absolute() and candidate.exists() and candidate.is_file():
                    resolved = candidate.resolve()
                    if resolved not in found:
                        found.append(resolved)
        walk(value)
        return found

    def _persist_task_bundle_locked(self, task: dict[str, Any]) -> None:
        output_dir = Path(str(task.get("output_dir") or (AUTONOMOUS_OUTPUT_ROOT / task["id"]))).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        json_path = output_dir / f"{task['id']}_report.json"
        md_path = output_dir / f"{task['id']}_report.md"
        md_path.write_text(self._render_markdown_report(task), encoding="utf-8")
        # 先落盘再登记，确保审计产物的 size_bytes 不是 0。
        json_path.write_text(json.dumps(task, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        self._add_artifact_locked(task, json_path, "audit_json")
        self._add_artifact_locked(task, md_path, "audit_markdown")
        code = str(task.get("generated_code") or "")
        if code:
            code_path = output_dir / "generated_task.py"
            code_path.write_text(code, encoding="utf-8")
            self._add_artifact_locked(task, code_path, "generated_code")
        # 再写一次，把刚登记的产物清单纳入最终审计快照，并刷新实际文件大小。
        json_path.write_text(json.dumps(task, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        self._add_artifact_locked(task, json_path, "audit_json")

    # ---------- node implementations ----------
    def _analyze_templates(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            task = self._load_task_locked(task_id)
            template_ids = list(task.get("template_ids") or [])
        if not template_ids:
            return {"template_count": 0, "message": "本任务未上传模板，将仅依据自然语言指令生成计划。", "combined_markdown": ""}
        items = []
        combined = []
        for template_id in template_ids:
            template = self.get_template(template_id)
            items.append(
                {
                    "id": template_id,
                    "file_name": template["file_name"],
                    "headings": template.get("headings", []),
                    "table_count": len(template.get("tables", [])),
                    "character_count": len(template.get("markdown", "")),
                }
            )
            combined.append(f"# 模板：{template['file_name']}\n{template.get('markdown', '')}")
        return {"template_count": len(items), "items": items, "combined_markdown": "\n\n".join(combined)[:50000]}

    def _analyze_instruction(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            task = self._load_task_locked(task_id)
            template_output = dict(self._node(task, "template_analysis").get("outputs") or {})
            template_markdown = str(template_output.get("combined_markdown") or "")
            previous_analysis = dict(task.get("intent_analysis") or {})

        user_explicit = CapturePlanBuilder.extract_constraints(task["instruction"], "")
        template_explicit = CapturePlanBuilder.extract_constraints("", template_markdown)
        parameter_context = {
            "policy": (
                "禁止任何默认采集参数。用户明确值优先于模板明确值；模板明确值优先于意图设计。"
                "仅对用户和模板均未指定的字段，根据本次任务意图设计合适参数。"
            ),
            "required_fields": list(CapturePlanBuilder.REQUIRED_CONSTRAINT_KEYS),
            "user_explicit_constraints": user_explicit,
            "template_explicit_constraints": template_explicit,
            "template_uploaded": bool(template_markdown.strip()),
        }
        result = self.planner.analyze_intent(
            instruction=task["instruction"],
            template_markdown=template_markdown,
            parameter_context=parameter_context,
            previous_analysis=previous_analysis,
            stream_handler=self._planner_stream_handler(task_id, "instruction_analysis"),
            cancel_checker=lambda: self._should_stop(task_id),
        )
        proposal = dict(result.get("proposal") or {})
        model_constraints = dict(proposal.get("constraints") or {})
        model_source_trace = dict(proposal.get("source_trace") or {})
        constraints: dict[str, Any] = {}
        sources: dict[str, str] = {}
        for key in CapturePlanBuilder.REQUIRED_CONSTRAINT_KEYS:
            if key in user_explicit:
                constraints[key] = user_explicit[key]
                sources[key] = "user_instruction"
            elif key in template_explicit:
                constraints[key] = template_explicit[key]
                sources[key] = "template"
            elif model_constraints.get(key) is not None:
                constraints[key] = model_constraints[key]
                trace = str(model_source_trace.get(key) or "").lower()
                if "user" in trace or "用户" in trace:
                    sources[key] = "user_instruction"
                elif "template" in trace or "模板" in trace:
                    sources[key] = "template"
                else:
                    sources[key] = "llm_intent_design"
            else:
                raise ValueError(
                    f"意图分析器未设计必需采集参数 {key}；系统禁止使用默认值或固定预设补齐"
                )

        constraints = CapturePlanBuilder.normalize_constraints(constraints)
        validation = CapturePlanBuilder.validate_constraints(constraints)
        proposal["constraints"] = dict(constraints)
        proposal["source_trace"] = dict(sources)
        with self._lock:
            current = self._load_task_locked(task_id)
            current["constraints"] = constraints
            current["constraint_sources"] = sources
            current["validation"] = validation
            current["intent_analysis"] = proposal
            current["parameter_resolution_policy"] = parameter_context["policy"]
            current["planner"] = result.get("model") or self.planner.runtime_info()
            self._save_task_locked(current)
        return {
            "intent_analysis": proposal,
            "constraints": constraints,
            "constraint_sources": sources,
            "validation": validation,
            "parameter_resolution_policy": parameter_context["policy"],
            "model": result.get("model"),
            "raw_model_output": result.get("raw_model_output"),
            "sources": ["用户明确参数", "DOCX 模板明确参数", "真实 LLM 按任务意图设计的其余参数"],
        }

    def _generate_plan_summary(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            task = self._load_task_locked(task_id)
            instruction = task["instruction"]
            intent_analysis = dict(task.get("intent_analysis") or {})
            constraints = dict(task.get("constraints") or {})
            validation = dict(task.get("validation") or {})
            plan_version = int(task.get("plan_version") or 1)
        prepare_result = self._prepare_spectrum_collection_plan(task_id, constraints)
        compiled_nodes = self._fixed_collection_nodes(prepare_result)
        proposal = {
            "objective": f"按标准化频谱采集工具完成：{instruction}",
            "strategy_summary": "平台固定调用 B 方案采集 tool：批准后执行采集、检查输出 .npz 文件、生成任务执行摘要。",
            "completion_contract": [
                "仅在用户批准后调用 execute_spectrum_collection",
                "输出文件检查只验证存在、扩展名 .npz、大小大于 0",
                "最后输出采集任务执行摘要和审计报告",
            ],
            "fixed_tool_flow": True,
            "prepare_result": prepare_result,
            "plan_steps_text": self._fixed_flow_text(compiled_nodes),
        }
        fingerprint = self.planner.plan_fingerprint(
            compiled_nodes,
            constraints=constraints,
            plan_version=plan_version,
            objective=proposal["objective"],
            completion_contract=proposal["completion_contract"],
        )
        with self._lock:
            current = self._load_task_locked(task_id)
            bootstrap_ids = {"template_analysis", "instruction_analysis", "plan_generation", "approval"}
            bootstrap_nodes = [node for node in current.get("nodes", []) if node.get("id") in bootstrap_ids]
            bootstrap_by_id = {node["id"]: node for node in bootstrap_nodes}
            ordered_bootstrap = [bootstrap_by_id[node_id] for node_id in ("template_analysis", "instruction_analysis", "plan_generation", "approval")]
            current["nodes"] = ordered_bootstrap + compiled_nodes
            current["plan_candidate"] = proposal
            current["plan_fingerprint"] = fingerprint
            current["approved_plan_fingerprint"] = None
            current["planner"] = self.planner.runtime_info()
            history_entry = {
                "plan_version": plan_version,
                "created_at": utc_now_iso(),
                "model": current["planner"],
                "proposal": proposal,
                "compiled_nodes": [
                    {
                        "id": node.get("id"),
                        "title": node.get("title"),
                        "kind": node.get("kind"),
                        "executor": node.get("executor"),
                        "dependencies": node.get("dependencies", []),
                        "tool_name": node.get("tool_name"),
                        "tool_arguments": node.get("tool_arguments", {}),
                        "allowed_tools": node.get("allowed_tools", []),
                        "success_criteria": node.get("success_criteria", []),
                        "planner_origin": node.get("planner_origin"),
                    }
                    for node in compiled_nodes
                ],
                "plan_fingerprint": fingerprint,
                "raw_model_output": "",
            }
            current.setdefault("plan_history", []).append(history_entry)
            self._append_event_locked(
                current,
                "fixed_tool_plan_compiled",
                f"已基于 B 方案工具生成 {len(compiled_nodes)} 个固定执行节点，等待用户确认参数并批准执行。",
                node_id="plan_generation",
                data={"plan_version": plan_version, "plan_fingerprint": fingerprint},
            )
            self._save_task_locked(current)
        return {
            "objective": proposal.get("objective") or instruction,
            "strategy_summary": proposal.get("strategy_summary"),
            "plan_version": plan_version,
            "locked_after_approval": True,
            "plan_fingerprint": fingerprint,
            "model": self.planner.runtime_info(),
            "llm_candidate": proposal,
            "compiled_nodes": history_entry["compiled_nodes"],
            "validator_summary": {
                "candidate_node_count": len(compiled_nodes),
                "executable_node_count": len(compiled_nodes),
                "inserted_nodes": [],
                "dag_validated": True,
                "tool_references_validated": True,
                "approval_gate_bound_to_roots": True,
                "workflow_template_used": True,
                "workflow_template": "B方案 prepare_spectrum_collection + execute_spectrum_collection",
            },
            "completion_contract": proposal.get("completion_contract") or [],
            "prepare_result": prepare_result,
            "plan_steps_text": proposal["plan_steps_text"],
            "raw_model_output": "",
        }

    @staticmethod
    def _local_prepare_spectrum_collection(args: dict[str, Any]) -> dict[str, Any]:
        required = {
            "room_name",
            "mode",
            "freq_start_mhz",
            "freq_stop_mhz",
            "freq_step_mhz",
            "dwell_ms",
            "repeat_count",
            "aggregation",
            "sample_rate",
            "bandwidth",
            "gain",
            "antenna",
        }
        missing = sorted(
            key for key in required
            if key not in args or args.get(key) is None or args.get(key) == ""
        )
        if missing:
            raise ValueError("本地采集计划缺少显式参数，禁止默认补齐：" + "、".join(missing))
        freq_start = float(args["freq_start_mhz"])
        freq_stop = float(args["freq_stop_mhz"])
        freq_step = float(args["freq_step_mhz"])
        repeat_count = int(args["repeat_count"])
        dwell_ms = float(args["dwell_ms"])
        freq_count = max(1, int(math.floor((freq_stop - freq_start) / freq_step)) + 1)
        return {
            "plan_id": new_id("scplan"),
            "room_name": str(args["room_name"]),
            "mode": str(args["mode"]),
            "freq_start_mhz": freq_start,
            "freq_stop_mhz": freq_stop,
            "freq_step_mhz": freq_step,
            "dwell_ms": dwell_ms,
            "repeat_count": repeat_count,
            "aggregation": str(args["aggregation"]),
            "sample_rate": float(args["sample_rate"]),
            "bandwidth": float(args["bandwidth"]),
            "gain": float(args["gain"]),
            "antenna": str(args["antenna"]),
            "freq_count": freq_count,
            "estimated_total_sec": round(freq_count * repeat_count * dwell_ms / 1000.0, 3),
            "device_id": args.get("device_id"),
            "device_status": {
                "status": "not_queried",
                "message": "当前运行环境没有完整平台上下文，仅校验本任务已解析参数。",
            },
            "dry_run": bool(args.get("dry_run", False)),
            "confirmation_prompt": "请确认本任务按意图解析的采集参数无误后执行。",
        }

    def _prepare_spectrum_collection_plan(self, task_id: str, constraints: dict[str, Any]) -> dict[str, Any]:
        node_id = "plan_generation"
        constraints = CapturePlanBuilder.normalize_constraints(constraints)
        args = {
            "room_name": constraints["room_name"],
            "mode": constraints["mode"],
            "freq_start_mhz": constraints["freq_start_mhz"],
            "freq_stop_mhz": constraints["freq_stop_mhz"],
            "freq_step_mhz": constraints["freq_step_mhz"],
            "dwell_ms": constraints["dwell_time_sec"] * 1000.0,
            "repeat_count": constraints["repeat_count"],
            "aggregation": constraints["aggregation"],
            "sample_rate": constraints["sample_rate"],
            "bandwidth": constraints["bandwidth"],
            "gain": constraints["gain"],
            "antenna": constraints["antenna"],
            "dry_run": False,
        }
        with self._lock:
            task = self._load_task_locked(task_id)
            node = self._node(task, node_id)
            node["logs"].append(self._log_entry("调用 prepare_spectrum_collection 生成待确认采集参数。", "info", args))
            self._append_event_locked(
                task,
                "tool_progress",
                "调用 B 方案 prepare_spectrum_collection 生成采集参数确认单。",
                node_id=node_id,
                data={"tool": "prepare_spectrum_collection", "stage": "prepare", "progress": 35},
            )
            self._save_task_locked(task)

        runtime = getattr(getattr(self.platform, "app", None), "runtime", None)
        platform_task = getattr(self.platform, "task", None)
        if runtime is None or platform_task is None or not hasattr(runtime, "tool_registry"):
            data = self._local_prepare_spectrum_collection(args)
            with self._lock:
                task = self._load_task_locked(task_id)
                node = self._node(task, node_id)
                node["logs"].append(self._log_entry("当前环境缺少完整平台上下文，已使用本地参数确认结果。", "warning", data))
                self._append_event_locked(
                    task,
                    "llm_reasoning_delta",
                    "已完成本地参数预检，等待用户在弹窗中批准执行。\n",
                    node_id=node_id,
                    data={"delta": "已完成本地参数预检，等待用户在弹窗中批准执行。\n", "progress": 70},
                )
                self._save_task_locked(task)
            return data

        run = Run(
            id=new_id("capture_run"),
            task_id=platform_task.id,
            trigger_kind=RunTriggerKind.CHAT,
            trigger_event_id=None,
            trigger_message_id=None,
            agent_profile="capture_agent",
            status=RunStatus.RUNNING,
            step_budget=1,
            step_count=0,
            started_at=utc_now(),
            conversation_id=None,
        )
        message = ChatMessage(
            id=new_id("capture_message"),
            conversation_id="capture-agent",
            task_id=platform_task.id,
            role=ChatRole.OPERATOR,
            content=self.get_task_internal(task_id)["instruction"],
            run_id=run.id,
            created_at=utc_now(),
        )
        context = ToolContext(
            task=platform_task,
            run=run,
            trigger_event=None,
            trigger_message=message,
            state_repo=runtime.state_repo,
            case_repo=runtime.case_repo,
            knowledge_base=runtime.knowledge_base,
            device_registry=runtime.device_registry,
            llm_client=runtime.llm_client,
            stream_handler=None,
            nl2sql_options=NL2SQLSessionConfig(),
            asset_manager=runtime.asset_manager,
            document_index=runtime.document_index,
            database_catalog=runtime.database_catalog,
            cancel_checker=lambda: self._cancel_events.setdefault(task_id, threading.Event()).is_set(),
        )
        result = runtime.tool_registry.execute("prepare_spectrum_collection", args, context)
        if result.result.status != "success":
            raise RuntimeError(result.result.error or "prepare_spectrum_collection 执行失败")
        data = dict(result.result.data or {})
        with self._lock:
            task = self._load_task_locked(task_id)
            node = self._node(task, node_id)
            node["logs"].append(self._log_entry("prepare_spectrum_collection 已返回待确认参数。", "success", data))
            self._append_event_locked(
                task,
                "llm_reasoning_delta",
                "B 方案工具已完成参数预检，等待用户在弹窗中批准执行。\n",
                node_id=node_id,
                data={"delta": "B 方案工具已完成参数预检，等待用户在弹窗中批准执行。\n", "progress": 70},
            )
            self._save_task_locked(task)
        return data

    def _fixed_collection_nodes(self, prepare_result: dict[str, Any]) -> list[dict[str, Any]]:
        now = utc_now_iso()
        plan_id = str(prepare_result.get("plan_id") or "")
        specs = [
            {
                "id": "device_scan",
                "title": "开始：查询可用设备",
                "description": "执行开始前查询当前可用 USRP 设备，选择空闲设备并记录设备状态，作为后续采集的前置条件。",
                "executor": "device_scan",
                "kind": "system_check",
                "dependencies": ["approval"],
                "tool_name": "scan_usrp_devices",
                "tool_arguments": {},
                "allowed_tool": "scan_usrp_devices",
                "allowed_tools": ["scan_usrp_devices"],
            },
            {
                "id": "collection_execution",
                "title": "执行标准频谱采集",
                "description": "固定调用 B 方案 execute_spectrum_collection，按已确认 plan_id 执行全频段/多频点采集并生成 .npz。",
                "executor": "collection_execution",
                "kind": "tool_call",
                "dependencies": ["device_scan"],
                "tool_name": "execute_spectrum_collection",
                "tool_arguments": {"plan_id": plan_id, "operator_confirmed": True},
                "allowed_tool": "execute_spectrum_collection",
                "allowed_tools": ["execute_spectrum_collection"],
            },
            {
                "id": "output_file_check",
                "title": "检查输出文件",
                "description": "只检查输出文件是否存在、是否为 .npz、文件大小是否大于 0。",
                "executor": "output_file_check",
                "kind": "system_check",
                "dependencies": ["collection_execution"],
                "tool_name": None,
                "tool_arguments": {},
                "allowed_tool": None,
                "allowed_tools": [],
            },
            {
                "id": "execution_summary",
                "title": "总结采集任务摘要",
                "description": "汇总采集参数、执行结果、输出文件检查结果和任务产物。",
                "executor": "execution_summary",
                "kind": "system_summary",
                "dependencies": ["output_file_check"],
                "tool_name": None,
                "tool_arguments": {},
                "allowed_tool": None,
                "allowed_tools": [],
            },
        ]
        nodes = []
        for spec in specs:
            nodes.append(
                {
                    **spec,
                    "status": "pending",
                    "progress": 0,
                    "attempts": 0,
                    "started_at": None,
                    "ended_at": None,
                    "updated_at": now,
                    "summary": "等待执行",
                    "success_criteria": CapturePlanBuilder.success_criteria(spec["id"]),
                    "expected_evidence": [],
                    "tool_input_overrides": {},
                    "risk_level": "medium" if spec["id"] == "collection_execution" else "low",
                    "planner_origin": "fixed_b_tool_flow",
                    "inputs": {},
                    "outputs": {},
                    "logs": [],
                    "notes": "",
                }
            )
        return nodes

    @staticmethod
    def _fixed_flow_text(nodes: list[dict[str, Any]]) -> str:
        return " → ".join(str(node.get("title") or node.get("id")) for node in nodes)

    def _execute_knowledge_retrieval(self, task_id: str, node_id: str) -> tuple[dict[str, Any], str]:
        task = self.get_task_internal(task_id)
        node = self._node(task, node_id)
        overrides = dict(node.get("tool_input_overrides") or {})
        query = str(overrides.get("query") or f"{task['instruction']} WebSocket FFT configure start stop 参数限制")
        top_k = max(1, min(12, int(overrides.get("top_k") or 6)))
        data = self._execute_tool(task_id, node_id, "retrieve_usrp_api_knowledge", {"query": query, "top_k": top_k})
        return data, f"已检索 {len(data.get('items') or [])} 个 USRP API 知识片段。"

    def _execute_device_scan(self, task_id: str, node_id: str) -> tuple[dict[str, Any], str]:
        task = self.get_task_internal(task_id)
        try:
            payload = self._execute_tool(task_id, node_id, "scan_usrp_devices", {})
        except Exception as exc:
            payload = {"devices": [], "scan_error": str(exc)}
        devices = list(payload.get("devices") or [])
        selected = next((item for item in devices if str(item.get("status") or "").upper() == "IDLE"), None)
        if selected is None:
            reason = payload.get("scan_error") or "未发现 IDLE 的可用 USRP 设备"
            return {**payload, "selected_device": None, "query_status": "no_idle_device"}, f"可用设备查询完成：{reason}；后续采集工具将继续执行自身设备检查。"
        return {**payload, "selected_device": selected, "query_status": "idle_device_selected"}, f"已选择空闲设备 {selected.get('dev_id')}。"

    def _execute_device_status(self, task_id: str, node_id: str) -> tuple[dict[str, Any], str]:
        task = self.get_task_internal(task_id)
        selected = dict((self._node_for_executor(task, "device_scan").get("outputs") or {}).get("selected_device") or {})
        payload = self._execute_tool(task_id, node_id, "list_usrp_devices", {})
        devices = list(payload.get("devices") or [])
        matched = next((item for item in devices if item.get("dev_id") == selected.get("dev_id")), None)
        if matched is None:
            raise RuntimeError("设备状态复核时未找到已选择设备")
        if str(matched.get("status") or "").upper() != "IDLE":
            raise RuntimeError(f"设备 {matched.get('dev_id')} 当前状态不是 IDLE")
        return {**payload, "selected_device": matched}, f"设备 {matched.get('dev_id')} 状态复核通过。"

    def _execute_parameter_validation(self, task_id: str, node_id: str) -> tuple[dict[str, Any], str]:
        task = self.get_task_internal(task_id)
        constraints = dict(task.get("constraints") or {})
        validation = CapturePlanBuilder.validate_constraints(constraints)
        device_output = self._node_for_executor(task, "device_scan").get("outputs") or {}
        selected = dict(device_output.get("selected_device") or {})
        config = dict(selected.get("dev_config") or {})
        freq_range = dict(config.get("freq_range") or {})
        min_freq = freq_range.get("min")
        max_freq = freq_range.get("max")
        if min_freq is not None and constraints["freq_start_mhz"] < _device_mhz_value(min_freq):
            raise RuntimeError(f"起始频率低于设备下限 {_human_frequency(_device_mhz_value(min_freq))}")
        if max_freq is not None and constraints["freq_stop_mhz"] > _device_mhz_value(max_freq):
            raise RuntimeError(f"终止频率高于设备上限 {_human_frequency(_device_mhz_value(max_freq))}")
        return {"status": "passed", "constraints": constraints, "validation": validation, "device_id": selected.get("dev_id")}, f"参数安全校验通过，共 {validation['frequency_count']} 个频点。"

    def _execute_device_configuration(self, task_id: str, node_id: str) -> tuple[dict[str, Any], str]:
        task = self.get_task_internal(task_id)
        constraints = dict(task["constraints"])
        selected = dict((self._node_for_executor(task, "device_scan").get("outputs") or {}).get("selected_device") or {})
        params = {
            "dev_id": selected.get("dev_id"),
            "freq": constraints["freq_start_mhz"],
            "sample_rate": constraints["sample_rate"],
            "bandwidth": constraints["bandwidth"],
            "gain": constraints["gain"],
            "slice_duration": constraints["dwell_time_sec"],
            "duration": constraints["dwell_time_sec"],
            "antenna": constraints["antenna"],
        }
        result = self._execute_tool(task_id, node_id, "configure_usrp_capture", params)
        return {"status": "configured", "params": params, "result": result}, "设备采集参数配置成功。"

    def _execute_code_generation(self, task_id: str, node_id: str) -> tuple[dict[str, Any], str]:
        task = self.get_task_internal(task_id)
        tool_plan = self._autonomous_tool_plan(task)
        data = self._execute_tool(
            task_id,
            node_id,
            "generate_usrp_task_code",
            {"task_description": self._tool_task_description(task), "task_plan": tool_plan},
        )
        code = str(data.get("code") or "")
        if not code:
            raise RuntimeError("采集代码生成器未返回代码")
        with self._lock:
            current = self._load_task_locked(task_id)
            current["generated_code"] = code
            self._save_task_locked(current)
        validation = dict(data.get("validation") or {})
        return data, f"真实 LLM 已生成受控 run_task(ctx)，安全检查状态：{validation.get('status', 'unknown')}。"

    def _execute_collection_execution(self, task_id: str, node_id: str) -> tuple[dict[str, Any], str]:
        task = self.get_task_internal(task_id)
        code = str(task.get("generated_code") or "")
        if not code:
            raise RuntimeError("缺少已验证的采集代码")
        data = self._execute_tool(
            task_id,
            node_id,
            "execute_usrp_task_code",
            {
                "code": code,
                "task_description": self._tool_task_description(task),
                "task_plan": self._autonomous_tool_plan(task),
            },
        )
        execution = dict(data.get("execution_result") or {})
        if execution.get("status") != "completed":
            raise RuntimeError(f"采集执行未完成：{execution.get('status') or 'unknown'}")
        with self._lock:
            current = self._load_task_locked(task_id)
            current["execution_result"] = execution
            output_file = execution.get("output_file")
            if output_file:
                self._add_artifact_locked(current, Path(str(output_file)), "collection_result")
            self._save_task_locked(current)
        return data, f"采集执行完成：{execution.get('freq_count', '-')} 个频点，重复 {execution.get('repeat_count', '-')} 次。"

    def _execute_result_verification(self, task_id: str, node_id: str) -> tuple[dict[str, Any], str]:
        task = self.get_task_internal(task_id)
        result = dict(task.get("execution_result") or {})
        expected = dict(task.get("validation") or {})
        errors = []
        if result.get("status") != "completed":
            errors.append("执行状态不是 completed")
        if int(result.get("freq_count") or -1) != int(expected.get("frequency_count") or -2):
            errors.append(f"频点数不匹配：实际 {result.get('freq_count')} / 计划 {expected.get('frequency_count')}")
        if int(result.get("repeat_count") or -1) != int(task["constraints"].get("repeat_count") or -2):
            errors.append("重复次数与计划不一致")
        output_file = str(result.get("output_file") or "")
        output_exists = bool(output_file and Path(output_file).exists())
        if not output_exists:
            errors.append("输出文件不存在")
        if errors:
            raise RuntimeError("；".join(errors))
        verification = {
            "status": "passed",
            "output_file": output_file,
            "output_exists": output_exists,
            "frequency_count": result.get("freq_count"),
            "repeat_count": result.get("repeat_count"),
            "data_source": result.get("data_source"),
            "approved_plan_fingerprint": task.get("approved_plan_fingerprint"),
        }
        return verification, "采集结果与批准的 LLM 计划一致，验收通过。"

    def _execute_artifact_save(self, task_id: str, node_id: str) -> tuple[dict[str, Any], str]:
        with self._lock:
            task = self._load_task_locked(task_id)
            report_dir = self.reports_dir / task_id
            report_dir.mkdir(parents=True, exist_ok=True)
            json_path = report_dir / f"{task_id}_report.json"
            md_path = report_dir / f"{task_id}_report.md"
            json_path.write_text(json.dumps(task, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            md_path.write_text(self._render_markdown_report(task), encoding="utf-8")
            json_artifact = self._add_artifact_locked(task, json_path, "audit_json")
            md_artifact = self._add_artifact_locked(task, md_path, "audit_markdown")
            self._save_task_locked(task)
        return {"report_json": json_artifact, "report_markdown": md_artifact}, "任务审计报告已保存。"

    def _execute_output_file_check(self, task_id: str, node_id: str) -> tuple[dict[str, Any], str]:
        task = self.get_task_internal(task_id)
        result = dict(task.get("execution_result") or {})
        output_file = str(result.get("output_file") or "")
        path = Path(output_file) if output_file else Path()
        checks = {
            "exists": bool(output_file and path.exists() and path.is_file()),
            "is_npz": bool(output_file and path.suffix.lower() == ".npz"),
            "size_gt_zero": bool(output_file and path.exists() and path.is_file() and path.stat().st_size > 0),
        }
        if not all(checks.values()):
            failed = "；".join(key for key, passed in checks.items() if not passed)
            raise RuntimeError(f"输出文件检查失败：{failed}")
        size_bytes = path.stat().st_size
        verification = {
            "status": "passed",
            "output_file": str(path.resolve()),
            "file_name": path.name,
            "size_bytes": size_bytes,
            "checks": checks,
        }
        with self._lock:
            current = self._load_task_locked(task_id)
            self._add_artifact_locked(current, path, "collection_result")
            self._save_task_locked(current)
        return verification, f"输出文件检查通过：存在、.npz、大小 {size_bytes} B。"

    def _execute_execution_summary(self, task_id: str, node_id: str) -> tuple[dict[str, Any], str]:
        with self._lock:
            task = self._load_task_locked(task_id)
            prepare_result = dict((task.get("plan_candidate") or {}).get("prepare_result") or {})
            execution_result = dict(task.get("execution_result") or {})
            output_check = dict(self._node(task, "output_file_check").get("outputs") or {})
            summary = {
                "task_id": task["id"],
                "plan_version": task.get("plan_version"),
                "room_name": prepare_result.get("room_name") or execution_result.get("room_name"),
                "plan_id": prepare_result.get("plan_id") or execution_result.get("plan_id"),
                "freq_count": execution_result.get("freq_count") or prepare_result.get("freq_count"),
                "success_count": execution_result.get("success_count"),
                "failed_count": execution_result.get("failed_count", 0),
                "repeat_count": execution_result.get("repeat_count") or prepare_result.get("repeat_count"),
                "aggregation": execution_result.get("aggregation") or prepare_result.get("aggregation"),
                "device_id": execution_result.get("device_id") or prepare_result.get("device_id"),
                "output_file": execution_result.get("output_file") or output_check.get("output_file"),
                "output_check": output_check.get("checks") or {},
                "status": execution_result.get("status") or "completed",
            }
            task["execution_summary"] = summary
            task["messages"].append(
                {
                    "id": f"msg_{uuid4().hex[:10]}",
                    "role": "assistant",
                    "content": (
                        f"采集任务执行摘要：会议室/区域 {summary.get('room_name') or '-'}，"
                        f"计划 {summary.get('plan_id') or '-'}，输出文件 {Path(str(summary.get('output_file') or '')).name or '-'}。"
                    ),
                    "created_at": utc_now_iso(),
                    "template_ids": [],
                }
            )
            self._save_task_locked(task)
        return summary, "采集任务执行摘要已生成。"

    # ---------- tool bridge ----------
    def _execute_tool(self, task_id: str, node_id: str, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            task = self._load_task_locked(task_id)
            self._assert_plan_integrity_locked(task)
            node = self._node(task, node_id)
            allowed_tools = list(node.get("allowed_tools") or ([] if not node.get("allowed_tool") else [node.get("allowed_tool")]))
            if tool_name not in allowed_tools:
                raise RuntimeError(f"计划约束拒绝工具调用：节点 {node_id} 仅允许 {allowed_tools or ['无']}，收到 {tool_name}")
            task = _deepcopy_json(task)
        runtime = getattr(getattr(self.platform, "app", None), "runtime", None)
        platform_task = getattr(self.platform, "task", None)
        if runtime is None or platform_task is None or not hasattr(runtime, "tool_registry"):
            raise RuntimeError(f"当前平台上下文不支持执行工具 {tool_name}")
        run = Run(
            id=new_id("capture_run"),
            task_id=platform_task.id,
            trigger_kind=RunTriggerKind.CHAT,
            trigger_event_id=None,
            trigger_message_id=None,
            agent_profile="capture_agent",
            status=RunStatus.RUNNING,
            step_budget=1,
            step_count=0,
            started_at=utc_now(),
            conversation_id=None,
        )
        message = ChatMessage(
            id=new_id("capture_message"),
            conversation_id="capture-agent",
            task_id=platform_task.id,
            role=ChatRole.OPERATOR,
            content=task["instruction"],
            run_id=run.id,
            created_at=utc_now(),
        )

        stream_state = {
            "reasoning_buffer": "",
            "content_buffer": "",
            "last_reasoning_flush": 0.0,
            "last_content_flush": 0.0,
        }

        def stream_handler(event_type: str, payload: dict[str, Any]) -> None:
            payload = dict(payload or {})
            nested = payload.get("data")
            data = dict(nested) if isinstance(nested, dict) else {}
            tool_event_name = str(payload.get("tool_name") or data.get("tool_name") or tool_name)
            stage = str(payload.get("stage") or data.get("stage") or event_type)
            delta = str(payload.get("delta") or data.get("delta") or "")
            message_text = str(payload.get("message") or data.get("message") or "")
            now = time.monotonic()

            # LLM 的 reasoning/content 流需要写入节点输出，并转发成前端能识别的
            # llm_* 事件。旧实现把它们当作普通工具日志，导致右侧面板一直为空。
            if event_type == "reasoning_delta":
                stream_state["reasoning_buffer"] += delta
                if len(stream_state["reasoning_buffer"]) < 80 and now - float(stream_state["last_reasoning_flush"]) < 0.12:
                    return
            elif event_type == "content_delta":
                stream_state["content_buffer"] += delta
                if len(stream_state["content_buffer"]) < 160 and now - float(stream_state["last_content_flush"]) < 0.18:
                    return

            with self._lock:
                current = self._load_task_locked(task_id)
                current_node = self._node(current, node_id)
                outputs = current_node.setdefault("outputs", {})
                event_name = "tool_progress"
                event_payload: dict[str, Any] = {"tool": tool_name, "tool_event": tool_event_name, "stage": stage}
                log_message = ""

                if event_type == "reasoning_start":
                    outputs.setdefault("llm_reasoning", "")
                    current_node["summary"] = "工具内智能体正在分析采集任务……"
                    current_node["progress"] = max(int(current_node.get("progress") or 0), 5)
                    event_name = "llm_reasoning_start"
                    message_text = message_text or "工具内模型开始输出实时推理。"
                elif event_type == "reasoning_delta":
                    chunk = stream_state["reasoning_buffer"]
                    stream_state["reasoning_buffer"] = ""
                    stream_state["last_reasoning_flush"] = now
                    outputs["llm_reasoning"] = (str(outputs.get("llm_reasoning") or "") + chunk)[-60000:]
                    current_node["summary"] = "工具内智能体正在生成采集函数……"
                    current_node["progress"] = max(int(current_node.get("progress") or 0), min(35, 7 + len(str(outputs["llm_reasoning"])) // 180))
                    event_name = "llm_reasoning_delta"
                    event_payload["delta"] = chunk
                    message_text = "工具内模型推理内容已更新。"
                elif event_type == "reasoning_done":
                    if stream_state["reasoning_buffer"]:
                        chunk = stream_state["reasoning_buffer"]
                        outputs["llm_reasoning"] = (str(outputs.get("llm_reasoning") or "") + chunk)[-60000:]
                        stream_state["reasoning_buffer"] = ""
                        event_payload["delta"] = chunk
                    event_name = "llm_reasoning_done"
                    message_text = message_text or "工具内模型推理阶段完成。"
                elif event_type == "content_start":
                    outputs.setdefault("llm_live_output", "")
                    event_name = "llm_content_start"
                    message_text = message_text or "工具内模型开始生成采集函数。"
                elif event_type == "content_delta":
                    chunk = stream_state["content_buffer"]
                    stream_state["content_buffer"] = ""
                    stream_state["last_content_flush"] = now
                    outputs["llm_live_output"] = (str(outputs.get("llm_live_output") or "") + chunk)[-80000:]
                    current_node["summary"] = "工具内智能体正在流式生成采集函数……"
                    current_node["progress"] = max(int(current_node.get("progress") or 0), min(45, 18 + len(str(outputs["llm_live_output"])) // 300))
                    event_name = "llm_content_delta"
                    event_payload["delta"] = chunk
                    message_text = "采集函数输出流已更新。"
                elif event_type == "content_done":
                    if stream_state["content_buffer"]:
                        chunk = stream_state["content_buffer"]
                        outputs["llm_live_output"] = (str(outputs.get("llm_live_output") or "") + chunk)[-80000:]
                        stream_state["content_buffer"] = ""
                        event_payload["delta"] = chunk
                    event_name = "llm_content_done"
                    message_text = message_text or "工具内模型代码生成完成。"
                else:
                    # SafeExecutionLogger 以及 run_autonomous_usrp_task 的阶段事件
                    # 作为可审计执行轨迹同步到右侧面板，避免模型不返回 reasoning
                    # 时出现空白区域。
                    if event_type == "tool_trace":
                        message_text = message_text or f"正在执行 {stage} 阶段。"
                    elif tool_event_name == "autonomous_usrp_progress":
                        message_text = message_text or "自主采集进度已更新。"
                    elif event_type == "tool_result" and tool_event_name == "generate_usrp_task_code":
                        message_text = "采集函数与静态校验结果已生成。"
                    elif event_type == "tool_result" and tool_event_name == "run_autonomous_usrp_task":
                        message_text = "端到端自主采集工具已返回最终结果。"
                    else:
                        message_text = message_text or f"工具事件：{event_type}"

                    trace_line = f"[{stage}] {message_text}\n"
                    outputs["llm_reasoning"] = (str(outputs.get("llm_reasoning") or "") + trace_line)[-60000:]
                    event_name = "llm_reasoning_delta"
                    event_payload["delta"] = trace_line
                    log_message = message_text

                # 将实时 FFT 连接信息透传给前端；仅保留轻量元数据，避免把 FFT 数组写入任务事件。
                detail_data = data.get("data") if isinstance(data.get("data"), dict) else {}
                if stage == "fft":
                    for key in ("ws_url", "task_id", "freq", "freq_mhz", "frame_count", "fft_size"):
                        if key in detail_data and detail_data.get(key) is not None:
                            event_payload[key] = detail_data.get(key)
                    legacy_freq = detail_data.get("freq_" + "hz")
                    if legacy_freq is not None and "freq_mhz" not in event_payload:
                        try:
                            event_payload["freq_mhz"] = float(legacy_freq) / 1e6
                        except Exception:
                            event_payload["freq_mhz"] = legacy_freq

                progress_data = data if data else payload
                progress = self._progress_from_tool_event(stage, progress_data)
                if progress is not None:
                    current_node["progress"] = max(int(current_node.get("progress") or 0), progress)
                event_payload["progress"] = current_node.get("progress", 0)
                current_node["updated_at"] = utc_now_iso()

                if log_message:
                    current_node["logs"].append(
                        self._log_entry(log_message, "info", {"stage": stage, "tool_event": tool_event_name})
                    )
                self._append_event_locked(current, event_name, message_text, node_id=node_id, data=event_payload)
                self._save_task_locked(current)

        context = ToolContext(
            task=self.platform.task,
            run=run,
            trigger_event=None,
            trigger_message=message,
            state_repo=runtime.state_repo,
            case_repo=runtime.case_repo,
            knowledge_base=runtime.knowledge_base,
            device_registry=runtime.device_registry,
            llm_client=runtime.llm_client,
            stream_handler=stream_handler,
            nl2sql_options=NL2SQLSessionConfig(),
            asset_manager=runtime.asset_manager,
            document_index=runtime.document_index,
            database_catalog=runtime.database_catalog,
            cancel_checker=lambda: self._cancel_events.setdefault(task_id, threading.Event()).is_set(),
        )
        result = runtime.tool_registry.execute(tool_name, args, context)
        if result.result.status != "success":
            raise RuntimeError(result.result.error or f"工具 {tool_name} 执行失败")
        return dict(result.result.data or {})

    @staticmethod
    def _progress_from_tool_event(stage: str, data: dict[str, Any]) -> int | None:
        explicit = data.get("progress")
        if explicit is None and isinstance(data.get("data"), dict):
            explicit = data["data"].get("progress")
        if explicit is not None:
            try:
                return max(1, min(95, int(float(explicit))))
            except Exception:
                pass
        stages = {
            "initializing": 3,
            "knowledge": 8,
            "code_generation": 16,
            "validation": 24,
            "validate": 26,
            "sandbox": 30,
            "execution": 38,
            "progress": 65,
            "scan": 42,
            "plan": 45,
            "configure": 50,
            "start": 55,
            "capture": 65,
            "fft": 78,
            "save": 90,
            "artifact_relocated": 92,
            "completed": 95,
            "error": 95,
        }
        return stages.get(stage)

    # ---------- helpers ----------
    def _tool_task_description(self, task: dict[str, Any]) -> str:
        constraints = task["constraints"]
        plan_steps = "；".join(
            f"{index + 1}.{node.get('title')}"
            for index, node in enumerate(self._execution_nodes_in_order(task))
        )
        return (
            f"{task['instruction']}\n"
            f"必须严格使用已批准的真实 LLM 计划 v{task['plan_version']}（指纹 {task.get('approved_plan_fingerprint') or task.get('plan_fingerprint')}）。"
            f"采集约束：{_human_frequency(constraints['freq_start_mhz'])} 至 {_human_frequency(constraints['freq_stop_mhz'])}，"
            f"步长 {_human_frequency(constraints['freq_step_mhz'])}，每个频点重复 {constraints['repeat_count']} 次，"
            f"驻留 {constraints['dwell_time_sec']} 秒。不得扩大频率范围、增加重复次数或修改数据源。\n"
            f"批准节点顺序：{plan_steps}"
        )

    def _autonomous_tool_plan(self, task: dict[str, Any]) -> dict[str, Any]:
        constraints = dict(task["constraints"])
        selected: dict[str, Any] = {}
        for candidate in task.get("nodes", []):
            outputs = candidate.get("outputs") or {}
            if not isinstance(outputs, dict):
                continue
            possible = outputs.get("selected_device")
            if not isinstance(possible, dict) and isinstance(outputs.get("result"), dict):
                possible = outputs["result"].get("selected_device")
            if isinstance(possible, dict) and possible.get("dev_id"):
                selected = dict(possible)
                break
        return {
            "task_type": "llm_planned_capture_agent_usrp_fft_stream",
            "data_source": "usrp_websocket_fft",
            "room_name": constraints["room_name"],
            "mode": constraints["mode"],
            "freq_start_mhz": constraints["freq_start_mhz"],
            "freq_stop_mhz": constraints["freq_stop_mhz"],
            "freq_step_mhz": constraints["freq_step_mhz"],
            "dwell_time_sec": constraints["dwell_time_sec"],
            "repeat_count": constraints["repeat_count"],
            "aggregation": constraints["aggregation"],
            "sample_rate": constraints["sample_rate"],
            "bandwidth": constraints["bandwidth"],
            "gain": constraints["gain"],
            "antenna": constraints["antenna"],
            "expected_fft_frames_per_capture": constraints["expected_fft_frames_per_capture"],
            "dev_id": selected.get("dev_id"),
            "capture_agent_task_id": task["id"],
            "output_dir": task.get("output_dir"),
            "strict_llm": True,
            "plan_version": task["plan_version"],
            "plan_fingerprint": task.get("approved_plan_fingerprint") or task.get("plan_fingerprint"),
            "llm_plan": [
                {
                    "id": node.get("id"),
                    "title": node.get("title"),
                    "kind": node.get("kind") or node.get("executor"),
                    "tool_name": node.get("tool_name") or node.get("allowed_tool"),
                    "dependencies": node.get("dependencies", []),
                    "success_criteria": node.get("success_criteria", []),
                }
                for node in self._execution_nodes_in_order(task)
            ],
        }

    def _node_inputs(self, task: dict[str, Any], node_id: str) -> dict[str, Any]:
        node = self._node(task, node_id)
        executor = str(node.get("executor") or node_id)
        constraints = dict(task.get("constraints") or {})
        dependency_outputs = {
            dep: (self._node(task, dep).get("outputs") or {})
            for dep in node.get("dependencies", [])
            if dep != "approval"
        }
        base = {
            "plan_version": task.get("plan_version"),
            "plan_fingerprint": task.get("approved_plan_fingerprint") or task.get("plan_fingerprint"),
            "planner": task.get("planner"),
            "kind": node.get("kind") or executor,
            "executor": executor,
            "tool_name": node.get("tool_name") or node.get("allowed_tool"),
            "tool_arguments": node.get("tool_arguments", {}),
            "dependencies": node.get("dependencies", []),
            "dependency_outputs": dependency_outputs,
            "output_dir": task.get("output_dir"),
        }
        if node.get("kind") == "tool_call":
            try:
                resolved = self._resolve_plan_value(dict(node.get("tool_arguments") or {}), task)
            except Exception as exc:
                resolved = {"resolution_error": str(exc), "raw": node.get("tool_arguments", {})}
            return {**base, "resolved_tool_arguments": resolved}
        if node.get("kind") in {"llm_analysis", "llm_verification", "decision"}:
            return {**base, "instruction": task.get("instruction"), "constraints": constraints}
        if node_id == "template_analysis":
            return {**base, "template_ids": task.get("template_ids", []), "templates": task.get("templates", [])}
        if node_id == "instruction_analysis":
            return {**base, "instruction": task.get("instruction"), "template_count": len(task.get("template_ids", [])), "requires_real_llm": True}
        if node_id == "plan_generation":
            return {
                **base,
                "instruction": task.get("instruction"),
                "intent_analysis": task.get("intent_analysis", {}),
                "constraints": constraints,
                "fixed_tool": "prepare_spectrum_collection",
            }
        if node_id == "approval":
            return {
                **base,
                "requires_explicit_approval": True,
                "prepare_result": (task.get("plan_candidate") or {}).get("prepare_result", {}),
                "plan_steps_text": (task.get("plan_candidate") or {}).get("plan_steps_text", ""),
            }
        if executor == "output_file_check":
            return {**base, "execution_result": task.get("execution_result", {}), "checks": ["exists", "is_npz", "size_gt_zero"]}
        if executor == "execution_summary":
            return {
                **base,
                "prepare_result": (task.get("plan_candidate") or {}).get("prepare_result", {}),
                "execution_result": task.get("execution_result", {}),
                "output_file_check": self._node(task, "output_file_check").get("outputs", {}),
            }
        if executor == "knowledge_retrieval":
            return {**base, "instruction": task.get("instruction"), "data_source": "usrp_websocket_fft"}
        if executor == "device_scan":
            return {**base, "device_concurrency_policy": "one_active_task_per_device"}
        selected_device = {}
        try:
            selected_device = dict((self._node_for_executor(task, "device_scan").get("outputs") or {}).get("selected_device") or {})
        except KeyError:
            pass
        if executor == "device_status":
            return {**base, "selected_device": selected_device}
        if executor == "parameter_validation":
            return {**base, "constraints": constraints, "selected_device": selected_device}
        if executor == "device_configuration":
            return {**base, "constraints": constraints, "selected_device": selected_device}
        if executor == "code_generation":
            return {**base, "task_description": self._tool_task_description(task), "task_plan": self._autonomous_tool_plan(task)}
        if executor == "collection_execution":
            return {**base, "task_plan": self._autonomous_tool_plan(task), "generated_code_length": len(str(task.get("generated_code") or ""))}
        if executor == "result_verification":
            return {**base, "expected": task.get("validation", {}), "execution_result": task.get("execution_result", {})}
        if executor == "artifact_save":
            return {**base, "task_id": task.get("id"), "existing_artifact_count": len(task.get("artifacts", []))}
        return base

    def _planner_stream_handler(self, task_id: str, node_id: str) -> Callable[[str, dict[str, Any]], None]:
        stream_state = {
            "content_buffer": "",
            "reasoning_buffer": "",
            "last_content_flush": 0.0,
            "last_reasoning_flush": 0.0,
        }

        def handler(event_type: str, payload: dict[str, Any]) -> None:
            now = time.monotonic()
            delta = str(payload.get("delta") or payload.get("text") or "")
            if event_type == "content_delta":
                stream_state["content_buffer"] += delta
                if len(stream_state["content_buffer"]) < 120 and now - float(stream_state["last_content_flush"]) < 0.18:
                    return
            elif event_type == "reasoning_delta":
                stream_state["reasoning_buffer"] += delta
                if len(stream_state["reasoning_buffer"]) < 80 and now - float(stream_state["last_reasoning_flush"]) < 0.12:
                    return

            with self._lock:
                try:
                    task = self._load_task_locked(task_id)
                    node = self._node(task, node_id)
                except KeyError:
                    return
                outputs = node.setdefault("outputs", {})
                event_data = {k: v for k, v in payload.items() if k not in {"delta", "text"}}

                if event_type == "content_delta":
                    chunk = stream_state["content_buffer"]
                    live = str(outputs.get("llm_live_output") or "") + chunk
                    outputs["llm_live_output"] = live[-50000:]
                    stream_state["content_buffer"] = ""
                    stream_state["last_content_flush"] = now
                    node["summary"] = "模型正在生成结构化结果……"
                    node["progress"] = max(int(node.get("progress") or 0), min(90, 12 + len(live) // 100))
                    message = "模型正在流式生成结构化结果。"
                    event_name = "llm_content_delta"
                    event_data["delta"] = chunk
                elif event_type == "reasoning_start":
                    outputs.setdefault("llm_reasoning", "")
                    node["summary"] = "模型正在分析任务与依赖关系……"
                    node["progress"] = max(int(node.get("progress") or 0), 6)
                    message = "模型开始输出可审计推理过程。"
                    event_name = "llm_reasoning_start"
                elif event_type == "reasoning_delta":
                    chunk = stream_state["reasoning_buffer"]
                    reasoning = str(outputs.get("llm_reasoning") or "") + chunk
                    outputs["llm_reasoning"] = reasoning[-50000:]
                    stream_state["reasoning_buffer"] = ""
                    stream_state["last_reasoning_flush"] = now
                    node["summary"] = "模型正在分析任务与依赖关系……"
                    node["progress"] = max(int(node.get("progress") or 0), min(88, 8 + len(reasoning) // 120))
                    message = "模型推理内容已更新。"
                    event_name = "llm_reasoning_delta"
                    event_data["delta"] = chunk
                elif event_type == "reasoning_done":
                    if stream_state["reasoning_buffer"]:
                        chunk = stream_state["reasoning_buffer"]
                        outputs["llm_reasoning"] = (str(outputs.get("llm_reasoning") or "") + chunk)[-50000:]
                        stream_state["reasoning_buffer"] = ""
                        event_data["delta"] = chunk
                    message = "模型推理阶段完成。"
                    event_name = "llm_reasoning_done"
                elif event_type == "llm_json_repair":
                    message = "模型首次输出未通过结构校验，正在自动修复 JSON。"
                    event_name = "llm_json_repair"
                    node["logs"].append(self._log_entry(message, "warning", {"error": payload.get("error")}))
                elif event_type == "llm_request_started":
                    message = f"调用模型 {payload.get('model') or task.get('planner', {}).get('model')}（第 {payload.get('attempt', 1)} 次）。"
                    event_name = "llm_request_started"
                    node["logs"].append(self._log_entry(message, "info"))
                elif event_type == "llm_request_completed":
                    if stream_state["content_buffer"]:
                        outputs["llm_live_output"] = (str(outputs.get("llm_live_output") or "") + stream_state["content_buffer"])[-50000:]
                        stream_state["content_buffer"] = ""
                    if stream_state["reasoning_buffer"]:
                        outputs["llm_reasoning"] = (str(outputs.get("llm_reasoning") or "") + stream_state["reasoning_buffer"])[-50000:]
                        stream_state["reasoning_buffer"] = ""
                    node["progress"] = max(int(node.get("progress") or 0), 95)
                    message = "模型输出完成，正在校验结构、占位符、工具参数和依赖 DAG。"
                    event_name = "llm_request_completed"
                    node["logs"].append(self._log_entry(message, "success"))
                else:
                    return
                node["updated_at"] = utc_now_iso()
                self._append_event_locked(task, event_name, message, node_id=node_id, data=event_data)
                self._save_task_locked(task)

        return handler

    @staticmethod
    def _execution_nodes_in_order(task: dict[str, Any]) -> list[dict[str, Any]]:
        bootstrap_ids = {"template_analysis", "instruction_analysis", "plan_generation", "approval"}
        return [node for node in task.get("nodes", []) if node.get("id") not in bootstrap_ids]

    @staticmethod
    def _node_for_executor(task: dict[str, Any], executor: str) -> dict[str, Any]:
        node = next((item for item in task.get("nodes", []) if item.get("executor") == executor), None)
        if node is None:
            raise KeyError(executor)
        return node

    def _preflight_plan_locked(self, task: dict[str, Any]) -> None:
        """Validate every generated node before asking the operator to approve it."""
        issues: list[str] = []
        for node in self._execution_nodes_in_order(task):
            if node.get("kind") != "tool_call":
                continue
            tool_name = str(node.get("tool_name") or node.get("allowed_tool") or "")
            if tool_name not in self._tool_definitions:
                issues.append(f"节点 {node.get('id')} 引用了不可用工具 {tool_name}")
                continue
            raw_args = self._inject_capture_tool_context(tool_name, dict(node.get("tool_arguments") or {}), task)
            # Static placeholders must resolve now. Dynamic node-output placeholders are
            # validated structurally by CaptureLLMPlanner and resolved when ancestors finish.
            for expression, location in self.planner._iter_placeholders(raw_args):
                tokens = self.planner._reference_tokens(expression)
                if tokens and tokens[0] == "nodes":
                    continue
                try:
                    self._lookup_plan_reference(expression, task)
                except RuntimeError as exc:
                    issues.append(f"节点 {node.get('id')} / {location}: {exc}")
            schema = dict(self._tool_definitions[tool_name].input_schema or {})
            required = set(schema.get("required") or [])
            supplied = set(raw_args)
            for key in sorted(required - supplied):
                if self._infer_tool_argument(tool_name, key, task, node) is _MISSING:
                    issues.append(f"节点 {node.get('id')} 的工具 {tool_name} 缺少必填参数 {key}")
        if issues:
            raise RuntimeError("LLM 计划预检失败：" + "；".join(issues[:12]))

    def _task_plan_fingerprint(self, task: dict[str, Any]) -> str:
        proposal = dict(task.get("plan_candidate") or {})
        return self.planner.plan_fingerprint(
            task.get("nodes", []),
            constraints=dict(task.get("constraints") or {}),
            plan_version=int(task.get("plan_version") or 1),
            objective=str(proposal.get("objective") or task.get("instruction") or ""),
            completion_contract=list(proposal.get("completion_contract") or []),
        )

    def _assert_plan_integrity_locked(self, task: dict[str, Any]) -> None:
        expected = str(task.get("approved_plan_fingerprint") or "")
        if not expected:
            raise RuntimeError("批准计划缺少锁定指纹")
        actual = self._task_plan_fingerprint(task)
        if actual != expected:
            raise RuntimeError("批准后的计划结构发生变化，执行器已拒绝继续")

    def _complete_node(self, task_id: str, node_id: str, summary: str, outputs: dict[str, Any]) -> None:
        with self._lock:
            task = self._load_task_locked(task_id)
            node = self._node(task, node_id)
            existing_outputs = dict(node.get("outputs") or {})
            streamed_outputs = {
                key: value
                for key, value in existing_outputs.items()
                if key.startswith("llm_")
            }
            node.update(
                {
                    "status": "completed",
                    "progress": 100,
                    "ended_at": utc_now_iso(),
                    "updated_at": utc_now_iso(),
                    "summary": summary,
                    # Streamed reasoning/content is produced before the final
                    # structured result. Preserve it instead of replacing the
                    # node output when the node is marked completed.
                    "outputs": {**streamed_outputs, **outputs},
                }
            )
            node["logs"].append(self._log_entry(summary, "success"))
            task["progress"] = self._task_progress(task)
            self._append_event_locked(task, "node_completed", summary, node_id=node_id, data={"progress": task["progress"]})
            self._save_task_locked(task)

    def _begin_node_locked(self, task: dict[str, Any], node: dict[str, Any], message: str) -> None:
        node["status"] = "in_progress"
        node["progress"] = max(1, int(node.get("progress") or 0))
        node["attempts"] = int(node.get("attempts") or 0) + 1
        node["started_at"] = node.get("started_at") or utc_now_iso()
        node["updated_at"] = utc_now_iso()
        node["summary"] = message
        node["inputs"] = self._node_inputs(task, node["id"])
        node["logs"].append(self._log_entry(message, "info"))
        task["current_node_id"] = node["id"]
        task["status"] = "planning" if node["id"] in {"template_analysis", "instruction_analysis", "plan_generation"} else "running"
        self._append_event_locked(task, "node_started", message, node_id=node["id"])

    @staticmethod
    def _assert_dependencies(task: dict[str, Any], node: dict[str, Any]) -> None:
        nodes = {item["id"]: item for item in task["nodes"]}
        incomplete = [dep for dep in node.get("dependencies", []) if nodes.get(dep, {}).get("status") != "completed"]
        if incomplete:
            raise RuntimeError(f"节点 {node['id']} 的依赖尚未完成：{', '.join(incomplete)}")

    def _wait_if_paused(self, task_id: str) -> None:
        while self._pause_events.setdefault(task_id, threading.Event()).is_set():
            if self._cancel_events.setdefault(task_id, threading.Event()).is_set():
                return
            time.sleep(0.2)

    def _should_stop(self, task_id: str) -> bool:
        if self._cancel_events.setdefault(task_id, threading.Event()).is_set():
            return True
        with self._lock:
            try:
                return self._load_task_locked(task_id).get("status") == "cancelled"
            except KeyError:
                return True

    @staticmethod
    def _plan_requires_device_access(task: dict[str, Any]) -> bool:
        device_tools = {
            "scan_usrp_devices",
            "list_usrp_devices",
            "configure_usrp_capture",
            "query_usrp_task",
            "execute_usrp_task_code",
            "run_autonomous_usrp_task",
            "execute_spectrum_collection",
        }
        return any(
            (node.get("tool_name") or node.get("allowed_tool")) in device_tools
            for node in task.get("nodes", [])
        )

    def _find_conflicting_real_task_locked(self, exclude_task_id: str) -> dict[str, Any] | None:
        for path in self.tasks_dir.glob("*.json"):
            try:
                candidate = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if candidate.get("id") == exclude_task_id:
                continue
            if (
                candidate.get("plan_locked")
                and candidate.get("status") in {"running", "paused"}
                and self._plan_requires_device_access(candidate)
            ):
                return candidate
        return None

    def _recover_interrupted_tasks(self) -> None:
        for path in self.tasks_dir.glob("*.json"):
            try:
                task = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if task.get("status") in {"planning", "running"}:
                task["status"] = "paused"
                task["pause_reason"] = "service_restart"
                task["error"] = "服务重启后任务已安全暂停，请人工继续。"
                current = task.get("current_node_id")
                if current:
                    node = next((item for item in task.get("nodes", []) if item.get("id") == current), None)
                    if node and node.get("status") == "in_progress":
                        node["status"] = "pending"
                        node["summary"] = "服务重启后等待恢复"
                self._append_event_locked(task, "task_recovered", "检测到服务重启，任务已安全暂停。", node_id=current, level="warning")
                self._save_task_locked(task)

    def _load_task_locked(self, task_id: str) -> dict[str, Any]:
        path = self.tasks_dir / f"{task_id}.json"
        if not path.exists():
            raise KeyError(task_id)
        return json.loads(path.read_text(encoding="utf-8"))

    def _save_task_locked(self, task: dict[str, Any]) -> None:
        task["updated_at"] = utc_now_iso()
        self._write_json(self.tasks_dir / f"{task['id']}.json", task)

    @staticmethod
    def _write_json(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        temp.replace(path)

    @staticmethod
    def _node(task: dict[str, Any], node_id: str) -> dict[str, Any]:
        node = next((item for item in task.get("nodes", []) if item.get("id") == node_id), None)
        if node is None:
            raise KeyError(node_id)
        return node

    @staticmethod
    def _log_entry(message: str, level: str = "info", data: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"timestamp": utc_now_iso(), "level": level, "message": message, "data": data or {}}

    def _append_event_locked(
        self,
        task: dict[str, Any],
        event_type: str,
        message: str,
        *,
        node_id: str | None = None,
        level: str = "info",
        data: dict[str, Any] | None = None,
    ) -> None:
        events = task.setdefault("events", [])
        seq = int(events[-1].get("seq") or 0) + 1 if events else 1
        events.append(
            {
                "seq": seq,
                "type": event_type,
                "task_id": task["id"],
                "node_id": node_id,
                "message": message,
                "level": level,
                "data": data or {},
                "created_at": utc_now_iso(),
            }
        )
        if len(events) > 3000:
            del events[:-3000]

    @staticmethod
    def _task_progress(task: dict[str, Any]) -> int:
        nodes = task.get("nodes", [])
        if not nodes:
            return 0
        total = sum(int(item.get("progress") or 0) for item in nodes)
        return max(0, min(100, int(total / len(nodes))))

    def _add_artifact_locked(self, task: dict[str, Any], path: Path, kind: str) -> dict[str, Any]:
        resolved = path.resolve()
        existing = next((item for item in task.get("artifacts", []) if item.get("path") == str(resolved)), None)
        if existing:
            existing["size_bytes"] = resolved.stat().st_size if resolved.exists() else 0
            existing["updated_at"] = utc_now_iso()
            return existing
        artifact = {
            "id": f"artifact_{uuid4().hex[:12]}",
            "kind": kind,
            "file_name": resolved.name,
            "path": str(resolved),
            "size_bytes": resolved.stat().st_size if resolved.exists() else 0,
            "created_at": utc_now_iso(),
        }
        task.setdefault("artifacts", []).append(artifact)
        return artifact

    @staticmethod
    def _public_template(payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": payload["id"],
            "name": payload.get("name"),
            "file_name": payload.get("file_name"),
            "size_bytes": payload.get("size_bytes"),
            "text_preview": payload.get("text_preview"),
            "headings": payload.get("headings", []),
            "table_count": len(payload.get("tables", [])),
            "created_at": payload.get("created_at"),
            "updated_at": payload.get("updated_at"),
        }

    def _all_task_payloads_locked(self) -> list[dict[str, Any]]:
        payloads: list[dict[str, Any]] = []
        for path in self.tasks_dir.glob("*.json"):
            try:
                payloads.append(json.loads(path.read_text(encoding="utf-8")))
            except Exception:
                continue
        payloads.sort(key=lambda item: (str(item.get("created_at") or ""), str(item.get("id") or "")))
        return payloads

    def _task_index_locked(self, task_id: str) -> int | None:
        for index, payload in enumerate(self._all_task_payloads_locked(), start=1):
            if payload.get("id") == task_id:
                return index
        return None

    @staticmethod
    def _task_display_fields(payload: dict[str, Any], index: int) -> dict[str, Any]:
        title = str(payload.get("title") or payload.get("instruction") or payload.get("id") or "采集任务").strip()
        return {"task_number": index, "display_title": f"任务{index}：{title}"}

    @staticmethod
    def _coerce_approval_constraints(current: dict[str, Any], parameters: dict[str, Any]) -> dict[str, Any]:
        result = dict(current or {})

        def has_value(key: str) -> bool:
            return key in parameters and parameters.get(key) not in {None, ""}

        text_keys = {"room_name", "mode", "aggregation", "antenna"}
        numeric_keys = {"freq_start_mhz", "freq_stop_mhz", "freq_step_mhz", "sample_rate", "bandwidth", "gain"}
        integer_keys = {"repeat_count", "expected_fft_frames_per_capture"}
        for key in text_keys:
            if has_value(key):
                result[key] = str(parameters[key]).strip()
        for key in numeric_keys:
            if has_value(key):
                result[key] = float(parameters[key])
        for key in integer_keys:
            if has_value(key):
                result[key] = int(float(parameters[key]))
        if has_value("dwell_ms"):
            result["dwell_time_sec"] = float(parameters["dwell_ms"]) / 1000.0
        elif has_value("dwell_time_sec"):
            result["dwell_time_sec"] = float(parameters["dwell_time_sec"])
        return CapturePlanBuilder.normalize_constraints(result)

    @staticmethod
    def _task_summary(payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": payload.get("id"),
            "title": payload.get("title"),
            "instruction": payload.get("instruction"),
            "status": payload.get("status"),
            "pause_reason": payload.get("pause_reason"),
            "progress": payload.get("progress", 0),
            "plan_version": payload.get("plan_version", 1),
            "plan_locked": payload.get("plan_locked", False),
            "current_node_id": payload.get("current_node_id"),
            "created_at": payload.get("created_at"),
            "updated_at": payload.get("updated_at"),
        }

    @staticmethod
    def _public_task(payload: dict[str, Any]) -> dict[str, Any]:
        task = _deepcopy_json(payload)
        for node in task.get("nodes", []):
            outputs = node.get("outputs")
            if isinstance(outputs, dict) and "combined_markdown" in outputs:
                outputs["combined_markdown"] = str(outputs["combined_markdown"])[:4000]
        task["generated_code"] = str(task.get("generated_code") or "")
        for artifact in task.get("artifacts", []):
            artifact.pop("path", None)
            base_url = f"/api/capture-agent/tasks/{task['id']}/artifacts/{artifact['id']}"
            artifact["download_url"] = base_url
            if str(artifact.get("file_name") or "").lower().endswith(".npz"):
                artifact["spectrum_url"] = f"{base_url}/spectrum"
        return task

    @staticmethod
    def _render_markdown_report(task: dict[str, Any]) -> str:
        lines = [
            f"# DeepEM 采集智能体任务报告：{task['id']}",
            "",
            f"- 状态：{task['status']}",
            f"- 计划版本：v{task['plan_version']}",
            f"- 创建时间：{task['created_at']}",
            f"- 指令：{task['instruction']}",
            "",
            "## 结构化采集参数",
            "",
            "```json",
            json.dumps(task.get("constraints", {}), ensure_ascii=False, indent=2),
            "```",
            "",
            "## 节点执行记录",
            "",
        ]
        for node in task.get("nodes", []):
            lines.extend(
                [
                    f"### {node['title']} `{node['status']}`",
                    "",
                    node.get("summary") or "",
                    "",
                    f"- 依赖：{', '.join(node.get('dependencies') or []) or '无'}",
                    f"- 允许工具：{node.get('allowed_tool') or '无'}",
                    f"- 开始：{node.get('started_at') or '-'}",
                    f"- 结束：{node.get('ended_at') or '-'}",
                    f"- 人工备注：{node.get('notes') or '-'}",
                    "",
                    "验收条件：",
                ]
            )
            lines.extend([f"- {item}" for item in node.get("success_criteria", [])])
            lines.extend(["", "执行日志："])
            lines.extend([f"- {entry.get('timestamp')} [{entry.get('level')}] {entry.get('message')}" for entry in node.get("logs", [])])
            lines.append("")
        lines.extend(["## 执行结果", "", "```json", json.dumps(task.get("execution_result", {}), ensure_ascii=False, indent=2), "```", ""])
        return "\n".join(lines)
