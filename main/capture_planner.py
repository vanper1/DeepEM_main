from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Callable, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .probe_evidence_compaction import build_probe_evidence_pack


PlannerStreamHandler = Callable[[str, dict[str, Any]], None]


class CapturePlanningUnavailable(RuntimeError):
    """Raised when no real remote/local model endpoint is configured."""


class CapturePlanValidationError(ValueError):
    """Raised when an LLM plan cannot be validated as an executable DAG."""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class CaptureIntentConstraints(BaseModel):
    """Complete collection parameters resolved for this task.

    Every field is required. Values explicitly supplied by the operator or a
    template must be preserved; only genuinely absent fields may be designed by
    the LLM from the task intent.
    """

    model_config = ConfigDict(extra="forbid")

    room_name: str = Field(min_length=1, max_length=120)
    mode: str = Field(min_length=1, max_length=80)
    freq_start_mhz: float = Field(gt=0)
    freq_stop_mhz: float = Field(gt=0)
    freq_step_mhz: float = Field(gt=0)
    dwell_time_sec: float = Field(gt=0)
    repeat_count: int = Field(ge=1)
    aggregation: Literal["mean", "median"]
    sample_rate: float = Field(gt=0)
    bandwidth: float = Field(gt=0)
    gain: float
    antenna: str = Field(min_length=1, max_length=40)
    expected_fft_frames_per_capture: int = Field(ge=1)


class CaptureIntentProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    constraints: CaptureIntentConstraints
    reason: str = Field(min_length=1, max_length=500)



DeviceType = Literal["usrp", "wifi_bluetooth_probe"]


class CaptureDeviceSelection(BaseModel):
    """LLM decision about which currently available acquisition devices are needed."""

    model_config = ConfigDict(extra="forbid")

    objective: str = Field(min_length=1, max_length=1200)
    selected_device_types: list[DeviceType] = Field(min_length=1, max_length=2)
    rationale: dict[str, str] = Field(default_factory=dict)
    probe_kinds: list[Literal["wifi_ap", "wifi_client", "bluetooth"]] = Field(default_factory=list, max_length=3)
    active_minutes: int = Field(default=30, ge=1, le=1440)
    unavailable_requirements: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("selected_device_types")
    @classmethod
    def unique_device_types(cls, value: list[DeviceType]) -> list[DeviceType]:
        result: list[DeviceType] = []
        for item in value:
            if item not in result:
                result.append(item)
        return result


NodeKind = Literal["llm_analysis", "tool_call", "llm_verification", "decision"]


class CapturePlanNodeProposal(BaseModel):
    """One freely planned node.

    There is intentionally no domain-stage/executor enum. The model may create any
    number of semantic nodes. A node either asks the LLM to analyse/decide/verify,
    or invokes one concrete registered tool. Tools may be reused in multiple nodes.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=2, max_length=80)
    title: str = Field(min_length=1, max_length=160)
    description: str = Field(min_length=1, max_length=2000)
    kind: NodeKind
    dependencies: list[str] = Field(default_factory=list, max_length=40)
    tool_name: str | None = Field(default=None, max_length=100)
    tool_arguments: dict[str, Any] = Field(default_factory=dict)
    success_criteria: list[str] = Field(min_length=1, max_length=30)
    expected_evidence: list[str] = Field(default_factory=list, max_length=30)
    risk_level: Literal["low", "medium", "high"] = "medium"
    max_attempts: int = Field(default=2, ge=1, le=3)

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
        normalized = re.sub(r"[^a-z0-9_]", "", normalized)
        if not normalized or not re.match(r"^[a-z][a-z0-9_]*$", normalized):
            raise ValueError("节点 id 必须以英文字母开头，且只包含小写字母、数字和下划线")
        return normalized

    @model_validator(mode="after")
    def validate_action(self) -> "CapturePlanNodeProposal":
        if self.kind == "tool_call" and not self.tool_name:
            raise ValueError("tool_call 节点必须提供 tool_name")
        if self.kind != "tool_call" and self.tool_name:
            raise ValueError("非 tool_call 节点不得提供 tool_name")
        if self.kind != "tool_call" and self.tool_arguments:
            raise ValueError("非 tool_call 节点不得提供 tool_arguments")
        return self


class CapturePlanProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    objective: str = Field(min_length=1, max_length=1200)
    strategy_summary: str = Field(min_length=1, max_length=5000)
    completion_contract: list[str] = Field(min_length=1, max_length=30)
    nodes: list[CapturePlanNodeProposal] = Field(min_length=1, max_length=32)


class CaptureNodeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["completed", "blocked", "failed"]
    summary: str = Field(min_length=1, max_length=3000)
    observations: list[str] = Field(default_factory=list, max_length=40)
    evidence: list[dict[str, Any]] = Field(default_factory=list, max_length=40)
    output: dict[str, Any] = Field(default_factory=dict)
    should_replan: bool = False
    replan_reason: str = Field(default="", max_length=2000)


class CaptureLLMPlanner:
    """Real-LLM planner with a non-templating safety validator.

    The model owns task decomposition. The validator only checks JSON shape,
    registered-tool references, arguments, DAG integrity, limits and approval
    binding. It never inserts workflow nodes, reorders semantic stages, or forbids
    reuse of the same tool.
    """

    def __init__(self, llm_client: Any) -> None:
        self.llm_client = llm_client

    USRP_PARAMETER_RANGES: dict[str, Any] = {
        "freq_start_mhz": {"min": 45.0, "max": 6000.0},
        "freq_stop_mhz": {"min": 45.0, "max": 6000.0},
        "freq_step_mhz": {"min": 0.001, "max": 1000.0},
        "sample_rate": {"min": 0.03125, "max": 16.0},
        "bandwidth": {"min": 0.2, "max": 16.0},
        "gain": {"min": 0.0, "max": 70.0},
        "dwell_time_sec": {"min": 0.01, "max": 60.0},
        "repeat_count": {"min": 1, "max": 3},
        "expected_fft_frames_per_capture": {"min": 1, "max": 10000},
        "antenna": ["TX/RX", "RX2"],
    }
    USRP_PARAMETER_RELATIONSHIPS: list[str] = [
        "freq_start_mhz 必须小于或等于 freq_stop_mhz",
        "bandwidth 必须小于或等于 sample_rate",
    ]

    @classmethod
    def usrp_parameter_context(cls) -> dict[str, Any]:
        """Return a fresh copy of the physical USRP parameter limits for every planning request."""
        return {
            "ranges": json.loads(json.dumps(cls.USRP_PARAMETER_RANGES, ensure_ascii=False)),
            "relationships": list(cls.USRP_PARAMETER_RELATIONSHIPS),
        }

    @staticmethod
    def normalize_reasoning_mode(value: str | None) -> str:
        return "deep" if str(value or "").strip().lower() == "deep" else "fast"

    @classmethod
    def generation_options_for_mode(cls, reasoning_mode: str | None, *, temperature: float = 0.1) -> dict[str, Any]:
        mode = cls.normalize_reasoning_mode(reasoning_mode)
        return {
            "temperature": temperature,
            "enable_thinking": mode == "deep",
            "preserve_thinking": mode == "deep",
        }

    def runtime_info(self) -> dict[str, Any]:
        client = self.llm_client
        client_name = client.__class__.__name__ if client is not None else "None"
        settings = getattr(client, "settings", None)
        model = str(getattr(settings, "model", "") or "")
        base_url = str(getattr(settings, "base_url", "") or "")
        api_key_configured = bool(str(getattr(settings, "api_key", "") or "").strip())
        endpoint = ""
        if base_url:
            parsed = urlparse(base_url)
            endpoint = parsed.netloc or parsed.path
        real = (
            client is not None
            and client_name != "LocalWorkflowLLMClient"
            and callable(getattr(client, "complete", None))
            and bool(model)
            and bool(base_url)
            and api_key_configured
        )
        return {
            "available": real,
            "mode": "real_llm" if real else "unavailable",
            "client": client_name,
            "model": model or "未配置",
            "endpoint": endpoint or "未配置",
            "planner": "freeform_llm_dag_plus_non_templating_validator",
            "fallback_enabled": False,
        }

    def ensure_available(self) -> None:
        if not self.runtime_info()["available"]:
            raise CapturePlanningUnavailable(
                "采集智能体要求真实 LLM 规划器。请配置 DEEPEM_LLM_API_KEY、DEEPEM_LLM_BASE_URL、"
                "DEEPEM_LLM_MODEL 后重启服务；本功能不会回退到本地固定计划。"
            )

    def select_devices(
        self,
        *,
        instruction: str,
        template_markdown: str,
        available_devices: dict[str, Any],
        reasoning_mode: str = "fast",
        stream_handler: PlannerStreamHandler | None = None,
        cancel_checker: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        self.ensure_available()
        system = (
            "你是 DeepEM 采集智能体的设备规划器。先依据任务背景和当前可用设备清单决定真正需要调用哪些设备，"
            "USRP 适合原始 IQ、频谱、指定频段/频率、射频能量与后续频谱分析；"
            "一般任务时，都应考虑wifi_bluetooth_probe；"
            "只有当问题明显偏离2.4GHz频段时，此时不考虑wifi_bluetooth_probe；"
            "probe_kinds 只在选择探针时填写需要的 wifi_ap/wifi_client/bluetooth；无法满足的需求写入 unavailable_requirements。"
            "请在 reasoning 通道给出简洁可审计理由；最终 content 只输出符合 JSON Schema 的 JSON。"
        )
        proposal, raw_text = self._complete_json(
            schema_model=CaptureDeviceSelection,
            messages=[
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "instruction": instruction,
                            "template_markdown": template_markdown[:30000],
                            "available_devices": available_devices,
                            "output_json_schema": CaptureDeviceSelection.model_json_schema(),
                        },
                        ensure_ascii=False,
                        default=str,
                    ),
                },
            ],
            phase="device_selection",
            reasoning_mode=reasoning_mode,
            stream_handler=stream_handler,
            cancel_checker=cancel_checker,
        )
        return {
            "proposal": proposal.model_dump(mode="json"),
            "raw_model_output": raw_text,
            "model": self.runtime_info(),
        }

    def analyze_intent(
        self,
        *,
        instruction: str,
        template_markdown: str,
        parameter_context: dict[str, Any],
        previous_analysis: dict[str, Any] | None = None,
        reasoning_mode: str = "fast",
        stream_handler: PlannerStreamHandler | None = None,
        cancel_checker: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        self.ensure_available()
        usrp_parameter_context = self.usrp_parameter_context()
        parameter_context = dict(parameter_context or {})
        # Always attach the real device limits to the same user payload that asks
        # the model to generate USRP parameters.  Keeping the limits only in the
        # system prompt proved too easy to lose across provider/model variations.
        parameter_context["usrp_device_parameter_ranges"] = usrp_parameter_context["ranges"]
        parameter_context["usrp_parameter_relationships"] = usrp_parameter_context["relationships"]
        ranges_text = json.dumps(usrp_parameter_context["ranges"], ensure_ascii=False, separators=(",", ":"))
        system = (
            "你只负责生成需要用户确认的 USRP 采集参数。"
            "参数优先级：用户明确指定 > 模板明确指定 > 模型根据当前任务意图补全。"
            "用户或模板已经明确的参数必须原样保留，不得修改。"
            "只有未明确的参数才允许根据任务意图补全。"
            "所有采集参数都必须给出非 null 的最终值。"
            "频率、采样率、带宽统一使用 MHz，时间统一使用秒。"
            f"所有生成参数必须满足以下设备允许范围：{ranges_text}。"
            "参数关系约束：freq_start_mhz 必须小于或等于 freq_stop_mhz；"
            "bandwidth 必须小于或等于 sample_rate。"
            "只输出 constraints 和 reason。"
            "reason 只需简短说明，说明参数为什么这样设置，不要展开分析。"
            "不要生成目标、任务摘要、假设、未决问题、风险、来源追踪或其他内容。"
            "最终只输出符合 JSON Schema 的 JSON 对象，不输出 Markdown 或额外解释。"
        )

        proposal, raw_text = self._complete_json(
            schema_model=CaptureIntentProposal,
            messages=[
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "instruction": instruction,
                            "template_markdown": template_markdown[:60000],
                            "parameter_resolution_context": parameter_context,
                            "usrp_device_parameter_ranges": usrp_parameter_context["ranges"],
                            "usrp_parameter_relationships": usrp_parameter_context["relationships"],
                            "previous_analysis_for_revision": previous_analysis or {},
                            "output_json_schema": CaptureIntentProposal.model_json_schema(),
                        },
                        ensure_ascii=False,
                        default=str,
                    ),
                },
            ],
            phase="intent_analysis",
            reasoning_mode=reasoning_mode,
            stream_handler=stream_handler,
            cancel_checker=cancel_checker,
        )
        return {
            "proposal": proposal.model_dump(mode="json"),
            "raw_model_output": raw_text,
            "model": self.runtime_info(),
        }

    def generate_plan(
        self,
        *,
        instruction: str,
        intent_analysis: dict[str, Any],
        normalized_constraints: dict[str, Any],
        validation: dict[str, Any],
        tool_catalog: dict[str, dict[str, Any]],
        plan_version: int,
        previous_plan: dict[str, Any] | None = None,
        reasoning_mode: str = "fast",
        stream_handler: PlannerStreamHandler | None = None,
        cancel_checker: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        self.ensure_available()
        system = (
            "你是 DeepEM 的 Codex 风格任务规划智能体。必须根据当前任务从零设计执行 DAG，禁止套用统一采集流程，"
            "禁止为了形式完整而机械加入设备扫描、配置、代码生成、采集、验证、保存等固定阶段。"
            "只有任务确实需要某项操作时才创建对应节点。你可以创建任意语义的 LLM 分析、决策、验证节点，"
            "也可以创建工具节点；同一工具可在不同节点重复使用，节点数量和依赖结构应随任务显著变化。"
            "tool_call 节点只能引用 tool_catalog 中真实存在的工具。工具参数可使用占位符："
            "${task.id}、${task.instruction}、${constraints.<key>}、${nodes.<node_id>.outputs.<path>}、"
            "${runtime.output_dir}、${runtime.autonomous_task_plan}。路径支持数组索引，例如 "
            "${nodes.scan.outputs.devices[0].dev_id}；可使用 :- 提供默认值，例如 ${nodes.scan.outputs.task_id:-}。"
            "占位符只能引用当前节点的前置祖先节点，不能引用自身或未来节点。系统只校验安全与 DAG，不会补节点或重排流程。"
            "根节点稍后统一绑定人工审批门禁。请在模型 reasoning 通道输出简洁、可审计的推理摘要；最终 content 只输出符合 JSON Schema 的 JSON。"
        )
        user_payload = {
            "instruction": instruction,
            "intent_analysis": intent_analysis,
            "normalized_constraints": normalized_constraints,
            "safety_validation": validation,
            "available_real_tools": tool_catalog,
            "planning_requirements": [
                "从任务目标推导节点，不得套用预设流程",
                "允许 1 到 32 个节点",
                "同一工具可以重复调用",
                "dependencies 只能引用当前计划节点 id",
                "LLM 分析/决策/验证节点不填 tool_name 和 tool_arguments",
                "工具节点 kind=tool_call 且必须填写真实 tool_name",
                "不得扩大已规范化的频率范围、重复次数或真实设备权限",
                "每个节点必须有可验证成功条件和预期证据",
            ],
            "execution_mode": "real_device",
            "plan_version": int(plan_version),
            "previous_plan_for_revision": previous_plan or {},
            "output_json_schema": CapturePlanProposal.model_json_schema(),
        }
        proposal, raw_text = self._complete_json(
            schema_model=CapturePlanProposal,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False, default=str)},
            ],
            phase="plan_generation",
            reasoning_mode=reasoning_mode,
            stream_handler=stream_handler,
            cancel_checker=cancel_checker,
        )
        compiled = self.compile_plan(proposal, tool_catalog=tool_catalog)
        return {
            "proposal": proposal.model_dump(mode="json"),
            "compiled_nodes": compiled,
            "raw_model_output": raw_text,
            "model": self.runtime_info(),
            "plan_fingerprint": self.plan_fingerprint(
                compiled,
                constraints=normalized_constraints,
                plan_version=plan_version,
                objective=proposal.objective,
                completion_contract=proposal.completion_contract,
            ),
        }

    def execute_llm_node(
        self,
        *,
        task_context: dict[str, Any],
        node: dict[str, Any],
        dependency_outputs: dict[str, Any],
        reasoning_mode: str = "fast",
        stream_handler: PlannerStreamHandler | None = None,
        cancel_checker: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        self.ensure_available()
        system = (
            "你正在执行一个已批准任务计划中的单个节点。只处理当前节点，不得跳到其他节点，也不得修改已批准约束。"
            "依据依赖节点输出，形成结构化、可审计的节点结果。不要声称执行了未调用的工具。"
            "如信息不足，返回 blocked；如发现后续计划必须修改，可设置 should_replan=true 并说明原因。"
            "请在 reasoning 通道输出简洁、可审计的节点推理摘要；最终 content 只输出符合 JSON Schema 的 JSON，不输出 Markdown 或额外解释。"
        )
        result, raw_text = self._complete_json(
            schema_model=CaptureNodeResult,
            messages=[
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "task": task_context,
                            "current_node": node,
                            "dependency_outputs": dependency_outputs,
                            "output_json_schema": CaptureNodeResult.model_json_schema(),
                        },
                        ensure_ascii=False,
                        default=str,
                    ),
                },
            ],
            phase="node_execution",
            reasoning_mode=reasoning_mode,
            stream_handler=stream_handler,
            cancel_checker=cancel_checker,
        )
        return {
            "result": result.model_dump(mode="json"),
            "raw_model_output": raw_text,
            "model": self.runtime_info(),
        }

    def summarize_live_status(
        self,
        *,
        task_context: dict[str, Any],
        node_context: dict[str, Any],
        latest_reasoning: str,
        latest_structured_output: str = "",
    ) -> str:
        """Generate one short lifecycle or live status summary with the current LLM."""
        self.ensure_available()
        phase = str(node_context.get("phase") or "").strip().lower()
        is_terminal = phase in {"node_terminal", "completed", "failed", "cancelled"}
        is_start = phase in {"node_started", "start"}

        if is_terminal:
            phase_instruction = (
                "当前节点已经结束。请生成这个节点的最终摘要，概括已完成动作、关键结果或失败/取消原因。"
            )
            length_instruction = "输出 1 句话，建议 18 到 60 个汉字"
            max_tokens = 100
            max_chars = 100
        elif is_start:
            phase_instruction = (
                "当前节点刚开始执行。请生成这个节点此刻的状态概述，说明正在做什么以及当前目标。"
            )
            length_instruction = "输出 1 句话，建议 18 到 60 个汉字"
            max_tokens = 100
            max_chars = 100
        else:
            phase_instruction = (
                "当前节点正在执行。请根据最新可见 reasoning/content 的真实变化，"
                "生成一条随进展变化的中文进行时状态；不要复述上一条状态。"
            )
            length_instruction = "长度 10 到 32 个汉字，最多一行"
            max_tokens = 40
            max_chars = 48

        response = self.llm_client.complete(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是 DeepEM 采集智能体的摘要概述器。只基于提供的真实任务、节点状态和可见输出生成简洁中文概述，"
                        "不要展开隐藏推理过程，不得虚构尚未发生的动作、结果、设备状态或证据。"
                        f"{phase_instruction}"
                        f"{length_instruction}；不要引号、序号、Markdown、句号或解释，也不要在末尾添加省略号。"
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "task": task_context,
                            "current_node": node_context,
                            "latest_reasoning": latest_reasoning[-2400:],
                            "latest_structured_output": latest_structured_output[-800:],
                        },
                        ensure_ascii=False,
                        default=str,
                    ),
                },
            ],
            tools=[],
            temperature=0.05,
            generation_options={
                "temperature": 0.05,
                "top_p": 0.2,
                "max_tokens": max_tokens,
                "enable_thinking": False,
                "preserve_thinking": False,
            },
            stream_handler=None,
            cancel_checker=None,
        )
        text = str(getattr(response, "content", "") or "").strip()
        if not text:
            text = str(getattr(response, "reasoning", "") or "").strip()
        text = re.sub(r"^[\s\-—–•*#>\d.、）)]+", "", text)
        text = text.splitlines()[0].strip(" \t\r\n\"'“”‘’。；;！!") if text else ""
        return text[:max_chars]

    def summarize_probe_evidence(
        self,
        *,
        task_context: dict[str, Any],
        evidence: dict[str, Any],
        reasoning_mode: str = "fast",
        stream_handler: PlannerStreamHandler | None = None,
        cancel_checker: Callable[[], bool] | None = None,
    ) -> str:
        """Summarize WiFi/Bluetooth probe evidence without changing the USRP verdict."""
        self.ensure_available()
        mode = self.normalize_reasoning_mode(reasoning_mode)
        if str(evidence.get("format") or "") != "deepem-probe-evidence-pack-v1":
            evidence = build_probe_evidence_pack(evidence)["llm_evidence"]
        serialized_evidence = json.dumps(evidence, ensure_ascii=False, separators=(",", ":"), default=str)
        response = self.llm_client.complete(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是 DeepEM 的 WiFi/蓝牙辅助证据分析器。请基于真实探针接口返回的数据，形成简洁、可审计的中文总结。"
                        "必须区分 WiFi 热点、WiFi 客户端和蓝牙设备，优先概括在线探针数、目标数、观测数、最强或最近信号、"
                        "可能与 USRP 结果相关的环境线索和数据缺口。不得虚构身份、位置或因果关系。探针证据仅用于辅助研判，"
                        "不能覆盖或把已经成功的 USRP 结果改判为失败。输入是受限长度的证据包：统计项覆盖全部原始记录，records/priority_records 是重点明细，"
                        "coverage 会声明未直接展示给模型的记录数量，integrity 提供原始证据哈希。若 coverage 存在省略，必须在总结中明确说明研判边界。"
                        "最终只输出 2 至 5 段中文正文，不要 JSON、Markdown 标题或代码块。"
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps({"task": task_context}, ensure_ascii=False, default=str)
                    + "\nprobe_evidence="
                    + serialized_evidence,
                },
            ],
            tools=[],
            temperature=0.1,
            generation_options={
                **self.generation_options_for_mode(mode, temperature=0.1),
                "top_p": 0.6,
                "max_tokens": 900,
            },
            stream_handler=stream_handler,
            cancel_checker=cancel_checker,
        )
        text = str(getattr(response, "content", "") or "").strip()
        if not text:
            text = str(getattr(response, "reasoning", "") or "").strip()
        return text

    def compile_plan(
        self,
        proposal: CapturePlanProposal,
        *,
        tool_catalog: dict[str, dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        catalog = tool_catalog or {}
        proposed = list(proposal.nodes)
        id_seen: set[str] = set()
        by_id: dict[str, CapturePlanNodeProposal] = {}
        for node in proposed:
            if node.id in id_seen:
                raise CapturePlanValidationError(f"模型计划节点 id 重复：{node.id}")
            id_seen.add(node.id)
            by_id[node.id] = node
            if node.kind == "tool_call" and node.tool_name not in catalog:
                raise CapturePlanValidationError(f"模型计划引用了未授权或不存在的工具：{node.tool_name}")
            if node.kind == "tool_call" and node.tool_name:
                schema = dict((catalog.get(node.tool_name) or {}).get("input_schema") or {})
                properties = dict(schema.get("properties") or {})
                required = set(schema.get("required") or [])
                auto_injected = {
                    "generate_usrp_task_code": {"task_description", "task_plan"},
                    "execute_usrp_task_code": {"code", "task_description", "task_plan"},
                    "run_autonomous_usrp_task": {"task_description", "task_plan"},
                }.get(node.tool_name, set())
                missing = sorted(required - set(node.tool_arguments) - auto_injected)
                if missing:
                    raise CapturePlanValidationError(
                        f"工具节点 {node.id} 缺少必填参数：{', '.join(missing)}"
                    )
                additional = schema.get("additionalProperties")
                if properties and additional is not True:
                    unknown_args = sorted(set(node.tool_arguments) - set(properties))
                    if unknown_args:
                        raise CapturePlanValidationError(
                            f"工具节点 {node.id} 包含未定义参数：{', '.join(unknown_args)}"
                        )

        deps: dict[str, set[str]] = {}
        for node in proposed:
            unknown = [dep for dep in node.dependencies if dep not in by_id]
            if unknown:
                raise CapturePlanValidationError(f"节点 {node.id} 引用了不存在的依赖：{', '.join(unknown)}")
            if node.id in node.dependencies:
                raise CapturePlanValidationError(f"节点 {node.id} 不能依赖自身")
            deps[node.id] = set(node.dependencies)

        ordered_ids = self._topological_order(deps, proposed)
        self._validate_plan_placeholders(proposed, ordered_ids)
        now = utc_now_iso()
        compiled: list[dict[str, Any]] = []
        for node_id in ordered_ids:
            node = by_id[node_id]
            ordered_deps = sorted(deps[node_id], key=ordered_ids.index)
            if not ordered_deps:
                ordered_deps = ["approval"]
            compiled.append(
                {
                    "id": node.id,
                    "title": node.title,
                    "description": node.description,
                    "kind": node.kind,
                    "executor": node.kind,
                    "dependencies": ordered_deps,
                    "tool_name": node.tool_name,
                    "tool_arguments": self._normalize_placeholder_value(node.tool_arguments),
                    "allowed_tool": node.tool_name,
                    "allowed_tools": [node.tool_name] if node.tool_name else [],
                    "status": "pending",
                    "progress": 0,
                    "attempts": 0,
                    "max_attempts": node.max_attempts,
                    "started_at": None,
                    "ended_at": None,
                    "updated_at": now,
                    "summary": "等待批准计划后执行",
                    "success_criteria": list(node.success_criteria),
                    "expected_evidence": list(node.expected_evidence),
                    "risk_level": node.risk_level,
                    "planner_origin": "llm",
                    "inputs": {},
                    "outputs": {},
                    "logs": [],
                    "notes": "",
                }
            )
        return compiled

    @staticmethod
    def _topological_order(deps: dict[str, set[str]], proposed: list[CapturePlanNodeProposal]) -> list[str]:
        original_index = {node.id: index for index, node in enumerate(proposed)}
        remaining = {key: set(value) for key, value in deps.items()}
        ordered: list[str] = []
        while remaining:
            ready = [node_id for node_id, values in remaining.items() if not values]
            if not ready:
                cycle_nodes = ", ".join(sorted(remaining))
                raise CapturePlanValidationError(f"模型计划依赖图存在环：{cycle_nodes}")
            ready.sort(key=lambda node_id: original_index.get(node_id, 10000))
            for node_id in ready:
                ordered.append(node_id)
                remaining.pop(node_id)
                for values in remaining.values():
                    values.discard(node_id)
        return ordered

    _PLACEHOLDER_RE = re.compile(r"\$\{([^{}]+)\}")
    _ALLOWED_REFERENCE_ROOTS = {"task", "constraints", "nodes", "runtime", "generated_code", "intent"}

    @classmethod
    def _normalize_placeholder_expression(cls, expression: str) -> str:
        expr = str(expression or "").strip()
        value_expr, separator, default_expr = expr.partition(":-")
        value_expr = value_expr.strip()
        # Accept a few common aliases produced by different models.
        value_expr = re.sub(r"^node\.([a-z][a-z0-9_]*)\.", r"nodes.\1.", value_expr)
        value_expr = re.sub(r"^steps\.([a-z][a-z0-9_]*)\.", r"nodes.\1.", value_expr)
        value_expr = re.sub(r"^(nodes\.[a-z][a-z0-9_]*)\.output\.", r"\1.outputs.", value_expr)
        value_expr = re.sub(r"^(nodes\.[a-z][a-z0-9_]*)\.data\.", r"\1.outputs.", value_expr)
        normalized = value_expr
        if separator:
            normalized += ":-" + default_expr.strip()
        return normalized

    @classmethod
    def _normalize_placeholder_value(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {key: cls._normalize_placeholder_value(item) for key, item in value.items()}
        if isinstance(value, list):
            return [cls._normalize_placeholder_value(item) for item in value]
        if not isinstance(value, str):
            return value
        return cls._PLACEHOLDER_RE.sub(
            lambda match: "${" + cls._normalize_placeholder_expression(match.group(1)) + "}",
            value,
        )

    @classmethod
    def _iter_placeholders(cls, value: Any, location: str = "tool_arguments"):
        if isinstance(value, dict):
            for key, item in value.items():
                yield from cls._iter_placeholders(item, f"{location}.{key}")
            return
        if isinstance(value, list):
            for index, item in enumerate(value):
                yield from cls._iter_placeholders(item, f"{location}[{index}]")
            return
        if not isinstance(value, str):
            return
        # Reject broken syntax early instead of failing during collection.
        if "${" in value:
            spans = list(cls._PLACEHOLDER_RE.finditer(value))
            scrubbed = cls._PLACEHOLDER_RE.sub("", value)
            if "${" in scrubbed or "}" in scrubbed:
                raise CapturePlanValidationError(f"{location} 包含不完整占位符：{value}")
            for match in spans:
                yield cls._normalize_placeholder_expression(match.group(1)), location

    @staticmethod
    def _reference_tokens(expression: str) -> list[str]:
        value_expr = expression.partition(":-")[0].strip()
        # Convert bracket indexes and quoted keys into dot tokens.
        value_expr = re.sub(r"\[\s*['\"]([^'\"]+)['\"]\s*\]", r".\1", value_expr)
        value_expr = re.sub(r"\[\s*(\d+)\s*\]", r".\1", value_expr)
        return [part for part in value_expr.split(".") if part]

    @classmethod
    def _validate_plan_placeholders(
        cls,
        proposed: list[CapturePlanNodeProposal],
        ordered_ids: list[str],
    ) -> None:
        by_id = {node.id: node for node in proposed}
        direct_deps = {node.id: set(node.dependencies) for node in proposed}

        def ancestors(node_id: str) -> set[str]:
            result: set[str] = set()
            stack = list(direct_deps.get(node_id, set()))
            while stack:
                current = stack.pop()
                if current in result:
                    continue
                result.add(current)
                stack.extend(direct_deps.get(current, set()))
            return result

        order_index = {node_id: index for index, node_id in enumerate(ordered_ids)}
        for node in proposed:
            if node.kind != "tool_call":
                continue
            normalized_args = cls._normalize_placeholder_value(node.tool_arguments)
            for expression, location in cls._iter_placeholders(normalized_args):
                tokens = cls._reference_tokens(expression)
                if not tokens:
                    raise CapturePlanValidationError(f"节点 {node.id} 的 {location} 含空占位符")
                root = tokens[0]
                if root not in cls._ALLOWED_REFERENCE_ROOTS:
                    raise CapturePlanValidationError(
                        f"节点 {node.id} 的 {location} 使用未知占位符根：{root}"
                    )
                if root == "nodes":
                    if len(tokens) < 2:
                        raise CapturePlanValidationError(
                            f"节点 {node.id} 的 {location} 必须指定被引用节点 id"
                        )
                    referenced = tokens[1]
                    if referenced not in by_id:
                        raise CapturePlanValidationError(
                            f"节点 {node.id} 的 {location} 引用了不存在的节点：{referenced}"
                        )
                    if referenced == node.id or referenced not in ancestors(node.id):
                        relation = "自身" if referenced == node.id else "非前置祖先节点"
                        raise CapturePlanValidationError(
                            f"节点 {node.id} 的 {location} 引用了{relation} {referenced}；执行时该输出尚不可用"
                        )
                    if order_index[referenced] >= order_index[node.id]:
                        raise CapturePlanValidationError(
                            f"节点 {node.id} 的 {location} 引用了未来节点 {referenced}"
                        )
                elif root == "task" and len(tokens) >= 2:
                    allowed_task_fields = {"id", "instruction", "title", "plan_version", "output_dir"}
                    if tokens[1] not in allowed_task_fields:
                        raise CapturePlanValidationError(
                            f"节点 {node.id} 的 {location} 引用了未知任务字段：{tokens[1]}"
                        )
                elif root == "constraints" and len(tokens) >= 2:
                    allowed = set(CaptureIntentConstraints.model_fields)
                    if tokens[1] not in allowed:
                        raise CapturePlanValidationError(
                            f"节点 {node.id} 的 {location} 引用了未知约束字段：{tokens[1]}"
                        )
                elif root == "intent" and len(tokens) >= 2:
                    allowed_intent_fields = set(CaptureIntentProposal.model_fields)
                    if tokens[1] not in allowed_intent_fields:
                        raise CapturePlanValidationError(
                            f"节点 {node.id} 的 {location} 引用了未知意图字段：{tokens[1]}"
                        )
                elif root == "generated_code" and len(tokens) > 1:
                    raise CapturePlanValidationError(
                        f"节点 {node.id} 的 {location} 不能在 generated_code 后继续取子字段"
                    )
                elif root == "runtime" and len(tokens) >= 2:
                    if tokens[1] not in {"output_dir", "autonomous_task_plan"}:
                        raise CapturePlanValidationError(
                            f"节点 {node.id} 的 {location} 引用了未知运行时字段：{tokens[1]}"
                        )

    @staticmethod
    def plan_fingerprint(
        nodes: list[dict[str, Any]],
        *,
        constraints: dict[str, Any] | None = None,
        plan_version: int | None = None,
        objective: str | None = None,
        completion_contract: list[str] | None = None,
    ) -> str:
        structural_nodes = [
            {
                "id": node.get("id"),
                "title": node.get("title"),
                "description": node.get("description"),
                "kind": node.get("kind") or node.get("executor"),
                "dependencies": node.get("dependencies", []),
                "tool_name": node.get("tool_name") or node.get("allowed_tool"),
                "tool_arguments": node.get("tool_arguments", {}),
                "allowed_tools": node.get("allowed_tools", []),
                "success_criteria": node.get("success_criteria", []),
                "expected_evidence": node.get("expected_evidence", []),
                "risk_level": node.get("risk_level"),
                "max_attempts": node.get("max_attempts", 2),
                "planner_origin": node.get("planner_origin"),
            }
            for node in nodes
            if node.get("id") not in {
                "template_analysis",
                "device_discovery",
                "device_selection",
                "instruction_analysis",
                "plan_generation",
                "approval",
            }
        ]
        contract = {
            "fingerprint_schema": "deepem-capture-freeform-plan-v4",
            "plan_version": plan_version,
            "objective": objective or "",
            "completion_contract": completion_contract or [],
            "constraints": constraints or {},
            "nodes": structural_nodes,
        }
        encoded = json.dumps(contract, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _complete_json(
        self,
        *,
        schema_model: type[BaseModel],
        messages: list[dict[str, Any]],
        phase: str,
        reasoning_mode: str = "fast",
        stream_handler: PlannerStreamHandler | None = None,
        cancel_checker: Callable[[], bool] | None = None,
    ) -> tuple[BaseModel, str]:
        last_text = ""
        last_error = ""
        current_messages = list(messages)
        for attempt in range(2):
            if stream_handler is not None:
                stream_handler("llm_request_started", {"phase": phase, "attempt": attempt + 1, "model": self.runtime_info().get("model")})
            response = self.llm_client.complete(
                messages=current_messages,
                tools=[],
                temperature=0.1,
                generation_options=self.generation_options_for_mode(reasoning_mode, temperature=0.1),
                stream_handler=(lambda event, data: stream_handler(event, {**data, "phase": phase, "attempt": attempt + 1})) if stream_handler else None,
                cancel_checker=cancel_checker,
            )
            last_text = str(getattr(response, "content", "") or "").strip()
            try:
                payload = self._extract_json_object(last_text)
                result = schema_model.model_validate(payload)
                if stream_handler is not None:
                    stream_handler("llm_request_completed", {"phase": phase, "attempt": attempt + 1})
                return result, last_text
            except (ValueError, json.JSONDecodeError, ValidationError) as exc:
                last_error = str(exc)
                if attempt == 0:
                    if stream_handler is not None:
                        stream_handler("llm_json_repair", {"phase": phase, "error": last_error[:1000]})
                    current_messages = [
                        {
                            "role": "system",
                            "content": "你是 JSON 修复器。只输出修复后的 JSON 对象，不要输出 Markdown 或解释。必须严格满足给定 JSON Schema。",
                        },
                        {
                            "role": "user",
                            "content": json.dumps(
                                {
                                    "invalid_output": last_text[:60000],
                                    "validation_error": last_error[:6000],
                                    "output_json_schema": schema_model.model_json_schema(),
                                },
                                ensure_ascii=False,
                            ),
                        },
                    ]
                    continue
        raise CapturePlanValidationError(f"真实 LLM 连续两次未返回合法结构化结果：{last_error or '无法解析 JSON'}")

    @staticmethod
    def _extract_json_object(text: str) -> dict[str, Any]:
        stripped = text.strip()
        fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", stripped, flags=re.I | re.S)
        if fence:
            stripped = fence.group(1).strip()
        try:
            payload = json.loads(stripped)
            if not isinstance(payload, dict):
                raise ValueError("模型输出根节点必须是 JSON 对象")
            return payload
        except json.JSONDecodeError:
            pass

        start = stripped.find("{")
        if start < 0:
            raise ValueError("模型输出中没有 JSON 对象")
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(stripped)):
            char = stripped[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    payload = json.loads(stripped[start : index + 1])
                    if not isinstance(payload, dict):
                        raise ValueError("模型输出根节点必须是 JSON 对象")
                    return payload
        raise ValueError("模型输出中的 JSON 对象不完整")
