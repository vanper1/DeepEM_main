from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from collections import Counter
from copy import deepcopy
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from eval.context_management.runtime_eval_scoring import judge_checkpoint, score_checkpoint
from main.agent.config import LLMSettings
from main.agent.llm import LLMClient, OpenAICompatibleLLMClient


def load_results(path: Path) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    if not path.exists():
        return results
    with path.open("r", encoding="utf-8") as handle:
        for line_no, raw in enumerate(handle, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid result JSONL at {path}:{line_no}: {exc}") from exc
            if isinstance(payload, dict):
                results.append(payload)
    return results


def rescore_results(
    results: list[dict[str, Any]],
    *,
    judge_client: LLMClient | None = None,
) -> list[dict[str, Any]]:
    scored = deepcopy(results)
    for result in scored:
        checkpoint_passes: list[bool] = []
        for checkpoint in result.get("checkpoints") or []:
            judge = checkpoint.get("judge")
            judge_error = checkpoint.get("judge_error")
            if judge_client is not None:
                judge, judge_error = judge_checkpoint(
                    judge_client,
                    question=str(checkpoint.get("question") or ""),
                    gold=dict(checkpoint.get("gold") or {}),
                    answer=str(checkpoint.get("answer") or ""),
                    tool_calls=list(checkpoint.get("tool_calls") or []),
                    evidence=list(checkpoint.get("retrieved_evidence") or []),
                )
            checkpoint["judge"] = judge
            checkpoint["judge_error"] = judge_error
            checkpoint["rule_score"] = score_checkpoint(checkpoint, judge=judge)
            checkpoint_passes.append(bool(checkpoint["rule_score"]["passed"]))
        result["passed"] = bool(checkpoint_passes) and all(checkpoint_passes)
    return scored


def write_reports(results: list[dict[str, Any]], output_dir: Path) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_rows = _checkpoint_rows(results)
    summary = _build_summary(results, checkpoint_rows)
    (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_csv(output_dir / "case_scores.csv", checkpoint_rows)
    summary_rows = [{"scope": "overall", "name": "all", **dict(summary.get("overall") or {})}]
    summary_rows.extend(
        {"scope": "suite", "name": name, **dict(metrics)}
        for name, metrics in dict(summary.get("suites") or {}).items()
    )
    summary_rows.extend(
        {"scope": "category", "name": name, **dict(metrics)}
        for name, metrics in dict(summary.get("categories") or {}).items()
    )
    _write_csv(output_dir / "summary.csv", summary_rows)
    with (output_dir / "failures.jsonl").open("w", encoding="utf-8") as handle:
        for row in checkpoint_rows:
            if not row.get("passed"):
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return summary


def _checkpoint_rows(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for result in results:
        for checkpoint in result.get("checkpoints") or []:
            metrics = dict(checkpoint.get("metrics") or {})
            llm_steps = list(checkpoint.get("llm_steps") or [])
            final_llm_step = llm_steps[-1] if llm_steps else {}
            score = dict(checkpoint.get("rule_score") or checkpoint.get("target_behavior_score") or {})
            measurement_score = dict(checkpoint.get("measurement_score") or {})
            target_behavior_score = dict(checkpoint.get("target_behavior_score") or score)
            policy_score = dict(checkpoint.get("policy_score") or {})
            tool_calls = list(checkpoint.get("tool_calls") or [])
            retrieval_calls = [item for item in tool_calls if item.get("tool_name") == "retrieve_tool_result_detail"]
            retrieval_success = [item for item in retrieval_calls if item.get("found") is True]
            rows.append(
                {
                    "experiment_key": result.get("experiment_key"),
                    "case_id": result.get("case_id"),
                    "turn_id": checkpoint.get("turn_id"),
                    "suite": result.get("suite"),
                    "category": result.get("category"),
                    "execution_mode": result.get("execution_mode"),
                    "requested_chat_mode": checkpoint.get("requested_chat_mode"),
                    "mode_protocol": checkpoint.get("mode_protocol"),
                    "measurement_complete": checkpoint.get("measurement_complete"),
                    "measurement_passed": measurement_score.get("passed"),
                    "target_behavior_passed": target_behavior_score.get("passed"),
                    "policy_evaluation_status": checkpoint.get("policy_evaluation_status") or policy_score.get("status"),
                    "policy_passed": policy_score.get("passed"),
                    "estimated_input_tokens": checkpoint.get("estimated_input_tokens"),
                    "passed": bool(score.get("passed")),
                    "answer_success": bool(str(checkpoint.get("answer") or "").strip())
                    and not checkpoint.get("error"),
                    "required_point_recall": score.get("required_point_recall"),
                    "forbidden_term_hit": bool(score.get("forbidden_hits")),
                    "expected_tool_recall": score.get("expected_tool_recall"),
                    "tool_anchor_recall": score.get("tool_anchor_recall"),
                    "retrieval_calls": len(retrieval_calls),
                    "retrieval_successes": len(retrieval_success),
                    "pseudo_tool_call": bool(score.get("pseudo_tool_call")),
                    "connection_error": bool(checkpoint.get("connection_error")),
                    "empty_assistant_response": bool(checkpoint.get("empty_assistant_response")),
                    "runtime_error": checkpoint.get("error"),
                    "llm_step_count": len(llm_steps),
                    "llm_latency_total_ms": _sum_step_metric(llm_steps, "latency_ms"),
                    "final_finish_reason": final_llm_step.get("finish_reason"),
                    "final_content_chars": final_llm_step.get("content_chars"),
                    "final_raw_content_chars": final_llm_step.get("raw_content_chars"),
                    "reasoning_chars_total": _sum_step_metric(llm_steps, "reasoning_chars"),
                    "reasoning_chars_max": _max_step_metric(llm_steps, "reasoning_chars"),
                    "raw_reasoning_chars_total": _sum_step_metric(llm_steps, "raw_reasoning_chars"),
                    "input_tokens_total": _sum_step_metric(llm_steps, "input_tokens"),
                    "output_tokens_total": _sum_step_metric(llm_steps, "output_tokens"),
                    "reasoning_tokens_total": _sum_step_metric(llm_steps, "reasoning_tokens"),
                    "peak_estimated_tokens": metrics.get("peak_estimated_tokens"),
                    "peak_token_usage_ratio": metrics.get("peak_token_usage_ratio"),
                    "final_token_usage_ratio": metrics.get("final_token_usage_ratio"),
                    "peak_before_reduction_token_usage_ratio": metrics.get("peak_before_reduction_token_usage_ratio"),
                    "peak_after_reduction_token_usage_ratio": metrics.get("peak_after_reduction_token_usage_ratio"),
                    "reducer_stages": ",".join(metrics.get("reducer_stages") or []),
                    "hard_limit_satisfied": metrics.get("hard_limit_satisfied"),
                    "history_units_total": metrics.get("history_units_total", 0),
                    "history_units_kept": metrics.get("history_units_kept", 0),
                    "history_units_summarized": metrics.get("history_units_summarized", 0),
                    "history_units_dropped": metrics.get("history_units_dropped", 0),
                    "tool_pairs_total": metrics.get("tool_pairs_total", 0),
                    "tool_pairs_compacted": metrics.get("tool_pairs_compacted", 0),
                    "tool_pairs_invalid": metrics.get("tool_pairs_invalid", 0),
                    "summary_cache_statuses": ",".join(metrics.get("summary_cache_statuses") or []),
                    "summary_cache_hit_count": metrics.get("summary_cache_hit_count", 0),
                    "summary_incremental_update_count": metrics.get("summary_incremental_update_count", 0),
                    "summary_rebuild_count": metrics.get("summary_rebuild_count", 0),
                    "summary_rejected_count": metrics.get("summary_rejected_count", 0),
                    "over_budget_prompt": bool(metrics.get("over_budget_prompt")),
                    "semantic_attempt_count": metrics.get("semantic_attempt_count", 0),
                    "semantic_success_count": metrics.get("semantic_success_count", 0),
                    "semantic_skipped_count": metrics.get("semantic_skipped_count", 0),
                    "semantic_fallback_reasons": ",".join(metrics.get("semantic_fallback_reasons") or []),
                    "context_compression_triggered": bool(metrics.get("context_compression_triggered")),
                    "summary_compression_ratio": metrics.get("summary_compression_ratio"),
                    "semantic_input_original_chars_max": metrics.get("semantic_input_original_chars_max"),
                    "semantic_input_chars_max": metrics.get("semantic_input_chars_max"),
                    "semantic_input_trimmed": bool(metrics.get("semantic_input_trimmed")),
                    "semantic_latency_ms_total": metrics.get("semantic_latency_ms_total"),
                    "semantic_latency_ms_max": metrics.get("semantic_latency_ms_max"),
                    "semantic_call_latencies_ms": metrics.get("semantic_call_latencies_ms") or [],
                    "aggressive_compression_applied": bool(metrics.get("aggressive_compression_applied")),
                    "tool_transcript_compression_applied": bool(
                        metrics.get("tool_transcript_compression_applied")
                    ),
                    "tool_transcript_chars_before_max": metrics.get("tool_transcript_chars_before_max"),
                    "tool_transcript_chars_after_min": metrics.get("tool_transcript_chars_after_min"),
                    "tool_transcript_compression_ratio": metrics.get("tool_transcript_compression_ratio"),
                    "tool_result_compression_count": metrics.get("tool_result_compression_count", 0),
                    "tool_result_raw_chars_total": metrics.get("tool_result_raw_chars_total"),
                    "tool_result_compact_chars_total": metrics.get("tool_result_compact_chars_total"),
                    "tool_result_compression_ratio": metrics.get("tool_result_compression_ratio"),
                    "compacted_tool_message_count": metrics.get("compacted_tool_message_count", 0),
                    "tool_call_count": metrics.get("tool_call_count", len(tool_calls)),
                    "duplicate_tool_call_count": metrics.get("duplicate_tool_call_count", 0),
                    "duplicate_tool_rate": metrics.get("duplicate_tool_rate", 0.0),
                    "retrieval_success_rate": metrics.get("retrieval_success_rate"),
                    "largest_components": ",".join(metrics.get("largest_components") or []),
                    "latency_ms": checkpoint.get("latency_ms"),
                    "time_to_stream_open_ms": checkpoint.get("time_to_stream_open_ms"),
                    "time_to_first_activity_ms": checkpoint.get("time_to_first_activity_ms"),
                    "time_to_first_visible_token_ms": checkpoint.get("time_to_first_visible_token_ms"),
                    "time_to_final_answer_token_ms": checkpoint.get("time_to_final_answer_token_ms"),
                    "turn_end_to_end_ms": checkpoint.get("turn_end_to_end_ms"),
                    "hard_failures": ",".join(score.get("hard_failures") or []),
                    "answer": checkpoint.get("answer"),
                }
            )
    return rows


def _build_summary(results: list[dict[str, Any]], rows: list[dict[str, Any]]) -> dict[str, Any]:
    suites = {
        suite: _metric_summary([row for row in rows if str(row.get("suite") or "") == suite])
        for suite in sorted({str(row.get("suite") or "") for row in rows})
    }
    categories = {
        category: _metric_summary([row for row in rows if str(row.get("category") or "") == category])
        for category in sorted({str(row.get("category") or "") for row in rows})
    }
    chat_modes = {
        mode: _metric_summary([row for row in rows if str(row.get("requested_chat_mode") or "") == mode])
        for mode in sorted({str(row.get("requested_chat_mode") or "") for row in rows if row.get("requested_chat_mode")})
    }
    return {
        "result_count": len(results),
        "checkpoint_count": len(rows),
        "context_mode": "semantic",
        "judge_enabled": False,
        "overall": _metric_summary(rows),
        "suites": suites,
        "categories": categories,
        "chat_modes": chat_modes,
        "failed_checkpoint_count": sum(1 for row in rows if not row.get("passed")),
    }


def _metric_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    attempts = sum(int(row.get("semantic_attempt_count") or 0) for row in rows)
    successes = sum(int(row.get("semantic_success_count") or 0) for row in rows)
    skips = sum(int(row.get("semantic_skipped_count") or 0) for row in rows)
    cache_hits = sum(int(row.get("summary_cache_hit_count") or 0) for row in rows)
    cache_incremental = sum(int(row.get("summary_incremental_update_count") or 0) for row in rows)
    cache_rebuilds = sum(int(row.get("summary_rebuild_count") or 0) for row in rows)
    cache_rejected = sum(int(row.get("summary_rejected_count") or 0) for row in rows)
    cache_lookups = sum(
        1
        for row in rows
        for status in str(row.get("summary_cache_statuses") or "").split(",")
        if status in {"miss", "hit", "incremental", "rebuild", "rejected"}
    )
    semantic_call_latencies = [
        float(value)
        for row in rows
        for value in (row.get("semantic_call_latencies_ms") or [])
    ]
    retrieval_calls = sum(int(row.get("retrieval_calls") or 0) for row in rows)
    retrieval_successes = sum(int(row.get("retrieval_successes") or 0) for row in rows)
    largest = Counter(
        component
        for row in rows
        for component in str(row.get("largest_components") or "").split(",")
        if component
    )
    reducer_stages = Counter(
        stage
        for row in rows
        for stage in str(row.get("reducer_stages") or "").split(",")
        if stage
    )
    return {
        "checkpoints": len(rows),
        "pass_rate": _rate(sum(bool(row.get("passed")) for row in rows), len(rows)),
        "answer_success_rate": _rate(sum(bool(row.get("answer_success")) for row in rows), len(rows)),
        "empty_answer_rate": _rate(sum(bool(row.get("empty_assistant_response")) for row in rows), len(rows)),
        "avg_required_point_recall": _mean(rows, "required_point_recall"),
        "avg_peak_token_usage_ratio": _mean(rows, "peak_token_usage_ratio"),
        "avg_before_reduction_token_usage_ratio": _mean(rows, "peak_before_reduction_token_usage_ratio"),
        "avg_after_reduction_token_usage_ratio": _mean(rows, "peak_after_reduction_token_usage_ratio"),
        "reducer_stage_distribution": dict(reducer_stages),
        "hard_limit_failure_count": sum(row.get("hard_limit_satisfied") is False for row in rows),
        "avg_history_units_total": _mean(rows, "history_units_total"),
        "avg_history_units_kept": _mean(rows, "history_units_kept"),
        "avg_history_units_summarized": _mean(rows, "history_units_summarized"),
        "avg_history_units_dropped": _mean(rows, "history_units_dropped"),
        "avg_tool_pairs_total": _mean(rows, "tool_pairs_total"),
        "avg_tool_pairs_compacted": _mean(rows, "tool_pairs_compacted"),
        "avg_tool_pairs_invalid": _mean(rows, "tool_pairs_invalid"),
        "context_compression_trigger_rate": _rate(
            sum(bool(row.get("context_compression_triggered")) for row in rows), len(rows)
        ),
        "avg_summary_compression_ratio": _mean(rows, "summary_compression_ratio"),
        "semantic_success_rate": _rate(successes, attempts),
        "semantic_attempts": attempts,
        "semantic_successes": successes,
        "semantic_skips": skips,
        "summary_cache_hit_count": cache_hits,
        "summary_cache_hit_rate": _rate(cache_hits, cache_lookups),
        "summary_incremental_update_count": cache_incremental,
        "summary_rebuild_count": cache_rebuilds,
        "summary_rejected_count": cache_rejected,
        "timeout_rate": _rate(
            sum("timeout" in str(row.get("semantic_fallback_reasons") or "") for row in rows), len(rows)
        ),
        "tool_anchor_recall": _mean(rows, "tool_anchor_recall"),
        "retrieval_success_rate": _rate(retrieval_successes, retrieval_calls),
        "tool_transcript_compression_rate": _rate(
            sum(bool(row.get("tool_transcript_compression_applied")) for row in rows), len(rows)
        ),
        "avg_tool_transcript_compression_ratio": _mean(rows, "tool_transcript_compression_ratio"),
        "avg_tool_result_compression_ratio": _mean(rows, "tool_result_compression_ratio"),
        "avg_duplicate_tool_rate": _mean(rows, "duplicate_tool_rate"),
        "pseudo_tool_call_rate": _rate(sum(bool(row.get("pseudo_tool_call")) for row in rows), len(rows)),
        "connection_error_rate": _rate(sum(bool(row.get("connection_error")) for row in rows), len(rows)),
        "over_budget_prompt_rate": _rate(sum(bool(row.get("over_budget_prompt")) for row in rows), len(rows)),
        "latency_p50_ms": _percentile(rows, "latency_ms", 0.50),
        "latency_p95_ms": _percentile(rows, "latency_ms", 0.95),
        "time_to_first_activity_p50_ms": _percentile(rows, "time_to_first_activity_ms", 0.50),
        "time_to_first_activity_p95_ms": _percentile(rows, "time_to_first_activity_ms", 0.95),
        "time_to_first_visible_token_p50_ms": _percentile(rows, "time_to_first_visible_token_ms", 0.50),
        "time_to_first_visible_token_p95_ms": _percentile(rows, "time_to_first_visible_token_ms", 0.95),
        "time_to_final_answer_token_p50_ms": _percentile(rows, "time_to_final_answer_token_ms", 0.50),
        "time_to_final_answer_token_p95_ms": _percentile(rows, "time_to_final_answer_token_ms", 0.95),
        "semantic_latency_p50_ms": _percentile(rows, "semantic_latency_ms_total", 0.50),
        "semantic_latency_p95_ms": _percentile(rows, "semantic_latency_ms_total", 0.95),
        "semantic_latency_total_per_checkpoint_p50_ms": _percentile(rows, "semantic_latency_ms_total", 0.50),
        "semantic_latency_total_per_checkpoint_p95_ms": _percentile(rows, "semantic_latency_ms_total", 0.95),
        "semantic_call_latency_p50_ms": _percentile_values(semantic_call_latencies, 0.50),
        "semantic_call_latency_p95_ms": _percentile_values(semantic_call_latencies, 0.95),
        "largest_component_distribution": dict(largest),
    }


def _mean(rows: list[dict[str, Any]], field: str) -> float | None:
    values = [float(row[field]) for row in rows if row.get(field) is not None]
    return round(statistics.fmean(values), 6) if values else None


def _sum_step_metric(steps: list[dict[str, Any]], field: str) -> int | None:
    values = [int(item[field]) for item in steps if item.get(field) is not None]
    return sum(values) if values else None


def _max_step_metric(steps: list[dict[str, Any]], field: str) -> int | None:
    values = [int(item[field]) for item in steps if item.get(field) is not None]
    return max(values) if values else None


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


def _percentile(rows: list[dict[str, Any]], field: str, fraction: float) -> float | None:
    values = [float(row[field]) for row in rows if row.get(field) is not None]
    return _percentile_values(values, fraction)


def _percentile_values(values: list[float], fraction: float) -> float | None:
    values = sorted(values)
    if not values:
        return None
    index = max(0, min(len(values) - 1, int(round((len(values) - 1) * fraction))))
    return round(values[index], 3)


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def build_judge_client_from_env() -> OpenAICompatibleLLMClient:
    import os

    required = [
        "DEEPEM_EVAL_JUDGE_LLM_API_KEY",
        "DEEPEM_EVAL_JUDGE_LLM_BASE_URL",
        "DEEPEM_EVAL_JUDGE_LLM_MODEL",
    ]
    missing = [name for name in required if not str(os.getenv(name) or "").strip()]
    if missing:
        raise ValueError(f"Missing independent judge configuration: {', '.join(missing)}")
    return OpenAICompatibleLLMClient(LLMSettings.from_env(prefix="DEEPEM_EVAL_JUDGE_LLM_"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Score DeepEM runtime context evaluation results.")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--judge", action="store_true")
    args = parser.parse_args()
    output_dir = args.output_dir or args.input.parent
    results = load_results(args.input)
    judge_client = build_judge_client_from_env() if args.judge else None
    scored = rescore_results(results, judge_client=judge_client)
    scored_path = output_dir / "scored_results.jsonl"
    output_dir.mkdir(parents=True, exist_ok=True)
    with scored_path.open("w", encoding="utf-8") as handle:
        for item in scored:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")
    summary = write_reports(scored, output_dir)
    print(
        f"results={len(scored)} checkpoints={summary['checkpoint_count']} "
        f"failed={summary['failed_checkpoint_count']} output={output_dir}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
