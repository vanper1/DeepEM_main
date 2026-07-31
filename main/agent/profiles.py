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
        "当工具返回的 compact result 中 can_retrieve_more 为 true，且你需要 omitted_fields 中的完整代码、日志、文档片段、SQL 行或嵌套执行详情时，调用 retrieve_tool_result_detail，使用 tool_result_id 和对应 path 获取原始字段。"
        "当 retrieve_tool_result_detail 返回 terminal=true 时，请直接使用 data.value 回答，不得再次回捞同一 tool_result_id 和 path。"
        "不要因为 can_retrieve_more 为 true 自动继续回捞；如果当前片段或回捞内容已经足以回答用户问题，应停止调用工具并直接回答。"
        "当用户提出复杂 USRP/频谱采集任务，尤其要求自动生成代码、全频段扫描、多频点、多轮平均、直接使用 WebSocket FFT、保存结果、异常自适应重扫时，优先调用 run_autonomous_usrp_task。"
        "如果用户想查看或分步控制代码生成过程，可以先调用 retrieve_usrp_api_knowledge，再调用 generate_usrp_task_code，最后调用 execute_usrp_task_code。"
        "调用自主采集工具时，task_description 应完整保留用户原始需求；除非用户明确要求演示/模拟，否则 dry_run 设为 false。"
        "不要反复调用同一个工具"
    ),
    allowed_tools=[
        "query_local_database",
        "query_uploaded_documents",
        "retrieve_tool_result_detail",
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
    step_budget=36,
    temperature=0.1,
)


GENERAL_QA_AGENT = AgentProfile(
    name="general_qa_agent",
    description="不访问工作区数据和工具的通用问答智能体。",
    system_prompt=(
        "你是 DeepEM 的通用问答助手。请使用通用知识直接、准确地回答问题。\n"
        "你无法访问当前工作区、实时设备、Case、告警、近期采集、数据库或上传文档。"
        "当用户询问这些现场状态或要求读取非图片附件时，明确说明无法在通用问答模式确认，并提示切换到工作区模式。"
        "不得猜测或编造当前现场状态。当前轮直接附带的图片可以查看。"
    ),
    allowed_tools=[],
    step_budget=2,
    temperature=0.3,
)
