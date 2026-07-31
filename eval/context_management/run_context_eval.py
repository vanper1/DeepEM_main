from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from datetime import timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from main.agent.profiles import TASK_CHAT_AGENT
from main.agent.prompt_builder import PromptBuilder
from main.nl2sql_config import NL2SQLSessionConfig
from main.protocol import (
    CaseRecord,
    CaseStatus,
    ChatMessage,
    ChatRole,
    Event,
    EventType,
    Part,
    PartKind,
    Run,
    RunStatus,
    RunTriggerKind,
    StateSnapshot,
    Task,
    TaskStatus,
    TaskType,
    utc_now,
)
from main.state.memory import InMemoryKnowledgeBase


DEFAULT_DATASET = Path("eval/context_management/context_eval_v1.jsonl")
DEFAULT_RESULTS_DIR = Path("eval/context_management/results")


@dataclass(slots=True)
class OfflineEvalResult:
    case_id: str
    category: str
    difficulty: str
    passed: bool
    prompt_chars: int
    estimated_prompt_tokens: int
    missing_context: list[str]
    matched_context: list[str]
    prompt_path: str | None = None


def load_cases(path: Path = DEFAULT_DATASET) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_no, raw in enumerate(handle, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                cases.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSONL at {path}:{line_no}: {exc}") from exc
    return cases


def assemble_case_messages(case: dict[str, Any]) -> list[dict[str, Any]]:
    now = utc_now()
    task_id = f"task_{case.get('id', 'offline')}"
    conversation_id = f"conv_{case.get('id', 'offline')}"
    run_id = f"run_{case.get('id', 'offline')}"
    setup = dict(case.get("conversation_setup") or {})
    state_facts = [str(item) for item in setup.get("state_facts") or []]
    prior_messages = list(setup.get("prior_messages") or [])

    task = Task(
        id=task_id,
        task_type=TaskType.PLACE_DETECTION,
        target={"place_id": "offline-lab", "eval_case_id": case.get("id")},
        input={"eval_case_id": case.get("id")},
        status=TaskStatus.RUNNING,
        created_by="context_eval",
        created_at=now,
        updated_at=now,
    )
    trigger_message = ChatMessage(
        id=f"msg_trigger_{case.get('id', 'offline')}",
        conversation_id=conversation_id,
        task_id=task_id,
        role=ChatRole.OPERATOR,
        content=str(case.get("user_message") or ""),
        run_id=None,
        created_at=now,
    )
    recent_messages = _build_recent_messages(prior_messages, trigger_message, task_id, conversation_id)
    state = _build_state(task_id=task_id, state_facts=state_facts, case=case)
    cases = _build_cases(task_id=task_id, conversation_id=conversation_id, state_facts=state_facts)
    recent_events = _build_recent_events(task_id=task_id, conversation_id=conversation_id, state_facts=state_facts)
    recent_parts = _build_recent_parts(task_id=task_id, run_id=run_id, conversation_id=conversation_id, case=case, state_facts=state_facts)
    knowledge_base = _build_knowledge_base(state_facts)
    run = Run(
        id=run_id,
        task_id=task_id,
        trigger_kind=RunTriggerKind.CHAT,
        trigger_event_id=None,
        trigger_message_id=trigger_message.id,
        agent_profile=TASK_CHAT_AGENT.name,
        status=RunStatus.RUNNING,
        step_budget=TASK_CHAT_AGENT.step_budget,
        step_count=0,
        started_at=now,
        conversation_id=conversation_id,
    )

    return PromptBuilder().build(
        profile=TASK_CHAT_AGENT,
        task=task,
        run=run,
        trigger_event=None,
        trigger_message=trigger_message,
        state=state,
        cases=cases,
        knowledge_base=knowledge_base,
        recent_events=recent_events,
        recent_parts=recent_parts,
        recent_messages=recent_messages,
        nl2sql_options=_build_nl2sql_options(state_facts),
    )


def assemble_case_prompt(case: dict[str, Any]) -> str:
    messages = assemble_case_messages(case)
    return "\n\n".join(f"{item.get('role', 'unknown').upper()}:\n{item.get('content', '')}" for item in messages)


def evaluate_case(case: dict[str, Any], *, prompt_dir: Path | None = None) -> dict[str, Any]:
    prompt = assemble_case_prompt(case)
    must_include = [str(item) for item in (case.get("expected_context") or {}).get("must_include") or []]
    matched = [item for item in must_include if _context_requirement_matches(item, prompt)]
    missing = [item for item in must_include if item not in matched]
    prompt_path = None
    if prompt_dir is not None:
        prompt_dir.mkdir(parents=True, exist_ok=True)
        prompt_file = prompt_dir / f"{case['id']}.txt"
        prompt_file.write_text(prompt, encoding="utf-8")
        prompt_path = str(prompt_file)
    result = OfflineEvalResult(
        case_id=str(case.get("id")),
        category=str(case.get("category")),
        difficulty=str(case.get("difficulty")),
        passed=not missing,
        prompt_chars=len(prompt),
        estimated_prompt_tokens=_estimate_tokens(prompt),
        missing_context=missing,
        matched_context=matched,
        prompt_path=prompt_path,
    )
    return asdict(result)


def run_eval(
    *,
    dataset: Path = DEFAULT_DATASET,
    output: Path | None = None,
    write_prompts: bool = False,
    category: str | None = None,
) -> list[dict[str, Any]]:
    cases = load_cases(dataset)
    if category:
        cases = [case for case in cases if case.get("category") == category]
    result_dir = output.parent if output else DEFAULT_RESULTS_DIR
    prompt_dir = result_dir / "prompts" if write_prompts else None
    results = [evaluate_case(case, prompt_dir=prompt_dir) for case in cases]
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("w", encoding="utf-8") as handle:
            for item in results:
                handle.write(json.dumps(item, ensure_ascii=False) + "\n")
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Run offline context assembly evaluation.")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output", type=Path, default=DEFAULT_RESULTS_DIR / "offline_context_eval.jsonl")
    parser.add_argument("--category", default=None)
    parser.add_argument("--write-prompts", action="store_true")
    args = parser.parse_args()

    results = run_eval(dataset=args.dataset, output=args.output, write_prompts=args.write_prompts, category=args.category)
    total = len(results)
    passed = sum(1 for item in results if item["passed"])
    avg_chars = round(sum(int(item["prompt_chars"]) for item in results) / total, 1) if total else 0
    avg_tokens = round(sum(int(item["estimated_prompt_tokens"]) for item in results) / total, 1) if total else 0
    print(f"cases={total} passed={passed} failed={total - passed} avg_prompt_chars={avg_chars} avg_est_tokens={avg_tokens}")
    if total - passed:
        print("failed_cases:")
        for item in results:
            if not item["passed"]:
                print(f"- {item['case_id']}: missing {item['missing_context']}")
    print(f"results={args.output}")
    return 0 if passed == total else 1


def _build_recent_messages(
    prior_messages: list[Any],
    trigger_message: ChatMessage,
    task_id: str,
    conversation_id: str,
) -> list[ChatMessage]:
    now = utc_now()
    role_map = {
        "assistant": ChatRole.ASSISTANT,
        "system": ChatRole.SYSTEM,
        "user": ChatRole.OPERATOR,
        "operator": ChatRole.OPERATOR,
    }
    messages: list[ChatMessage] = []
    for index, item in enumerate(prior_messages[-7:]):
        payload = dict(item or {})
        role = role_map.get(str(payload.get("role") or "user").lower(), ChatRole.OPERATOR)
        messages.append(
            ChatMessage(
                id=f"msg_prior_{index}",
                conversation_id=conversation_id,
                task_id=task_id,
                role=role,
                content=str(payload.get("content") or ""),
                run_id=None,
                created_at=now,
            )
        )
    messages.append(trigger_message)
    return messages[-8:]


def _build_state(*, task_id: str, state_facts: list[str], case: dict[str, Any]) -> StateSnapshot:
    metadata: dict[str, Any] = {
        "context_eval": {
            "case_id": case.get("id"),
            "category": case.get("category"),
            "difficulty": case.get("difficulty"),
            "state_facts": state_facts,
        }
    }
    for fact in state_facts:
        _merge_fact_into_metadata(metadata, fact)
    return StateSnapshot(task_id=task_id, place_id="offline-lab", metadata=metadata)


def _merge_fact_into_metadata(metadata: dict[str, Any], fact: str) -> None:
    text = fact.strip()
    lower = text.lower()
    collector = metadata.setdefault("collector", {})
    if "base url" in lower or "base_url" in lower:
        collector.setdefault("config_notes", []).append(text)
    if "active_session" in lower or "active session" in lower or "usrp_task_id" in lower:
        collector.setdefault("active_session_notes", []).append(text)
    if "downloaded_npz_files" in lower or ".npz" in lower:
        collector.setdefault("download_notes", []).append(text)
    if "configure error" in lower or "last_configure_error" in lower or "http 409" in lower:
        collector["last_configure_error"] = text
    if "conversation_summary" in lower:
        metadata["conversation_summary"] = text
    if "baseline" in lower:
        metadata.setdefault("place_baseline", {"initialized": True, "fingerprints": []})
        metadata["place_baseline"].setdefault("notes", []).append(text)
    if "peak_" in lower or "classification" in lower:
        metadata.setdefault("signal_summary", []).append(text)


def _build_cases(task_id: str, conversation_id: str, state_facts: list[str]) -> list[CaseRecord]:
    cases: list[CaseRecord] = []
    for index, fact in enumerate(state_facts):
        lower = fact.lower()
        if "case" not in lower and "sig-" not in lower:
            continue
        risk = "high" if "high" in lower or "高" in fact else "medium"
        status = CaseStatus.OPEN if "open" in lower or "异常" in fact or "case" in lower else CaseStatus.INVESTIGATING
        signal_id = _first_token_with_prefix(fact, "sig-") or f"sig-eval-{index + 1:03d}"
        cases.append(
            CaseRecord(
                id=_first_token_with_prefix(fact, "case-") or f"case-eval-{index + 1:03d}",
                task_id=task_id,
                signal_id=signal_id,
                status=status,
                risk_level=risk,
                hypothesis=fact,
                notes=[fact],
                conversation_id=conversation_id,
            )
        )
    return cases[:6]


def _build_recent_events(task_id: str, conversation_id: str, state_facts: list[str]) -> list[Event]:
    events: list[Event] = []
    now = utc_now().astimezone(timezone.utc)
    for index, fact in enumerate(state_facts[-8:], start=1):
        events.append(
            Event(
                id=f"evt_eval_{index}",
                task_id=task_id,
                seq=index,
                event_type=EventType.STRUCTURED_SIGNAL_DETECTED,
                source="context_eval",
                payload={"summary": fact},
                occurred_at=now,
                recorded_at=now,
                conversation_id=conversation_id,
            )
        )
    return events


def _build_recent_parts(
    *,
    task_id: str,
    run_id: str,
    conversation_id: str,
    case: dict[str, Any],
    state_facts: list[str],
) -> list[Part]:
    parts: list[Part] = []
    now = utc_now()
    expectations = case.get("expected_context") or {}
    for index, fact in enumerate((state_facts + list(expectations.get("nice_to_include") or []))[-8:]):
        parts.append(
            Part(
                id=f"part_eval_{index}",
                task_id=task_id,
                run_id=run_id,
                kind=PartKind.OBSERVATION,
                content=str(fact),
                created_at=now,
                conversation_id=conversation_id,
            )
        )
    return parts


def _build_knowledge_base(state_facts: list[str]) -> InMemoryKnowledgeBase:
    baselines: dict[str, list[str]] = {}
    for fact in state_facts:
        lower = fact.lower()
        if "baseline" not in lower:
            continue
        fingerprints = [token.strip(".,;:") for token in fact.split() if "-" in token and not token.startswith("http")]
        if fingerprints:
            baselines["offline-lab"] = fingerprints[:8]
    return InMemoryKnowledgeBase(place_baselines=baselines)


def _build_nl2sql_options(state_facts: list[str]) -> NL2SQLSessionConfig:
    joined = "\n".join(state_facts).lower()
    return NL2SQLSessionConfig(
        force_enabled="force_enabled=true" in joined,
        auto_select_tables="auto_select_tables=false" not in joined,
        manual_selected_tables=_extract_manual_tables(state_facts),
    )


def _extract_manual_tables(state_facts: list[str]) -> list[str]:
    tables: list[str] = []
    for fact in state_facts:
        lower = fact.lower()
        if "manual selected tables" not in lower:
            continue
        _, _, tail = fact.partition("are")
        if not tail:
            _, _, tail = fact.partition(":")
        for item in tail.replace("and", ",").split(","):
            table = item.strip(" .")
            if table:
                tables.append(table)
    return tables


def _first_token_with_prefix(text: str, prefix: str) -> str | None:
    for token in text.replace(",", " ").replace(";", " ").split():
        cleaned = token.strip(" .:，。；")
        if cleaned.startswith(prefix):
            return cleaned
    return None


def _estimate_tokens(text: str) -> int:
    # Conservative mixed Chinese/English approximation for comparing variants.
    ascii_count = sum(1 for char in text if ord(char) < 128)
    non_ascii_count = len(text) - ascii_count
    return int(ascii_count / 4 + non_ascii_count / 1.7)


def _context_requirement_matches(requirement: str, prompt: str) -> bool:
    requirement_lower = requirement.lower()
    prompt_lower = prompt.lower()
    if requirement_lower in prompt_lower:
        return True
    tokens = _meaningful_tokens(requirement_lower)
    if not tokens:
        return False
    hits = sum(1 for token in tokens if token in prompt_lower)
    if len(tokens) <= 2:
        return hits == len(tokens)
    return hits >= max(2, int(len(tokens) * 0.6))


def _meaningful_tokens(text: str) -> list[str]:
    stopwords = {
        "a",
        "an",
        "and",
        "are",
        "as",
        "be",
        "by",
        "current",
        "from",
        "has",
        "if",
        "in",
        "is",
        "it",
        "latest",
        "must",
        "of",
        "or",
        "should",
        "the",
        "to",
        "with",
    }
    raw = re.findall(r"https?://[^\s,.;]+|ws://[^\s,.;]+|[a-zA-Z0-9_.:/-]+|[\u4e00-\u9fff]{2,}", text)
    result: list[str] = []
    for token in raw:
        cleaned = token.strip(".,;:，。；：").lower()
        if len(cleaned) < 2 or cleaned in stopwords:
            continue
        if cleaned not in result:
            result.append(cleaned)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
