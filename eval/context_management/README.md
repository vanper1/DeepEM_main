# DeepEM Context Management Evaluation Dataset

This directory contains a lightweight dataset for A/B testing context-management changes in the DeepEM agent runtime.

It now has two evaluation layers:

- the original offline/mock contract checks described below;
- a runtime-level long-conversation and document-QA experiment that uses the real `RunEngine`, LLM endpoint, document tools, context compression, and debug metrics in isolated in-memory repositories.

## Files

- `context_eval_v1.jsonl`: one JSON object per evaluation case.
- `tool_result_compression_v1.jsonl`: synthetic raw tool-result cases for validating compact tool messages.
- `long_conversation_v1.jsonl`: six session-level long-conversation cases with 8 scored checkpoints.
- `document_qa_v1.jsonl`: ten PReD/API document-QA cases.
- `run_runtime_eval.py`: end-to-end batch runner with rule/semantic strategies and resume support.
- `score_runtime_eval.py`: deterministic scoring, independent LLM judge, CSV/JSON summaries, and failure extraction.

## Runtime Context Evaluation

The runtime evaluation uses:

- the real configured Qwen-compatible endpoint;
- the production `RunEngine` and built-in tool handlers;
- production context budget, prompt composition, context compression, and tool transcript compression;
- an isolated in-memory state/repository layer, so it does not write to the product SQLite database;
- the same `ElasticsearchDocumentIndex`, connection settings, document processing, and tool handlers used by the frontend runtime, populated through the production `UploadProcessor` from `PReD.pdf` and `API_DOCS.md`;
- a unique isolated ES index based on `${DEEPEM_ES_INDEX}_context_eval` by default, or `DEEPEM_EVAL_ES_INDEX` when explicitly configured; a per-run suffix prevents duplicate file records and the runner deletes the index during normal cleanup.

Runtime document evaluation therefore requires the configured Elasticsearch service to be available. Unit tests replace the index builder with an in-memory test double; real evaluation does not use a separate BM25 implementation. Leave `DEEPEM_USE_MEMORY_DOCUMENT_INDEX` unset (or set it to `0`) for the ES path.

`DEEPEM_EVAL_ES_INDEX` is an index-name prefix and must differ from `DEEPEM_ES_INDEX`. An interrupted process can leave its uniquely suffixed evaluation index behind, but it cannot affect later runs or frontend filename resolution.

Run a small smoke first:

```powershell
python eval\context_management\run_runtime_eval.py `
  --case-id lc-01,dq-01 `
  --max-agent-steps 6 `
  --output-dir eval\context_management\results\runtime_smoke
```

Run the complete v1 experiment:

```powershell
python eval\context_management\run_runtime_eval.py `
  --suite all `
  --resume `
  --output-dir eval\context_management\results\runtime_v1
```

The runner always uses semantic context compression, does not call an LLM Judge, and runs each case once. The batch is sequential by design. Each result is flushed to `raw_results.jsonl`, so an interrupted run can continue with `--resume` without repeating completed cases.

Sequential-agent results contain an ordered `all_turns` ledger. Unscored setup turns keep their full diagnostics; scored turns use a lightweight `checkpoint_index` reference to the full record in `checkpoints`, avoiding duplicate payloads. Only `checkpoints` are included in reports.

`--max-agent-steps` is intended for smoke diagnosis only. Omit it in the formal experiment to use the current production `TASK_CHAT_AGENT.step_budget`; when supplied, the value is written to `manifest.json` so capped runs cannot be confused with formal results.

The production token-budget estimator and this runner default to downloading tokenizer files through the domestic Hugging Face mirror at `https://hf-mirror.com`. Set `HF_ENDPOINT` in the process environment or project `.env` to use a different endpoint. Use `--tokenizer-offline` to force local-cache-only loading (`HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`); if the tokenizer is unavailable, the production estimator records the error and falls back to the character budget.

Rebuild deterministic reports without re-running the main agent:

```powershell
python eval\context_management\score_runtime_eval.py `
  --input eval\context_management\results\runtime_v1\raw_results.jsonl `
  --output-dir eval\context_management\results\runtime_v1
```

Runtime output files:

- `manifest.json`: command, models, Git state, dataset/document hashes, and compression settings.
- `raw_results.jsonl`: one record per case, including checkpoint answers, tool traces, summaries, and production debug metrics.
- `summary.json` / `summary.csv`: overall, suite, and category aggregates for the current semantic implementation.
- `case_scores.csv`: checkpoint-level deterministic scores and core context/tool metrics.
- `failures.jsonl`: failed checkpoints and diagnostic fields.

The default runner does not store API keys, full chain-of-thought, full raw tool payloads, or full prompts. Use `--save-prompts` only for targeted failure diagnosis.

## Case Schema

Each line in the JSONL file has these fields:

- `id`: stable case id.
- `category`: broad scenario group.
- `difficulty`: `easy`, `medium`, or `hard`.
- `conversation_setup`: prior turns or state facts that should be available before the evaluated user message.
- `user_message`: the message to send to the agent.
- `expected_context`: facts that must be available to the model after context assembly.
- `expected_tools`: tool names that should be called, when applicable.
- `forbidden_claims`: claims the answer must not make.
- `success_criteria`: observable pass conditions.
- `scoring`: 0-2 manual rubric.
- `metrics`: recommended instrumentation to compare A/B runs.

## Manual Scoring

- `2`: correct, grounded, uses the right context/tools, and avoids unsupported claims.
- `1`: partially correct, but misses a key fact/tool or gives an incomplete answer.
- `0`: incorrect, hallucinates, ignores required context, or fails the task.

## A/B Use

Run every case with the current prompt builder and again with the optimized context builder. Compare:

- prompt size
- latency
- required fact recall
- required tool use
- hallucination rate
- final task success

## Offline Assembly Test

This test does not call the model and does not require the USRP service. It only assembles prompts from the dataset and checks whether required context facts survive prompt construction.

```powershell
python eval\context_management\run_context_eval.py --write-prompts
```

The runner writes:

- `eval/context_management/results/offline_context_eval.jsonl`
- `eval/context_management/results/prompts/*.txt` when `--write-prompts` is set

Filter a single category:

```powershell
python eval\context_management\run_context_eval.py --category usrp_status --write-prompts
```

Exit code is `0` only when all cases pass. A non-zero exit code is expected for a baseline run if any required context fact is missing.

## Mock Tool Agent Test

This test does not call the model and does not require the USRP service. It uses a deterministic mock agent to select tools from each case and checks whether the selected tools include `expected_tools` and avoid simple forbidden claims.

```powershell
python eval\context_management\run_mock_agent_eval.py
```

The runner writes:

- `eval/context_management/results/mock_agent_eval.jsonl`

Filter a single category:

```powershell
python eval\context_management\run_mock_agent_eval.py --category usrp_status
```

This is a semi-offline gate for tool-routing expectations. It does not judge final answer quality from Qwen.

## LLM + Mock Tool Agent Test

Use `--mode llm` to call the Qwen-compatible chat endpoint configured in `.env`, while still mocking every tool result locally. This lets you evaluate whether the model chooses the right tools even when the USRP server is offline.

```powershell
python eval\context_management\run_mock_agent_eval.py --mode llm --category usrp_status
```

The runner writes:

- `eval/context_management/results/mock_agent_eval_llm.jsonl`

This mode loads `DEEPEM_LLM_*` settings from `.env`, sends the assembled DeepEM context to Qwen, passes mock tool definitions, appends deterministic mock tool responses, and asks Qwen for the final answer. No real USRP, database, or document tool is executed.

## Tool Result Compression Test

This test targets the `ToolResultCompressor` contract directly. It does not call the model. It feeds synthetic large `raw_result` payloads into the evaluation compressor and checks whether compact tool messages preserve required facts, remove noisy fields, stay under budget, and include recovery metadata such as `tool_result_id`.

```powershell
python eval\context_management\run_tool_result_compression_eval.py
```

The runner writes:

- `eval/context_management/results/tool_result_compression_eval.jsonl`

Filter a single category:

```powershell
python eval\context_management\run_tool_result_compression_eval.py --category nl2sql_rows
```

The current dataset covers:

- `nl2sql_rows`: large SQL rows should become top-row previews plus SQL/row metadata.
- `document_chunks`: large document chunks should become short snippets plus source metadata.
- `usrp_code_execution`: generated code, logs, FFT arrays, and per-frequency details should be omitted while key sweep results remain.
- `tool_error`: errors should keep status/stage/error summaries while long stderr/traceback are omitted.

Key output metrics:

- `raw_chars`
- `compact_chars`
- `compression_ratio`
- `missing_required_values`
- `missing_required_fields`
- `unexpected_omitted_fields_present`
- `has_result_id`
- `has_truncated_flag`
- `retrievable_omissions`

## Frontend HTTP/SSE Runtime Evaluation

This runner executes the two runtime datasets through the same server endpoints used by `chat.js`. It creates a real chat session, uploads documents through the production upload handler, sends every turn through `/api/chat/sse`, and reads the resulting run diagnostics from the configured debug directory.

Prerequisites:

- Start the DeepEM server and confirm the chat page works.
- Enable semantic compression in `.env` with `DEEPEM_CONTEXT_SEMANTIC_COMPRESSION=1`.
- Keep the configured SQLite, Elasticsearch, asset storage, main LLM, and tokenizer available.
- Set `HF_ENDPOINT=https://hf-mirror.com` when the tokenizer is not already cached.
- Avoid simultaneous manual chat traffic during timed runs because all chat sessions share the server task lock.

For targeted diagnosis of Semantic `output_too_long`, set `DEEPEM_CONTEXT_SEMANTIC_DEBUG_CAPTURE=1` before starting the server. This writes the complete Semantic JSON response and `semantic_raw_output_chars` only into the corresponding run debug JSONL. It is intentionally excluded from runtime evaluation result files and should remain disabled outside the local evaluation environment because it can contain conversation and document content.

Smoke test:

```powershell
D:\anaconda3\envs\deepem\python.exe eval\context_management\run_http_runtime_eval.py `
  --base-url http://127.0.0.1:8000 `
  --case-id lc-05,dq-02,dq-06 `
  --output-dir eval\context_management\results\http_smoke_v1
```

Full test:

```powershell
D:\anaconda3\envs\deepem\python.exe eval\context_management\run_http_runtime_eval.py `
  --base-url http://127.0.0.1:8000 `
  --suite all `
  --output-dir eval\context_management\results\http_full_v1
```

Use `--resume` to skip case keys already present in `raw_results.jsonl`. The runner is semantic-only and does not use an LLM Judge. Results include deterministic gold scoring, context/tool compression metrics, `semantic_latency_ms`, time to first activity, time to first visible token, time to the first final-answer token, and end-to-end turn latency.

Dataset `prior_messages` are not inserted directly into storage. Operator/user prefix messages are replayed as real HTTP turns and the server generates their assistant replies; fixed assistant prefix text is skipped because the frontend has no API for injecting an assistant message.

The HTTP runner intentionally uses the current server's SQLite, Elasticsearch index, and asset directory. Evaluation records therefore remain in the test environment, and old indexed documents can affect retrieval if that environment is not cleaned between experiments.

## Cross-Version Chat Mode Evaluation

`chat_mode_acceptance_v1.jsonl` contains 32 cases covering general knowledge, general follow-ups, onsite questions used in general mode, mixed-mode history isolation, and workspace regressions. Every turn declares `chat_mode` and an LLM options profile.

The HTTP runner checks `/openapi.json` before the run:

- A legacy server without `ChatIn.chat_mode` receives no `chat_mode` field. The run is a baseline and succeeds when measurement is complete, even when target behavior does not pass.
- A server exposing `ChatIn.chat_mode` receives the requested mode on every turn. Its `execution_policy` SSE event is required and is scored against the dataset policy.
- `--mode-protocol legacy` forces the old wire protocol on a newer server. `--mode-protocol explicit` rejects a server that does not advertise the field.

Current-version baseline:

```powershell
python eval\context_management\run_http_runtime_eval.py `
  --suite chat_mode_acceptance `
  --mode-protocol auto `
  --repeat 20 `
  --output-dir eval\context_management\results\chat_mode_baseline_v1
```

Candidate run:

```powershell
python eval\context_management\run_http_runtime_eval.py `
  --suite chat_mode_acceptance `
  --mode-protocol auto `
  --repeat 20 `
  --output-dir eval\context_management\results\chat_mode_candidate_v1
```

Compare the aligned `case_id + turn_id + repeat_index` checkpoints:

```powershell
python eval\context_management\compare_chat_mode_results.py `
  --baseline eval\context_management\results\chat_mode_baseline_v1\raw_results.jsonl `
  --candidate eval\context_management\results\chat_mode_candidate_v1\raw_results.jsonl `
  --output-dir eval\context_management\results\chat_mode_comparison_v1
```

The comparison writes `comparison.json`, `comparison.csv`, `unmatched_rows.jsonl`, and `comparison_report.md`. Acceptance fails when protocols, dataset versions, or dataset SHA-256 hashes differ; the fixed 32-case/repeat matrix is incomplete; checkpoint keys or metadata do not align; any run/measurement/explicit policy is incomplete; or a performance metric is missing. Token measurements are tokenizer estimates named `estimated_input_tokens`; they are not reported as model API usage. Performance comparisons require the same model, machine, concurrency, workspace state, and repeat count.
