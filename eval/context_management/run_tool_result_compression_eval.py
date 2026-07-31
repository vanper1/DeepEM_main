from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from main.agent.tool_result_compressor import CompressionOptions, ToolResultCompressor
from main.protocol import ToolResult


DEFAULT_DATASET = Path("eval/context_management/tool_result_compression_v1.jsonl")
DEFAULT_OUTPUT = Path("eval/context_management/results/tool_result_compression_eval.jsonl")


@dataclass(slots=True)
class ToolResultCompressionEvalResult:
    case_id: str
    category: str
    difficulty: str
    passed: bool
    raw_chars: int
    compact_chars: int
    compact_data_chars: int
    omitted_fields_chars: int
    envelope_chars: int
    compression_ratio: float
    data_compression_ratio: float
    missing_required_values: list[str]
    missing_required_fields: list[str]
    unexpected_omitted_fields_present: list[str]
    has_result_id: bool
    has_truncated_flag: bool
    retrievable_omissions: bool


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


def evaluate_case(case: dict[str, Any]) -> dict[str, Any]:
    raw_result = dict(case.get("raw_result") or {})
    expected = dict(case.get("expected_compact") or {})
    compact = compact_tool_result(case)

    raw_text = json.dumps(raw_result, ensure_ascii=False, sort_keys=True)
    compact_text = json.dumps(compact, ensure_ascii=False, sort_keys=True)
    raw_chars = len(raw_text)
    compact_chars = len(compact_text)
    compression_ratio = round(compact_chars / raw_chars, 6) if raw_chars else 1.0
    compression = compact.get("compression") if isinstance(compact.get("compression"), dict) else {}
    compact_data_chars = int(compression.get("data_chars") or len(json.dumps(compact.get("data"), ensure_ascii=False, sort_keys=True)))
    omitted_fields_chars = int(
        compression.get("omitted_fields_chars") or len(json.dumps(compact.get("omitted_fields") or [], ensure_ascii=False, sort_keys=True))
    )
    envelope_chars = int(compression.get("envelope_chars") or max(0, compact_chars - compact_data_chars - omitted_fields_chars))
    data_compression_ratio = round(float(compression.get("data_compression_ratio") or (compact_data_chars / raw_chars if raw_chars else 1.0)), 6)

    missing_values = [
        value
        for value in [str(item) for item in expected.get("must_include_values") or []]
        if value not in compact_text
    ]
    missing_fields = [
        field
        for field in [str(item) for item in expected.get("must_keep_fields") or []]
        if not _has_path(compact, field)
    ]
    unexpected_omitted = [
        field
        for field in [str(item) for item in expected.get("must_omit_fields") or []]
        if _field_name_present(compact.get("data"), field)
    ]

    has_result_id = bool(compact.get("tool_result_id"))
    has_truncated_flag = "truncated" in compact
    omitted_fields = compact.get("omitted_fields")
    retrievable_omissions = bool(compact.get("can_retrieve_more")) if omitted_fields else True

    max_chars = int(expected.get("max_compact_chars") or 10**9)
    max_ratio = float(expected.get("max_compression_ratio") or 1.0)
    max_data_ratio = float(expected.get("max_data_compression_ratio") or 0.75)
    requires_result_id = bool(expected.get("requires_result_id", False))
    requires_truncated_flag = bool(expected.get("requires_truncated_flag", False))
    requires_retrievable = bool(expected.get("requires_retrievable_omissions", False))

    passed = (
        not missing_values
        and not missing_fields
        and not unexpected_omitted
        and compact_chars <= max_chars
        and (compression_ratio <= max_ratio or data_compression_ratio <= max_data_ratio)
        and (has_result_id or not requires_result_id)
        and (has_truncated_flag or not requires_truncated_flag)
        and (retrievable_omissions or not requires_retrievable)
    )

    return asdict(
        ToolResultCompressionEvalResult(
            case_id=str(case.get("id")),
            category=str(case.get("category")),
            difficulty=str(case.get("difficulty")),
            passed=passed,
            raw_chars=raw_chars,
            compact_chars=compact_chars,
            compact_data_chars=compact_data_chars,
            omitted_fields_chars=omitted_fields_chars,
            envelope_chars=envelope_chars,
            compression_ratio=compression_ratio,
            data_compression_ratio=data_compression_ratio,
            missing_required_values=missing_values,
            missing_required_fields=missing_fields,
            unexpected_omitted_fields_present=unexpected_omitted,
            has_result_id=has_result_id,
            has_truncated_flag=has_truncated_flag,
            retrievable_omissions=retrievable_omissions,
        )
    )


def run_eval(
    *,
    dataset: Path = DEFAULT_DATASET,
    output: Path | None = DEFAULT_OUTPUT,
    category: str | None = None,
) -> list[dict[str, Any]]:
    cases = load_cases(dataset)
    if category:
        cases = [case for case in cases if case.get("category") == category]
    results = [evaluate_case(case) for case in cases]
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("w", encoding="utf-8") as handle:
            for item in results:
                handle.write(json.dumps(item, ensure_ascii=False) + "\n")
    return results


def compact_tool_result(case: dict[str, Any]) -> dict[str, Any]:
    raw = dict(case.get("raw_result") or {})
    return ToolResultCompressor(CompressionOptions(max_output_chars=int((case.get("expected_compact") or {}).get("max_compact_chars") or 8000))).compress(
        tool_name=str(case.get("tool_name") or ""),
        arguments=dict(case.get("arguments") or {}),
        tool_result=ToolResult(
            status=str(raw.get("status") or "success"),
            data=raw,
            error=str(raw.get("error")) if raw.get("error") is not None else None,
            emitted_event_ids=list(raw.get("emitted_event_ids") or []),
        ),
        tool_call_id=f"tool_result:{case.get('id')}",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run tool-result compression evaluation.")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--category", default=None)
    args = parser.parse_args()

    results = run_eval(dataset=args.dataset, output=args.output, category=args.category)
    total = len(results)
    passed = sum(1 for item in results if item["passed"])
    avg_ratio = round(sum(float(item["compression_ratio"]) for item in results) / total, 4) if total else 0.0
    print(f"cases={total} passed={passed} failed={total - passed} avg_compression_ratio={avg_ratio}")
    by_category: dict[str, list[float]] = {}
    for item in results:
        by_category.setdefault(str(item["category"]), []).append(float(item["compression_ratio"]))
    for category, ratios in sorted(by_category.items()):
        category_ratio = round(sum(ratios) / len(ratios), 4)
        print(f"category={category} avg_compression_ratio={category_ratio}")
    by_category_data: dict[str, list[float]] = {}
    for item in results:
        by_category_data.setdefault(str(item["category"]), []).append(float(item["data_compression_ratio"]))
    for category, ratios in sorted(by_category_data.items()):
        category_ratio = round(sum(ratios) / len(ratios), 4)
        print(f"category={category} avg_data_compression_ratio={category_ratio}")
    if total - passed:
        print("failed_cases:")
        for item in results:
            if not item["passed"]:
                print(
                    f"- {item['case_id']}: missing_values={item['missing_required_values']} "
                    f"missing_fields={item['missing_required_fields']} "
                    f"unexpected_omitted={item['unexpected_omitted_fields_present']}"
                )
    print(f"results={args.output}")
    return 0 if passed == total else 1


def _has_path(value: Any, path: str) -> bool:
    if isinstance(value, dict) and _has_path_exact(value, path):
        return True
    if isinstance(value, dict) and "data" in value:
        return _has_path_exact(value["data"], path)
    return False


def _has_path_exact(value: Any, path: str) -> bool:
    current = value
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
            continue
        return False
    return True


def _field_name_present(value: Any, field_name: str) -> bool:
    target = field_name.split(".")[-1].replace("[]", "")
    if isinstance(value, dict):
        return target in value or any(_field_name_present(item, field_name) for item in value.values())
    if isinstance(value, list):
        return any(_field_name_present(item, field_name) for item in value)
    return False

if __name__ == "__main__":
    raise SystemExit(main())
