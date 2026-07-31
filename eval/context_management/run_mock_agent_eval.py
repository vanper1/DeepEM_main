from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from eval.context_management.run_context_eval import (
    DEFAULT_DATASET,
    DEFAULT_RESULTS_DIR,
    assemble_case_messages,
    load_cases,
)
from main.agent.config import LLMSettings
from main.agent.llm import LLMClient, LLMResponse, OpenAICompatibleLLMClient
from main.tools.base import ToolDefinition


DEFAULT_OUTPUT = DEFAULT_RESULTS_DIR / "mock_agent_eval.jsonl"
DEFAULT_LLM_OUTPUT = DEFAULT_RESULTS_DIR / "mock_agent_eval_llm.jsonl"
MOCK_TOOL_NAMES = [
    "scan_usrp_devices",
    "list_usrp_devices",
    "configure_usrp_capture",
    "query_usrp_task",
    "query_cases",
    "query_recent_observations",
    "query_uploaded_documents",
    "query_local_database",
    "query_knowledge",
    "query_state",
]


@dataclass(slots=True)
class MockAgentResult:
    case_id: str
    category: str
    difficulty: str
    passed: bool
    selected_tools: list[str]
    expected_tools: list[str]
    missing_expected_tools: list[str]
    unexpected_tools: list[str]
    forbidden_claim_hits: list[str]
    answer: str


@dataclass(slots=True)
class LLMMockAgentResult:
    case_id: str
    category: str
    difficulty: str
    mode: str
    passed: bool
    selected_tools: list[str]
    expected_tools: list[str]
    missing_expected_tools: list[str]
    unexpected_tools: list[str]
    forbidden_claim_hits: list[str]
    answer: str
    tool_steps: int
    prompt_chars: int
    latency_ms: int
    llm_error: str | None = None


def select_mock_tools(case: dict[str, Any]) -> list[str]:
    category = str(case.get("category") or "")
    message = str(case.get("user_message") or "")
    setup_text = _setup_text(case)
    combined = f"{message}\n{setup_text}".lower()

    tools: list[str] = []
    if category == "usrp_status":
        tools.extend(["scan_usrp_devices", "list_usrp_devices"])
    elif category == "usrp_start":
        tools.append("configure_usrp_capture")
    elif category == "usrp_task_files":
        if any(token in combined for token in ["busy", "完成", "采完", "running", "status"]):
            tools.append("list_usrp_devices")
    elif category == "case_memory":
        tools.append("query_cases")
        if "观测" in message or "2.4g" in message.lower() or "原因" in message:
            tools.append("query_recent_observations")
    elif category == "document_context":
        if "api_docs" in combined or "platform_base_url" in combined or "文档" in message:
            tools.append("query_uploaded_documents")
    elif category == "nl2sql":
        tools.append("query_local_database")
    elif category == "tool_result_summarization":
        if "list_usrp_devices" in combined or "device" in combined or "task_id" in combined:
            tools.append("list_usrp_devices")
        if "nl2sql" in combined or "sql" in combined or "database" in combined:
            tools.append("query_local_database")
    elif category == "anti_hallucination":
        if "采集" in message or "usrp" in combined or "last week" in combined or "上周" in message:
            if "上周" in message or "historical" in combined:
                tools.append("query_cases")
            elif "模型服务" not in message:
                tools.append("list_usrp_devices")
    elif category == "important_state_priority":
        if "启动" in message or "采集" in message:
            tools.append("list_usrp_devices")
        elif "频谱" in message:
            tools.append("query_recent_observations")
    elif category == "long_term_memory":
        tools.extend(["query_knowledge", "query_state"])
        if "之前" in message or "historical" in combined:
            tools.insert(0, "query_cases")
    elif category == "context_budget":
        if "停止" in message:
            tools.append("query_usrp_task")
        elif "任务" in message:
            tools.append("list_usrp_devices")
    return _dedupe(tools)


def evaluate_mock_case(case: dict[str, Any]) -> dict[str, Any]:
    selected_tools = select_mock_tools(case)
    expected_tools = [str(item) for item in case.get("expected_tools") or []]
    missing = [tool for tool in expected_tools if tool not in selected_tools]
    unexpected = [tool for tool in selected_tools if tool not in expected_tools]
    answer = build_mock_answer(case, selected_tools)
    forbidden_hits = [claim for claim in case.get("forbidden_claims") or [] if _claim_hits(str(claim), answer)]
    result = MockAgentResult(
        case_id=str(case.get("id")),
        category=str(case.get("category")),
        difficulty=str(case.get("difficulty")),
        passed=not missing and not forbidden_hits,
        selected_tools=selected_tools,
        expected_tools=expected_tools,
        missing_expected_tools=missing,
        unexpected_tools=unexpected,
        forbidden_claim_hits=forbidden_hits,
        answer=answer,
    )
    return asdict(result)


def evaluate_llm_case(
    case: dict[str, Any],
    *,
    llm_client: LLMClient,
    max_steps: int = 2,
) -> dict[str, Any]:
    start = time.perf_counter()
    expected_tools = [str(item) for item in case.get("expected_tools") or []]
    selected_tools: list[str] = []
    answer = ""
    llm_error: str | None = None
    messages = _build_llm_messages(case)
    tools = _build_mock_tool_definitions()
    tool_steps = 0

    try:
        for _ in range(max(1, max_steps)):
            response = llm_client.complete(
                messages=messages,
                tools=tools,
                temperature=0.1,
                generation_options={"enable_thinking": False, "preserve_thinking": False},
            )
            if response.content:
                answer = response.content
            if not response.tool_calls:
                break

            tool_steps += 1
            messages.append(_assistant_tool_call_message(response))
            for tool_call in response.tool_calls:
                selected_tools.append(tool_call.name)
                messages.append(_mock_tool_message(case, tool_call.id, tool_call.name, tool_call.arguments))
        else:
            if not answer:
                answer = "LLM reached max tool steps before producing a final answer."
    except Exception as exc:  # pragma: no cover - exercised by real integration runs
        llm_error = f"{type(exc).__name__}: {exc}"
        answer = f"LLM 调用失败：{llm_error}"

    selected_tools = _dedupe(selected_tools)
    missing = [tool for tool in expected_tools if tool not in selected_tools]
    unexpected = [tool for tool in selected_tools if tool not in expected_tools]
    forbidden_hits = [claim for claim in case.get("forbidden_claims") or [] if _claim_hits(str(claim), answer)]
    result = LLMMockAgentResult(
        case_id=str(case.get("id")),
        category=str(case.get("category")),
        difficulty=str(case.get("difficulty")),
        mode="llm",
        passed=not missing and not forbidden_hits and llm_error is None,
        selected_tools=selected_tools,
        expected_tools=expected_tools,
        missing_expected_tools=missing,
        unexpected_tools=unexpected,
        forbidden_claim_hits=forbidden_hits,
        answer=answer,
        tool_steps=tool_steps,
        prompt_chars=sum(len(str(item.get("content") or "")) for item in messages),
        latency_ms=int((time.perf_counter() - start) * 1000),
        llm_error=llm_error,
    )
    return asdict(result)


def run_eval(
    *,
    dataset: Path = DEFAULT_DATASET,
    output: Path | None = DEFAULT_OUTPUT,
    category: str | None = None,
    mode: str = "heuristic",
    llm_client: LLMClient | None = None,
    max_steps: int = 2,
) -> list[dict[str, Any]]:
    cases = load_cases(dataset)
    if category:
        cases = [case for case in cases if case.get("category") == category]
    if mode == "llm":
        client = llm_client or _build_llm_client_from_env()
        results = [evaluate_llm_case(case, llm_client=client, max_steps=max_steps) for case in cases]
    else:
        results = [evaluate_mock_case(case) for case in cases]
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("w", encoding="utf-8") as handle:
            for item in results:
                handle.write(json.dumps(item, ensure_ascii=False) + "\n")
    return results


def build_mock_answer(case: dict[str, Any], selected_tools: list[str]) -> str:
    category = str(case.get("category") or "")
    missing_context = [str(item) for item in (case.get("expected_context") or {}).get("must_include") or []]
    tool_note = f"计划调用工具：{', '.join(selected_tools)}。" if selected_tools else "无需调用实时工具，基于当前上下文回答。"
    if category == "anti_hallucination":
        return f"{tool_note} 当前证据不足时不能确认采集完成或比较历史异常，需要先查询状态或历史记录。"
    if category.startswith("usrp") or category in {"context_budget", "important_state_priority"}:
        return f"{tool_note} 需要以设备状态、dev_id、task_id、采集参数和最近错误为准；服务不可达时应明确说明无法确认。"
    if category == "document_context":
        return f"{tool_note} 需要区分文档内容、项目 .env 配置和实际探测结果，不能把三者混为一谈。"
    if category == "nl2sql":
        return f"{tool_note} 需要遵守 NL2SQL 会话配置和表选择限制，只读查询数据库。"
    if category in {"case_memory", "long_term_memory"}:
        return f"{tool_note} 需要使用 Case、场所基线和历史记录，不应编造不存在的异常。"
    return f"{tool_note} 需要保留关键上下文：{'; '.join(missing_context[:3])}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run mock-tool agent evaluation without real USRP access.")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--category", default=None)
    parser.add_argument("--mode", choices=["heuristic", "llm"], default="heuristic")
    parser.add_argument("--max-steps", type=int, default=2)
    args = parser.parse_args()

    output = args.output or (DEFAULT_LLM_OUTPUT if args.mode == "llm" else DEFAULT_OUTPUT)
    results = run_eval(
        dataset=args.dataset,
        output=output,
        category=args.category,
        mode=args.mode,
        max_steps=args.max_steps,
    )
    total = len(results)
    passed = sum(1 for item in results if item["passed"])
    print(f"cases={total} passed={passed} failed={total - passed}")
    if total - passed:
        print("failed_cases:")
        for item in results:
            if not item["passed"]:
                print(
                    f"- {item['case_id']}: missing_tools={item['missing_expected_tools']} "
                    f"forbidden_hits={item['forbidden_claim_hits']} "
                    f"llm_error={item.get('llm_error')}"
                )
    print(f"results={output}")
    return 0 if passed == total else 1


def _build_llm_client_from_env() -> OpenAICompatibleLLMClient:
    _load_project_env()
    return OpenAICompatibleLLMClient(LLMSettings.from_env(prefix="DEEPEM_LLM_"))


def _load_project_env() -> None:
    env_path = PROJECT_ROOT / ".env"
    try:
        from dotenv import load_dotenv
    except Exception:
        _load_env_file_without_dependency(env_path)
        return
    load_dotenv(env_path)


def _load_env_file_without_dependency(path: Path) -> None:
    if not path.exists():
        return
    import os

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _build_llm_messages(case: dict[str, Any]) -> list[dict[str, Any]]:
    eval_system = (
        "你是 DeepEM 上下文管理评测智能体。请根据上下文判断是否需要调用工具。"
        "可以调用的工具均为评测 mock 工具，不会访问真实 USRP 或数据库。"
        "需要实时状态、历史 Case、文档或数据库信息时必须先调用相应工具；"
        "工具返回后再给出简洁、可验证、避免幻觉的中文答案。"
    )
    source_messages = assemble_case_messages(case)
    system_parts = [eval_system]
    normalized: list[dict[str, Any]] = []
    for item in source_messages:
        role = str(item.get("role") or "user")
        content = item.get("content") or ""
        if role == "system":
            system_parts.append(str(content))
            continue
        normalized.append({"role": role if role in {"user", "assistant"} else "user", "content": content})
    return [{"role": "system", "content": "\n\n".join(system_parts)}] + normalized


def _build_mock_tool_definitions() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name=name,
            description=_mock_tool_description(name),
            input_schema={"type": "object", "properties": {}, "additionalProperties": True},
            handler=_unused_tool_handler,
        )
        for name in MOCK_TOOL_NAMES
    ]


def _unused_tool_handler(arguments: dict[str, Any], context: Any) -> Any:
    raise RuntimeError("Context evaluation mock tools are not executed through ToolDefinition handlers.")


def _assistant_tool_call_message(response: LLMResponse) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": response.content or "",
        "tool_calls": [
            {
                "id": item.id,
                "type": "function",
                "function": {"name": item.name, "arguments": json.dumps(item.arguments, ensure_ascii=False)},
            }
            for item in response.tool_calls
        ],
    }


def _mock_tool_message(
    case: dict[str, Any],
    tool_call_id: str,
    tool_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    payload = _mock_tool_payload(case, tool_name, arguments)
    return {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "name": tool_name,
        "content": json.dumps(payload, ensure_ascii=False),
    }


def _mock_tool_payload(case: dict[str, Any], tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    setup_text = _setup_text(case)
    facts = [str(item) for item in (case.get("conversation_setup") or {}).get("state_facts") or []]
    offline = any(token in setup_text.lower() for token in ["offline", "unreachable", "不可达", "关机", "maintenance"])
    base: dict[str, Any] = {
        "mocked": True,
        "tool": tool_name,
        "arguments": arguments,
        "case_id": case.get("id"),
        "relevant_facts": facts[:8],
    }
    if tool_name == "scan_usrp_devices":
        base.update({"status": "unreachable" if offline else "ok", "devices": [] if offline else ["usrp-30B1FDE"]})
    elif tool_name == "list_usrp_devices":
        state = "BUSY" if "busy" in setup_text.lower() else "IDLE"
        base.update({"devices": [{"dev_id": "usrp-30B1FDE", "state": state}], "source": "mock_state_facts"})
    elif tool_name == "configure_usrp_capture":
        conflict = "409" in setup_text or "busy" in setup_text.lower()
        base.update({"accepted": not conflict, "error": "HTTP 409 device busy" if conflict else None, "task_id": "mock-usrp-task-001"})
    elif tool_name == "query_usrp_task":
        base.update({"task": {"task_id": "mock-usrp-task-001", "status": "running" if "running" in setup_text.lower() else "unknown"}})
    elif tool_name == "query_cases":
        base.update({"cases": [fact for fact in facts if "case" in fact.lower() or "sig-" in fact.lower()]})
    elif tool_name == "query_recent_observations":
        base.update({"observations": facts[-6:]})
    elif tool_name == "query_uploaded_documents":
        base.update({"documents": [fact for fact in facts if "api" in fact.lower() or "doc" in fact.lower() or "url" in fact.lower()]})
    elif tool_name == "query_local_database":
        base.update({"sql": "SELECT ... -- mock read-only query", "rows": facts[:3]})
    elif tool_name in {"query_knowledge", "query_state"}:
        base.update({"items": facts[:8]})
    return base


def _mock_tool_description(name: str) -> str:
    descriptions = {
        "scan_usrp_devices": "Mock scan of USRP service reachability and discovered devices.",
        "list_usrp_devices": "Mock list of known USRP devices, states, dev_id, and busy/idle status.",
        "configure_usrp_capture": "Mock configuration/start of a USRP capture task.",
        "query_usrp_task": "Mock lookup for a USRP task status or output files.",
        "query_cases": "Mock query of historical DeepEM cases and signal records.",
        "query_recent_observations": "Mock query of recent observations, state facts, and spectrum notes.",
        "query_uploaded_documents": "Mock query of uploaded project/API documents.",
        "query_local_database": "Mock read-only NL2SQL/local database query.",
        "query_knowledge": "Mock query of long-term knowledge and place baselines.",
        "query_state": "Mock query of current platform state snapshot.",
    }
    return descriptions.get(name, f"Mock tool for {name}.")


def _setup_text(case: dict[str, Any]) -> str:
    setup = dict(case.get("conversation_setup") or {})
    state_facts = [str(item) for item in setup.get("state_facts") or []]
    prior_messages = [str((item or {}).get("content") or "") for item in setup.get("prior_messages") or []]
    return "\n".join(state_facts + prior_messages)


def _dedupe(items: list[str]) -> list[str]:
    result: list[str] = []
    for item in items:
        if item not in result:
            result.append(item)
    return result


def _claim_hits(claim: str, answer: str) -> bool:
    claim_lower = claim.lower()
    answer_lower = answer.lower()
    if claim_lower in answer_lower:
        return True
    # The dataset phrases forbidden claims as instructions. Avoid treating the
    # shared words "do not" as evidence that the forbidden claim appeared.
    cleaned = (
        claim_lower.replace("do not", "")
        .replace("don't", "")
        .replace("unless", "")
        .replace("without", "")
        .strip(" .")
    )
    if len(cleaned) < 8:
        return False
    return cleaned in answer_lower


if __name__ == "__main__":
    raise SystemExit(main())
