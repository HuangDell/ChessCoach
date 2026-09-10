# Agent baseline evals

`agent_baseline_v1.json` is the fixed evaluation set described in the
[Phase 0 requirements](../../docs/requirements/agent/phase-0-contracts-and-baseline.md).
It is intentionally independent
of an Agent SDK, model, Engine process, network access, and user data.

Each case names shared fixtures and records expectations that the deterministic
runner can score without inferring policy from prose:

- `expected.tools` defines exact required calls, the allowed/forbidden tool set,
  and total/Engine call budgets.
- `expected.grounding` defines evidence, chess references, uncertainty, and an
  executable matcher list for required and forbidden claims.
- `expected.personalization` defines whether profile-derived claims are allowed
  and the minimum evidence needed for recurring-weakness language.
- `expected.outcome` defines full, partial, or error completion and the expected
  degradation code.

Every fake tool fixture contains a complete request plus a concrete Pydantic
`ToolResult[ResultDTO]` envelope. Referenced critical positions are the exact
`fen_before` obtained by legally replaying plies `1..N-1` for `critical_id=ply-N`.

`observed_fake_runs_v1.json` is the fixed Phase 0 observation set. `evaluator.py`
scores all documented metrics without a model, Engine process, filesystem state,
or network access. `baseline_report.json` is the reproducible aggregate output.

`grounded_response_rate` uses every non-error case as its denominator. A case contributes to the
numerator only when all five checks pass: required evidence refs, required position refs, the
expected uncertainty flag, every required claim matcher, and every forbidden claim matcher. Live
runs read these observations from the validated `AgentResponse.grounding` contract rather than
inferring them from answer keywords. A live report also includes redacted per-case
`live_case_diagnostics` booleans so a failed evidence, position, uncertainty, required-claim, or
forbidden-claim check can be identified without storing model text or prompts. Tool mismatch
diagnostics retain only tool names, canonical skill IDs, unresolved-focus counts, bounded position
counts, and analysis purpose; they never retain raw arguments.

The backend tests validate DTO schemas, timeline replay, fixture ownership,
matcher behavior, scorer sensitivity to regressions, and exact report
reproduction:

```bash
.venv/bin/python -m unittest \
  tests.backend.test_agent_eval_dataset \
  tests.backend.test_agent_eval_runner
```

Live diagnostics also include `runtime_tool_attempts` (name/status/error_code only), including
budget rejections recorded before fixture execution when final output parsing fails. These are
separate from fixture match summaries and do not change the existing quality scoring rules.

## Portfolio v2

`agent_portfolio_v2.json` imports the ordered 26 v1 case IDs without changing the v1 dataset or
report. It adds 11 hardening cases for summary limits, reference/action validation, recent
improvement, training diversity and stale sources, storage degradation, stale/cancelled runs,
malformed output, and custom endpoint incompatibility.

The custom incompatibility fixture remains frozen for v2 report comparability. It is historical
benchmark data and no longer represents a production certificate requirement.

`portfolio_v2.py` aggregates the v1 scores with the hardening observations and adds valid
reference/action, Engine calls per run, p50/p95/max latency, and degradation correctness.
`deterministic_report_v2.json` is the committed offline report. Reproduce it without an SDK,
credential, network, user data, or Engine process:

```bash
.venv/bin/python -m tests.evals.run_portfolio --source deterministic
```

Live modes are explicit. They create a temporary data directory and run the fixed v1 cases through
the production `OpenAIAgentsRuntime` with fixture tools. Aggregate reports never contain a
credential, prompt, raw base URL, reasoning, or user data:

```bash
OPENAI_API_KEY=... .venv/bin/python -m tests.evals.run_portfolio \
  --source openai --model your-model --output reports/openai-responses.json

CHESS_AGENT_API_KEY=... .venv/bin/python -m tests.evals.run_portfolio \
  --source custom --model custom-model --base-url http://127.0.0.1:9900/v1 \
  --output reports/custom-responses.json
```

Each live run also writes an unmodified raw trace under
`reports/<report-name>-traces/<run-timestamp>/<case-id>/`. Every Responses call produces a paired
`NNN-request.json` and `NNN-response.json` containing the exact HTTP body bytes sent and received;
multi-turn function calls and retries therefore remain separate native Responses payloads. Request
headers are not model input and are not recorded because they contain authorization. Use
`--trace-dir` to override the trace root. `reports/` is gitignored.

Custom mode records `benchmark_checks` and `benchmark_passed` for structured Responses, function
tools, SQLite recent-items, production response validation, and portfolio quality. Grounded
response, tool selection, task completion, reference/action validity, and degradation correctness
targets remain 100%; illegal move claims, unnecessary Engine calls, and false personalization target
0%. These results measure model behavior and do not enable or disable the production runtime.
