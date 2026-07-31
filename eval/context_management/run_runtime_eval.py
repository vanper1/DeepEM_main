from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from eval.context_management.runtime_eval_core import prepare_documents, run_case
from eval.context_management.runtime_eval_schema import load_jsonl_cases, validate_cases
from eval.context_management.score_runtime_eval import load_results, write_reports
from main.agent.config import LLMSettings
from main.agent.llm import OpenAICompatibleLLMClient


DEFAULT_LONG_DATASET = Path("eval/context_management/long_conversation_v1.jsonl")
DEFAULT_DOCUMENT_DATASET = Path("eval/context_management/document_qa_v1.jsonl")
DEFAULT_PRESSURE_DATASET = Path("eval/context_management/context_pressure_v2.jsonl")
DEFAULT_OUTPUT_DIR = Path("eval/context_management/results/runtime_v1")
DEFAULT_HF_ENDPOINT = "https://hf-mirror.com"


def load_project_env(path: Path | None = None) -> None:
    env_path = path or PROJECT_ROOT / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


def configure_tokenizer_network(*, offline: bool) -> None:
    os.environ.setdefault("HF_ENDPOINT", DEFAULT_HF_ENDPOINT)
    if offline:
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"


def select_cases(
    *,
    suite: str,
    case_ids: set[str] | None,
    long_dataset: Path = DEFAULT_LONG_DATASET,
    document_dataset: Path = DEFAULT_DOCUMENT_DATASET,
    pressure_dataset: Path = DEFAULT_PRESSURE_DATASET,
) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    if suite in {"all", "long_conversation"}:
        cases.extend(load_jsonl_cases(long_dataset))
    if suite in {"all", "document_qa"}:
        cases.extend(load_jsonl_cases(document_dataset))
    if suite in {"all", "context_pressure"}:
        cases.extend(load_jsonl_cases(pressure_dataset))
    validate_cases(cases, source=suite)
    if case_ids:
        known = {str(case["id"]) for case in cases}
        missing = sorted(case_ids - known)
        if missing:
            raise ValueError(f"Unknown case ids: {', '.join(missing)}")
        cases = [case for case in cases if str(case["id"]) in case_ids]
    return cases


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run DeepEM end-to-end context management evaluation.")
    parser.add_argument("--suite", choices=["all", "long_conversation", "document_qa", "context_pressure"], default="all")
    parser.add_argument("--case-id", default=None, help="Comma-separated case ids")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--save-prompts", action="store_true")
    parser.add_argument("--pred-path", type=Path, default=None)
    parser.add_argument("--api-docs-path", type=Path, default=None)
    parser.add_argument(
        "--pressure-dataset",
        type=Path,
        default=DEFAULT_PRESSURE_DATASET,
        help="Path to the context-pressure JSONL dataset",
    )
    parser.add_argument(
        "--max-agent-steps",
        type=int,
        default=None,
        help="Optional smoke-test cap; default uses the production profile step budget",
    )
    parser.add_argument(
        "--tokenizer-offline",
        action="store_true",
        help="Use only the local tokenizer cache; default downloads through the configured HF endpoint",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.max_agent_steps is not None and args.max_agent_steps <= 0:
        parser.error("--max-agent-steps must be greater than zero")

    load_project_env()
    configure_tokenizer_network(offline=args.tokenizer_offline)
    case_ids = {item.strip().lower() for item in (args.case_id or "").split(",") if item.strip()} or None
    cases = select_cases(suite=args.suite, case_ids=case_ids, pressure_dataset=args.pressure_dataset)
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_path = output_dir / "raw_results.jsonl"
    if raw_path.exists() and not args.resume:
        raw_path.write_text("", encoding="utf-8")
    existing_results = load_results(raw_path) if args.resume else []
    completed = {str(item.get("experiment_key")) for item in existing_results}

    main_settings = LLMSettings.from_env(prefix="DEEPEM_LLM_")
    llm_client = OpenAICompatibleLLMClient(main_settings)
    results = list(existing_results)
    with prepare_documents(
        llm_client,
        pred_path=args.pred_path,
        api_docs_path=args.api_docs_path,
    ) as documents:
        experiment_id = datetime.now(timezone.utc).strftime("runtime-v1-%Y%m%dT%H%M%SZ")
        manifest = _manifest(
            experiment_id=experiment_id,
            args=args,
            cases=cases,
            main_model=main_settings.model,
            documents=documents,
        )
        (output_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        with raw_path.open("a", encoding="utf-8") as handle:
            for case in cases:
                key = f"{case['id']}:semantic:0"
                if key in completed:
                    print(f"skip {key}")
                    continue
                print(f"run {key}", flush=True)
                try:
                    result = run_case(
                        case,
                        strategy="semantic",
                        repeat_index=0,
                        llm_client=llm_client,
                        documents=documents,
                        judge_client=None,
                        save_prompts=args.save_prompts,
                        max_agent_steps=args.max_agent_steps,
                    )
                except Exception as exc:
                    result = {
                        "experiment_key": key,
                        "case_id": case["id"],
                        "suite": case["suite"],
                        "category": case.get("category"),
                        "execution_mode": case.get("execution_mode"),
                        "strategy": "semantic",
                        "repeat_index": 0,
                        "passed": False,
                        "checkpoints": [],
                        "fatal_error": f"{type(exc).__name__}: {exc}",
                    }
                result.update(
                    {
                        "experiment_id": experiment_id,
                        "dataset_version": "runtime_context_v1",
                        "main_model": main_settings.model,
                        "judge_model": None,
                        "document_source_hashes": documents.source_hashes,
                    }
                )
                handle.write(json.dumps(result, ensure_ascii=False) + "\n")
                handle.flush()
                results.append(result)
                completed.add(key)

    summary = write_reports(results, output_dir)
    print(
        f"results={len(results)} checkpoints={summary['checkpoint_count']} "
        f"failed={summary['failed_checkpoint_count']} output={output_dir}"
    )
    return 0


def _manifest(
    *,
    experiment_id: str,
    args: argparse.Namespace,
    cases: list[dict[str, Any]],
    main_model: str,
    documents,
) -> dict[str, Any]:
    return {
        "experiment_id": experiment_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "command": " ".join(sys.argv),
        "suite": args.suite,
        "case_ids": [str(case["id"]) for case in cases],
        "context_mode": "semantic",
        "repeat_count": 1,
        "judge_enabled": False,
        "main_model": main_model,
        "judge_model": None,
        "semantic_timeout_seconds": 60.0,
        "semantic_max_input_chars": 12000,
        "document_source_hashes": documents.source_hashes,
        "document_source_paths": documents.source_paths,
        "dataset_hashes": {
            "long_conversation": _sha256_file(DEFAULT_LONG_DATASET),
            "document_qa": _sha256_file(DEFAULT_DOCUMENT_DATASET),
            "context_pressure": _sha256_file(getattr(args, "pressure_dataset", DEFAULT_PRESSURE_DATASET)),
        },
        "git_commit": _git_value(["rev-parse", "HEAD"]),
        "git_dirty": bool(_git_value(["status", "--porcelain"])),
        "resume": bool(args.resume),
        "save_prompts": bool(args.save_prompts),
        "max_agent_steps": args.max_agent_steps,
        "tokenizer_endpoint": os.environ.get("HF_ENDPOINT"),
        "tokenizer_offline": bool(args.tokenizer_offline),
    }


def _sha256_file(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git_value(arguments: list[str]) -> str | None:
    try:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception:
        return None
    text = completed.stdout.strip()
    return text or None


if __name__ == "__main__":
    raise SystemExit(main())
