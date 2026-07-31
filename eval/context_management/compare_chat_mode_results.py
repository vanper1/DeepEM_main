from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path
from typing import Any

from eval.context_management.score_runtime_eval import load_results


def nearest_rank_percentile(values: list[float] | list[int], fraction: float) -> float | None:
    if not 0 < fraction <= 1:
        raise ValueError("fraction must be within (0, 1]")
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    rank = max(1, math.ceil(fraction * len(ordered)))
    return round(ordered[rank - 1], 6)


def compare_result_sets(baseline_results: list[dict[str, Any]], candidate_results: list[dict[str, Any]]) -> dict[str, Any]:
    baseline = _checkpoint_index(baseline_results)
    candidate = _checkpoint_index(candidate_results)
    common_keys = sorted(baseline.keys() & candidate.keys())
    rows = [_comparison_row(key, baseline[key], candidate[key]) for key in common_keys]
    unmatched = [
        *({"comparison_key": key, "side": "baseline"} for key in sorted(baseline.keys() - candidate.keys())),
        *({"comparison_key": key, "side": "candidate"} for key in sorted(candidate.keys() - baseline.keys())),
    ]
    general_rows = [row for row in rows if row["requested_chat_mode"] == "general"]
    metadata_mismatches = [
        row["comparison_key"]
        for row in rows
        if not row["metadata_matches"]
    ]
    protocols_valid = _protocols_valid(baseline_results, candidate_results)
    suite_complete = _suite_complete(baseline)
    mixed_rows = [row for row in rows if row["category"] == "mixed_mode"]
    misuse_rows = [row for row in rows if row["category"] == "onsite_misuse"]
    general_content_rows = [
        row for row in rows if row["category"] in {"general_knowledge", "general_followup"}
    ]
    workspace_rows = [row for row in rows if row["category"] == "workspace_regression"]
    baseline_tokens = [row["baseline_estimated_input_tokens"] for row in general_rows if row["baseline_estimated_input_tokens"] is not None]
    candidate_tokens = [row["candidate_estimated_input_tokens"] for row in general_rows if row["candidate_estimated_input_tokens"] is not None]
    baseline_token_median = nearest_rank_percentile(baseline_tokens, 0.5)
    candidate_token_median = nearest_rank_percentile(candidate_tokens, 0.5)
    token_reduction = None
    if baseline_token_median not in (None, 0) and candidate_token_median is not None:
        token_reduction = round(1 - candidate_token_median / baseline_token_median, 6)
    summary = {
        "matched_checkpoint_count": len(rows),
        "unmatched_checkpoint_count": len(unmatched),
        "metadata_mismatch_count": len(metadata_mismatches),
        "protocols_valid": protocols_valid,
        "suite_complete": suite_complete,
        "runner_pass_rate": _paired_rate(rows, "run_passed"),
        "measurement_pass_rate": _paired_rate(rows, "measurement_passed"),
        "target_behavior_pass_rate": _paired_rate(rows, "target_behavior_passed"),
        "candidate_policy_pass_rate": _single_rate(rows, "candidate_policy_passed"),
        "candidate_general_policy_pass_rate": _single_rate(general_rows, "candidate_policy_passed"),
        "general_tool_call_rate": _paired_positive_rate(general_rows, "tool_call_count"),
        "onsite_misuse_target_pass_rate": _paired_rate(misuse_rows, "target_behavior_passed"),
        "mixed_mode_leakage_rate": _paired_rate(mixed_rows, "workspace_leakage"),
        "general_content_pass_rate": _paired_rate(general_content_rows, "target_behavior_passed"),
        "workspace_regression_pass_rate": _paired_rate(workspace_rows, "target_behavior_passed"),
        "general_estimated_input_tokens": _paired_metric_summary(general_rows, "estimated_input_tokens"),
        "general_time_to_first_visible_token_ms": _paired_metric_summary(
            general_rows, "time_to_first_visible_token_ms"
        ),
        "general_turn_end_to_end_ms": _paired_metric_summary(general_rows, "turn_end_to_end_ms"),
        "general_estimated_input_token_median_reduction": token_reduction,
    }
    acceptance_gates = evaluate_acceptance_gates(summary)
    return {"summary": summary, "acceptance_gates": acceptance_gates, "rows": rows, "unmatched": unmatched}


def evaluate_acceptance_gates(summary: dict[str, Any]) -> dict[str, Any]:
    workspace_rates = summary.get("workspace_regression_pass_rate") or {}
    items = [
        _gate("protocols_valid", summary.get("protocols_valid") is True),
        _gate("suite_complete", summary.get("suite_complete") is True),
        _gate("complete_alignment", summary.get("unmatched_checkpoint_count") == 0),
        _gate("metadata_matches", summary.get("metadata_mismatch_count") == 0),
        _gate(
            "runner_complete",
            (summary.get("runner_pass_rate") or {}).get("baseline") == 1.0
            and (summary.get("runner_pass_rate") or {}).get("candidate") == 1.0,
        ),
        _gate(
            "measurement_complete",
            (summary.get("measurement_pass_rate") or {}).get("baseline") == 1.0
            and (summary.get("measurement_pass_rate") or {}).get("candidate") == 1.0,
        ),
        _gate("all_candidate_policy", summary.get("candidate_policy_pass_rate") == 1.0),
        _gate("general_policy", summary.get("candidate_general_policy_pass_rate") == 1.0),
        _gate("general_zero_tool_calls", (summary.get("general_tool_call_rate") or {}).get("candidate") == 0.0),
        _gate("onsite_misuse", (summary.get("onsite_misuse_target_pass_rate") or {}).get("candidate") == 1.0),
        _gate("mixed_mode_no_leakage", (summary.get("mixed_mode_leakage_rate") or {}).get("candidate") == 0.0),
        _gate("general_content", _at_least((summary.get("general_content_pass_rate") or {}).get("candidate"), 0.9)),
        _gate(
            "workspace_regression",
            workspace_rates.get("candidate") == 1.0
            and _at_least(workspace_rates.get("candidate"), workspace_rates.get("baseline")),
        ),
        _gate(
            "estimated_input_token_reduction",
            _at_least(summary.get("general_estimated_input_token_median_reduction"), 0.8),
        ),
        _gate(
            "performance_metrics_complete",
            all(
                _metric_complete(summary.get(name), side)
                for name in (
                    "general_estimated_input_tokens",
                    "general_time_to_first_visible_token_ms",
                    "general_turn_end_to_end_ms",
                )
                for side in ("baseline", "candidate")
            ),
        ),
        _gate(
            "first_visible_token_p95",
            _below(
                ((summary.get("general_time_to_first_visible_token_ms") or {}).get("candidate") or {}).get("p95"),
                10_000,
            ),
        ),
        _gate(
            "turn_end_to_end_p95",
            _below(
                ((summary.get("general_turn_end_to_end_ms") or {}).get("candidate") or {}).get("p95"),
                20_000,
            ),
        ),
    ]
    return {"passed": all(item["passed"] for item in items), "items": items}


def write_comparison(comparison: dict[str, Any], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "comparison.json").write_text(
        json.dumps(comparison, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _write_csv(output_dir / "comparison.csv", list(comparison.get("rows") or []))
    with (output_dir / "unmatched_rows.jsonl").open("w", encoding="utf-8") as handle:
        for item in comparison.get("unmatched") or []:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")
    summary = comparison.get("summary") or {}
    report = [
        "# Chat Mode Cross-Version Comparison",
        "",
        f"- Matched checkpoints: {summary.get('matched_checkpoint_count', 0)}",
        f"- Unmatched checkpoints: {summary.get('unmatched_checkpoint_count', 0)}",
        f"- General estimated-input median reduction: {summary.get('general_estimated_input_token_median_reduction')}",
        "",
        "## Summary",
        "",
        "```json",
        json.dumps(summary, ensure_ascii=False, indent=2),
        "```",
        "",
        "## Acceptance Gates",
        "",
        "```json",
        json.dumps(comparison.get("acceptance_gates") or {}, ensure_ascii=False, indent=2),
        "```",
        "",
    ]
    (output_dir / "comparison_report.md").write_text("\n".join(report), encoding="utf-8")


def _checkpoint_index(results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for result in results:
        case_id = str(result.get("case_id") or "")
        repeat_index = int(result.get("repeat_index") or 0)
        category = str(result.get("category") or "")
        for checkpoint in result.get("checkpoints") or []:
            turn_id = str(checkpoint.get("turn_id") or "")
            key = f"{case_id}:{turn_id}:{repeat_index}"
            if key in indexed:
                raise ValueError(f"Duplicate comparison key: {key}")
            score = checkpoint.get("target_behavior_score")
            score = score if isinstance(score, dict) else checkpoint.get("rule_score") or {}
            indexed[key] = {
                "case_id": case_id,
                "turn_id": turn_id,
                "repeat_index": repeat_index,
                "category": category,
                "mode_protocol": result.get("mode_protocol"),
                "dataset_version": result.get("dataset_version"),
                "dataset_hash": result.get("dataset_hash"),
                "run_passed": bool(result.get("passed")),
                "requested_chat_mode": checkpoint.get("requested_chat_mode"),
                "estimated_input_tokens": checkpoint.get("estimated_input_tokens"),
                "time_to_first_visible_token_ms": checkpoint.get("time_to_first_visible_token_ms"),
                "turn_end_to_end_ms": checkpoint.get("turn_end_to_end_ms"),
                "tool_call_count": len(checkpoint.get("tool_calls") or []),
                "measurement_passed": bool((checkpoint.get("measurement_score") or {}).get("passed")),
                "target_behavior_passed": bool(score.get("passed")),
                "workspace_leakage": _workspace_projection_leakage(checkpoint, score),
                "policy_passed": (checkpoint.get("policy_score") or {}).get("passed"),
            }
    return indexed


def _workspace_projection_leakage(checkpoint: dict[str, Any], score: dict[str, Any]) -> bool:
    """Detect general prompts that claim or show forbidden workspace projections."""
    if bool(score.get("forbidden_hits")):
        return True
    if str(checkpoint.get("requested_chat_mode") or "") != "general":
        return False
    policy = checkpoint.get("observed_execution_policy")
    if isinstance(policy, dict) and any(
        policy.get(field) is True
        for field in ("workspace_snapshot_injected", "tool_transcript_included", "retrieval_context_included")
    ):
        return True
    for step in checkpoint.get("prompt_steps") or []:
        composition = step.get("prompt_composition") if isinstance(step, dict) else None
        for component in (composition or {}).get("components") or []:
            if component.get("name") == "transcript_tool_messages" and int(component.get("estimated_tokens") or 0) > 0:
                return True
    return False


def _comparison_row(key: str, baseline: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    row = {
        "comparison_key": key,
        "case_id": baseline["case_id"],
        "turn_id": baseline["turn_id"],
        "repeat_index": baseline["repeat_index"],
        "category": baseline["category"],
        "requested_chat_mode": baseline["requested_chat_mode"],
        "metadata_matches": all(
            baseline.get(field) == candidate.get(field)
            for field in (
                "case_id",
                "turn_id",
                "repeat_index",
                "category",
                "requested_chat_mode",
                "dataset_version",
                "dataset_hash",
            )
        ),
    }
    for side, item in (("baseline", baseline), ("candidate", candidate)):
        for field in (
            "estimated_input_tokens",
            "time_to_first_visible_token_ms",
            "turn_end_to_end_ms",
            "tool_call_count",
            "run_passed",
            "measurement_passed",
            "target_behavior_passed",
            "workspace_leakage",
            "policy_passed",
        ):
            row[f"{side}_{field}"] = item.get(field)
    return row


def _paired_rate(rows: list[dict[str, Any]], field: str) -> dict[str, float | None]:
    return {
        side: _rate(sum(bool(row.get(f"{side}_{field}")) for row in rows), len(rows))
        for side in ("baseline", "candidate")
    }


def _paired_positive_rate(rows: list[dict[str, Any]], field: str) -> dict[str, float | None]:
    return {
        side: _rate(sum(int(row.get(f"{side}_{field}") or 0) > 0 for row in rows), len(rows))
        for side in ("baseline", "candidate")
    }


def _single_rate(rows: list[dict[str, Any]], field: str) -> float | None:
    return _rate(sum(bool(row.get(field)) for row in rows), len(rows))


def _gate(name: str, passed: bool) -> dict[str, Any]:
    return {"name": name, "passed": bool(passed)}


def _at_least(value: Any, threshold: Any) -> bool:
    return value is not None and threshold is not None and float(value) >= float(threshold)


def _below(value: Any, threshold: float) -> bool:
    return value is not None and float(value) < threshold


def _paired_metric_summary(rows: list[dict[str, Any]], field: str) -> dict[str, dict[str, float | None]]:
    result: dict[str, dict[str, float | None]] = {}
    for side in ("baseline", "candidate"):
        values = [
            float(row[f"{side}_{field}"])
            for row in rows
            if row.get(f"{side}_{field}") is not None
        ]
        result[side] = {
            "p50": nearest_rank_percentile(values, 0.5),
            "p95": nearest_rank_percentile(values, 0.95),
            "available_count": len(values),
            "expected_count": len(rows),
        }
    return result


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


def _protocols_valid(baseline_results: list[dict[str, Any]], candidate_results: list[dict[str, Any]]) -> bool:
    baseline_protocols = {item.get("mode_protocol") for item in baseline_results}
    candidate_protocols = {item.get("mode_protocol") for item in candidate_results}
    baseline_versions = {item.get("dataset_version") for item in baseline_results}
    candidate_versions = {item.get("dataset_version") for item in candidate_results}
    baseline_hashes = {item.get("dataset_hash") for item in baseline_results}
    candidate_hashes = {item.get("dataset_hash") for item in candidate_results}
    return (
        bool(baseline_results)
        and bool(candidate_results)
        and baseline_protocols == {"legacy"}
        and candidate_protocols == {"explicit"}
        and len(baseline_versions) == 1
        and baseline_versions == candidate_versions
        and next(iter(baseline_versions), "").startswith("chat_mode_acceptance_v")
        and len(baseline_hashes) == 1
        and baseline_hashes == candidate_hashes
        and None not in baseline_hashes
    )


def _suite_complete(indexed: dict[str, dict[str, Any]]) -> bool:
    case_repeats: dict[str, set[int]] = {}
    for item in indexed.values():
        case_repeats.setdefault(str(item["case_id"]), set()).add(int(item["repeat_index"]))
    if len(case_repeats) != 32:
        return False
    repeat_sets = list(case_repeats.values())
    expected = repeat_sets[0] if repeat_sets else set()
    return bool(expected) and expected == set(range(max(expected) + 1)) and all(items == expected for items in repeat_sets)


def _metric_complete(summary: Any, side: str) -> bool:
    metric = summary.get(side) if isinstance(summary, dict) else None
    return bool(
        isinstance(metric, dict)
        and metric.get("expected_count", 0) > 0
        and metric.get("available_count") == metric.get("expected_count")
    )


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = list(rows[0])
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare legacy and explicit chat-mode evaluation results.")
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    comparison = compare_result_sets(load_results(args.baseline), load_results(args.candidate))
    write_comparison(comparison, args.output_dir)
    print(
        f"matched={comparison['summary']['matched_checkpoint_count']} "
        f"unmatched={comparison['summary']['unmatched_checkpoint_count']} output={args.output_dir}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
