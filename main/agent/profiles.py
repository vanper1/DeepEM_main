from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class AgentProfile:
    name: str
    description: str
    system_prompt: str
    allowed_tools: list[str]
    step_budget: int = 6
    temperature: float = 0.2


PLACE_DETECTION_AGENT = AgentProfile(
    name="place_detection_agent",
    description="面向异常信号复核与 Case 分诊的场所检测智能体。",
    system_prompt=(
        "你是 DeepEM 的场所异常检测智能体。\n"
        "你只处理可疑结构化信号事件及其后续调查。\n"
        "采集证据来自真实 USRP 设备的事件上报，原始 IQ 文件已由平台落本地文件，元信息会进入证据索引。\n"
        "请使用工具检查工作区状态、USRP 设备/任务、近期观测、按需汇总聚焦证据、请求深度分析，并更新 Case。"
        "所有结论都要尽量基于证据，不要跳到没有依据的异常判断。"
        "不要反复调用同一个工具"
    ),
    allowed_tools=[
        # "query_state",
        "query_cases",
        "query_knowledge",
        "query_recent_observations",
        "scan_usrp_devices",
        "list_usrp_devices",
        "configure_usrp_capture",
        "query_usrp_task",
        "request_focused_collection",
        "request_deep_analysis",
        "update_case",
        "update_state",
    ],
    step_budget=6,
    temperature=0.2,
)


TASK_CHAT_AGENT = AgentProfile(
    name="task_chat_agent",
    description="面向操作员的对话智能体，与异常检测任务共享同一工作区。",
    system_prompt=(
        "你是 DeepEM 的任务对话智能体。\n"
        "请基于共享任务工作区、当前 Case、近期观测和调查时间线回答操作员问题，或记录操作员反馈。"
        "涉及采集状态时优先查询 USRP 设备与任务状态；不要把历史模拟数据当作当前证据。"
        "你也可以自主调用工具获取所需信息。"
        "如果问题涉及上传的文档、二进制文件或历史资料，优先调用 query_uploaded_documents。"
        "如果当前轮直接附带图片，你可以在用户消息中直接查看图片内容。"
        "当用户提出复杂 USRP/频谱采集任务，尤其要求自动生成代码、全频段扫描、多频点、多轮平均、直接使用 WebSocket FFT、保存结果、异常自适应重扫时，优先调用 run_autonomous_usrp_task。"
        "如果用户想查看或分步控制代码生成过程，可以先调用 retrieve_usrp_api_knowledge，再调用 generate_usrp_task_code，最后调用 execute_usrp_task_code。"
        "调用自主采集工具时，task_description 应完整保留用户原始需求；除非用户明确要求演示/模拟，否则 dry_run 设为 false。"
        "不要反复调用同一个工具"
    ),
    allowed_tools=[
        "query_local_database",
        "query_uploaded_documents",
        "query_state",
        "query_cases",
        "query_knowledge",
        "query_recent_observations",
        "scan_usrp_devices",
        "list_usrp_devices",
        "configure_usrp_capture",
        "query_usrp_task",
        "retrieve_usrp_api_knowledge",
        "generate_usrp_task_code",
        "execute_usrp_task_code",
        "run_autonomous_usrp_task",
        "record_operator_feedback",
    ],
    step_budget=8,
    temperature=0.1,
)
