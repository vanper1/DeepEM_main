# Context Pressure Dataset Design

## Goal

Add a separate, reproducible context-pressure evaluation suite for validating
token-threshold semantic compression. It must exercise the 65-80%, 80-90%,
and above-90% usable-input-token bands that the existing 18-checkpoint
baseline does not reach.

The existing `long_conversation_v1.jsonl` and `document_qa_v1.jsonl` files
remain unchanged and continue to be the report baseline.

## Scope

The new dataset is `eval/context_management/context_pressure_v1.jsonl`.
Each case contains a declared `pressure_target` and normal scored turns. The
target has a lower and upper token-usage ratio, plus a filler profile. A
runtime helper creates deterministic, non-actionable observation-log text for
the first turn. The real HTTP/SSE runner sends that prefix as a normal user
turn, so the server's production prompt assembly, compression, and tool
protocol are evaluated unchanged.

The helper estimates the generated prefix using the project's
`ContextBudgetEstimator`; it adjusts its filler size until the estimated
prefix is near the requested band. This is preparation only. The actual band
result is the `peak_token_usage_ratio` recorded from runtime debug traces.

The first version contains six independent cases:

1. `cp-01`: 65-80% pressure; expects no semantic summary once threshold
   compression is enabled and retains an early document fact.
2. `cp-02`: 80-90% pressure; verifies normal semantic compression preserves
   an early fact, a negation, and the document page scope.
3. `cp-03`: above-90% pressure; verifies aggressive compression does not
   leave an orphaned tool-call or tool-result message.
4. `cp-04`: high pressure plus detail recovery; a recovered complete field
   must remain available rather than becoming a preview again.
5. `cp-05`: high pressure with similar historical anchors; the requested
   `tool_result_id + path` must be selected rather than another result.
6. `cp-06`: high pressure and document-only source scope; runtime or
   workspace facts must not appear in the final answer.

`cp-01` will document the current behavior before threshold gating and become
an expected no-summary assertion after that P0 change. It is not a claim that
the current runtime already has the desired behavior.

## Data and CLI Contract

The schema gains the `context_pressure` suite and an optional
`pressure_target` object:

```json
{
  "min_usage_ratio": 0.65,
  "max_usage_ratio": 0.80,
  "filler_profile": "observation_log",
  "calibration_turn_id": "pressure"
}
```

The object is required for `context_pressure` cases, and forbidden for the
two existing suites. Bounds are inclusive, must be in `(0, 1]`, and the lower
bound cannot exceed the upper bound.

Both evaluation runners gain `--dataset-path PATH`. Supplying it selects only
that JSONL file; no standard suite is implicitly included. Supplying it with
`--suite` or `--case-id` remains supported as filtering over the custom
dataset. The manifest records the custom file path and SHA-256 hash.

The HTTP runner expands the calibration turn immediately before execution.
The native runtime runner uses the same expansion. Generated text is not
stored in the dataset and is marked in results as generated pressure context.

## Measurements and Pass Conditions

Existing answer, evidence, anchor, duplicate-call, retrieval, latency, and
context metrics remain authoritative. New pressure metadata records:

- requested lower/upper ratios;
- estimated ratio at generation time;
- actual peak ratio from debug traces;
- whether the actual peak was within the requested band;
- calibration text character count and filler profile.

A band miss is reported explicitly and makes the case fail its pressure
assertion, but it remains distinguishable from answer or protocol failures.
This prevents a correct answer at 45% usage from being counted as successful
threshold-compression validation.

The suite does not attempt to force a provider context-length error. That
behavior remains an integration test because exact provider wrappers and
hidden token accounting are outside the local tokenizer's control.

## Error Handling

If tokenizer estimation is unavailable, pressure generation fails before any
model request with a clear error. It must never silently fall back to a
character threshold because the purpose is token-band validation. If a target
cannot be approached within a bounded number of adjustments, the helper
returns its closest estimate and records a band miss rather than looping.

## Test Coverage

Tests are written before production changes and cover:

1. schema validation accepts a valid pressure target and rejects invalid or
   misplaced targets;
2. deterministic calibration produces bounded non-actionable text and uses
   the token estimator;
3. custom dataset selection leaves the standard default selection unchanged;
4. both manifests record the custom dataset hash;
5. a measured band miss is surfaced separately and fails the pressure
   assertion;
6. generated pressure cases keep existing scored-turn and tool-scoring rules.

HTTP execution itself will be smoke-tested with one 65-80% case before the
six-case run because each case intentionally has a large prompt.

## Non-Goals

This change does not implement threshold-triggered compression, alter normal
or aggressive compression behavior, change tool-result compaction, or modify
the 18-case report baseline. It supplies the measurement harness needed to
evaluate those later behavior changes.
