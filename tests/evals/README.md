# Benchmark 与评测入口

Agent portfolio 与 RAG 是两个独立 Benchmark，共用 `tests/evals/` 目录约定，
不合并 case、runner、分数或报告。

| Benchmark | 数据与入口 | 评估范围 | 当前状态 |
| --- | --- | --- | --- |
| Agent portfolio（26 条 baseline） | 本目录 `agent_baseline_v1.json`、`run_portfolio.py` | 工具编排、grounding、预算与降级 | 可运行 deterministic / live；工具为 fixture，不执行真实 RAG |
| RAG 中文查询 / 英文教材（40 条） | [rag/](rag/README.md)、`rag/datasets/zh-en-v2/`、`rag/runner.py` | 书籍检索相关性、证据边界；后续单独评价生成 | v3 索引与中文/英文检索诊断已运行；标注为模型辅助复核 |

RAG 本地数据已从 `.chess-review/knowledge/benchmarks/zh-en-v1/` 移到
`tests/evals/rag/datasets/zh-en-v1/`；真实问题、标注及教材快照继续被 Git 忽略。
新 checkout 不包含这些本地素材，需显式准备数据。代码与说明进入版本控制。
Agent 输出沿用 `reports/<report-name>.json`；RAG 后续运行产物使用
`reports/rag/<run-id>/`，不写入 Agent 报告。RAG 待完善项见 [现状分析](rag/ANALYSIS.md)。

下文保留 Agent portfolio 的使用说明；Explanation 对比和 orchestration 研究入口另行独立。

## Agent baseline evals

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


## Explanation 共享 facts 对比

显式入口 `tests.evals.explanation_compare` 不加入默认测试或 CI，不运行 Ask Coach live portfolio。
使用配置的 Explanation Chat Completions Provider，temperature 固定 0.2；仅后端读取凭据。

**在修改 builder 之前**冻结旧请求：

```bash
.venv/bin/python -m tests.evals.explanation_compare freeze --directory /tmp/chesscoach-explanation-eval
```

freeze 只读个人数据，优先匹配 2026-09-12 基线的八个局面；不可用时按最新 analysis 确定性选取，
尽量保留两个无 motif 局面。完整 analysis、逐局面 memory、模型参数和当时的 ExplanationRequest
保存在指定目录。目录必须在仓库之外。仓库不保存旧 builder；当前代码 freeze 的是当前版本，
不能凭空重建 v3 请求。已有冻结文件可直接使用。

修改后从冻结数据构建新输入并审计所有投影/allowlist 引用，再显式执行：

```bash
CHESSCOACH_DATA_DIR=/tmp/chesscoach-eval-isolated \
.venv/bin/python -m tests.evals.explanation_compare prepare --directory /tmp/chesscoach-explanation-eval

CHESSCOACH_DATA_DIR=/tmp/chesscoach-eval-isolated \
.venv/bin/python -m tests.evals.explanation_compare run --directory /tmp/chesscoach-explanation-eval
```

prepare 使用冻结 memory，不检索当前个人数据；不修改 analysis 或重排变化线。run 接受保存的请求，
顺序固定为旧、新、新、旧，每轮保持八个局面的顺序，最多 32 次请求，不自动重试。
已有 calls 目录时拒绝重跑，避免误重复付费；网络无响应时停止，需要先检查原始日志，不能把
超时误判为模型未执行。不要为了改善数字重复整批调用。启动前校验模型、endpoint 和语言与冻结
配置一致。不清空供应商缓存；首次测量不代表严格冷缓存，本地结果缓存不计入供应商命中。

每次调用的请求、原始 HTTP response、讲解文本、验证错误仅写入本地目录。report.json 报告双方、
各轮和有/无 motif 分组的 input、output、reasoning（已包含在 output 内）、耗时、模型调用数、
生产校验率、每条有效讲解 input，以及缓存 hit/miss 和 token 加权命中率。缺少明细保持未知，
同时报告有 usage/cache/reasoning 的调用数，不能将未知解读为零命中。

完成后匿名交错的 `blind_review.json` 提供统一完整源证据和输出。先逐条填写 review 的布尔值：
core_problem（核心问题正确）、board_reasons（有具体且正确的棋盘原因）、unsupported_claims
（存在证据外声明），可加 notes；核对后再打开独立 blind_key.json。重新汇总不会覆盖批注：

```bash
.venv/bin/python -m tests.evals.explanation_compare report --directory /tmp/chesscoach-explanation-eval
```

完整记录和质量审查之前 success 为 null。成功要求总 input 减少至少 30%、生产有效率不下降、
miss tokens 和每条有效讲解 input 不恶化，并且有/无 motif 两组的质量指标不下降。缓存收益受
供应商和样本影响，不是长期保证；历史 4.38% 只作参考。仅脱敏 report/config 摘要和数据指纹可
进入仓库，原始输出和冻结个人数据不得提交。
