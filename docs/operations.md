# Operations

Chess Review Coach is a local, single-user Web process. Keep `CHESS_WEB_HOST` on loopback and use a
dedicated `CHESSCOACH_DATA_DIR` for development, tests, and each installation.

## Runtime and credentials

The core Web application needs Python and Stockfish. Agent support additionally needs the locked
Agents SDK extra, an explicit model, and a backend credential:

```bash
uv sync --extra agent
CHESS_WEB_OPEN=0 CHESS_AGENT_MODEL=your-model \
OPENAI_API_KEY=... uv run python -m server.web.runner
```

`OPENAI_API_KEY`, `CHESS_AGENT_API_KEY`, and `CHESS_EXPLANATION_API_KEY` are read only by the
backend. They are not returned to the browser or written to artifacts, prompts, telemetry, or eval
reports. The bounded explanation provider has its own base URL/model/key and does not enter the
Agent loop.

Agent runs are non-streaming and bounded by `CHESS_AGENT_MAX_TURNS`,
`CHESS_AGENT_MAX_TOOL_CALLS`, `CHESS_AGENT_MAX_ENGINE_CALLS`, and `CHESS_AGENT_TIMEOUT`.
`CHESS_AGENT_RUN_MAX_RECORDS` bounds the run log and defaults to 1000.

## Data layout

```text
<DATA_DIR>/
  games/<game_id>/analysis*.json       authoritative Engine review
  games/<game_id>/explanations*.json   validated bounded explanations
  history/games.jsonl                  local history
  history/attempts.jsonl               deterministic training attempts
  learning/observations.jsonl          canonical evidence
  learning/estimates.json              rebuildable learning projection
  agent/conversations.sqlite3          SDK conversation items
  agent/sessions/*.json                chess checkpoints and summaries
  agent/runs.jsonl                     redacted bounded telemetry
  agent/compatibility/custom-responses.json
```

Run telemetry contains IDs, version metadata, status, stable errors, usage totals, latency, and
redacted tool records. It never contains prompts, raw endpoint URLs, credentials, full FEN/PV,
reasoning, or tracebacks. `GET /api/agent/metrics?limit=100` aggregates recent records.
`DELETE /api/agent/runs` clears only telemetry; the Settings data controls expose the same action.

## Degradation

- Missing Agent SDK/model/key: Agent chat reports `agent_unavailable`.
- Missing or mismatched custom certificate: `agent_endpoint_incompatible`.
- Provider auth, rate, timeout, or malformed output: stable typed Agent errors; staged conversation
  is discarded.
- Missing model configuration never disables Engine Review, history, learning, puzzles, or training.
- Missing Stockfish disables new Engine analysis, but saved artifacts and non-Engine views remain
  readable.
- Run-log write failure is internal telemetry degradation and never rolls back a valid conversation
  or business artifact.

## Eval and custom certification

The offline portfolio is reproducible and never reads credentials or user data:

```bash
.venv/bin/python -m tests.evals.run_portfolio --source deterministic
```

Official OpenAI and custom evals are explicit network operations. Both use a temporary data
directory, fixture tools, and the production `OpenAIAgentsRuntime`:

```bash
OPENAI_API_KEY=... .venv/bin/python -m tests.evals.run_portfolio \
  --source openai --model your-model --output reports/openai-responses.json

CHESS_AGENT_API_KEY=... .venv/bin/python -m tests.evals.run_portfolio \
  --source custom --model custom-model --base-url http://127.0.0.1:9900/v1 \
  --certificate-data-dir "$CHESSCOACH_DATA_DIR" \
  --output reports/custom-responses.json
```

A custom certificate is written only when the Responses structured-output, function-tool, SQLite
recent-items, and portfolio quality gates all pass. Reports record an endpoint fingerprint rather
than the raw custom URL. Changing the URL, model, SDK, policy, response schema, dataset, or scorer
invalidates the certificate immediately.

Live reports include `live_case_diagnostics` with only case IDs and boolean grounding/tool/outcome
checks. For `grounded_response_rate`, every non-error case is applicable and passes only if its
required evidence, position, uncertainty, required claims, and forbidden claims all match. The
diagnostics deliberately omit prompts, answer text, credentials, and raw endpoint data.
Tool-attempt summaries are limited to tool names, canonical skill IDs, unresolved-focus counts,
bounded position counts, and analysis purpose; raw tool arguments are not retained.
