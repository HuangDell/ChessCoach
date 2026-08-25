# Chess Review Coach

Chess Review Coach 是一个仅在本机运行的单用户国际象棋复盘应用。唯一产品运行时是一个 Python
进程：FastAPI 同时提供 JSON API 和 `frontend/` 下的无构建 Web 前端。Stockfish 和确定性 Core
拥有棋类事实；可选 Agent 与 bounded explanation 只解释已验证证据。

没有模型、credential 或 Agent SDK 时，棋盘、Engine Review、历史、长期学习画像、Retry、Puzzle
和训练仍然工作。项目不提供 MCP、模型 CLI/subprocess、交互式登录、conversation resume 或隐式
transport fallback。

## 安装与启动

要求 Python 3.11+ 和 Stockfish。推荐使用 `uv`：

```bash
uv sync
CHESS_WEB_OPEN=0 uv run python -m server.web.runner
```

启动时会自动读取项目根目录的 `.env`。可从 `.env.example` 开始配置；Shell 或启动器中已经设置的
环境变量优先于 `.env` 中的同名值。

需要 Agent 时安装锁定的 `openai-agents==0.22.0` extra：

```bash
uv sync --extra agent
CHESS_AGENT_MODEL=your-model OPENAI_API_KEY=... \
CHESS_WEB_OPEN=0 uv run python -m server.web.runner
```

也可使用普通虚拟环境：

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
CHESS_WEB_OPEN=0 .venv/bin/python -m server.web.runner
```

默认访问 <http://127.0.0.1:8765>。Web 只允许监听 loopback。Stockfish 会从
`STOCKFISH_PATH`、系统 `PATH`、常见安装目录和 `<DATA_DIR>/engine/` 查找；也可运行：

```bash
python scripts/download_stockfish.py
python -m server.doctor
```

## 复盘与训练

从 Games -> Import 粘贴或上传 PGN。分析流程为全盘快速扫描、关键局面选择、MultiPV 深度分析和
确定性 facts 提取。FEN、合法着重放、评价、分类和训练判定都不依赖模型。

复盘页可浏览 Key positions、My mistakes 和 All moves，并进入 Retry 或 Practice。个人训练会复用
现有 Engine artifact；未覆盖的合法着才触发按需 Stockfish。每次 attempt 会投影为 canonical
observation，再确定性重建 recent/lifetime skill estimate。

`Generate AI explanation` 使用独立的 bounded OpenAI-compatible API provider，结果通过 schema、
权威着法/分类和 evidence 校验后写入 `explanations.json`。它不进入 Agent loop，也不复用 Agent
credential：

```bash
CHESS_EXPLANATION_PROVIDER=openai-compatible \
CHESS_EXPLANATION_BASE_URL=https://api.example.com/v1 \
CHESS_EXPLANATION_MODEL=your-model \
CHESS_EXPLANATION_API_KEY=... \
.venv/bin/python -m server.web.runner
```

`auto` 只选择这条 API provider；base URL 或 model 缺失时 explanation 明确 unavailable，不回退
CLI。模型失败不会修改 `analysis.json`，也不会影响 Engine Review。

## Agent 配置

Agent 使用后端 Responses API、一个 Chess Coach Agent、typed function tools、structured output、
SQLite conversation session 和非流式 bounded runs。

| 环境变量 | 用途 | 默认值 |
| --- | --- | --- |
| `CHESS_AGENT_ENABLED` | 启用 Agent surface | `1` |
| `CHESS_AGENT_MODEL` | 显式模型显示名/ID | 未设置 |
| `OPENAI_API_KEY` | 官方 OpenAI Responses credential | 未设置 |
| `CHESS_AGENT_BASE_URL` | 自定义 Responses-compatible base URL | 官方 OpenAI API |
| `CHESS_AGENT_API_KEY` | 自定义 endpoint credential | 未设置 |
| `CHESS_AGENT_MAX_TURNS` | 单 run 最大 turn | `4` |
| `CHESS_AGENT_MAX_TOOL_CALLS` | 单 run 最大工具调用 | `6` |
| `CHESS_AGENT_MAX_ENGINE_CALLS` | 单 run 最大 Engine 工具调用 | `2` |
| `CHESS_AGENT_TIMEOUT` | 单 run wall-clock 秒数 | `120` |
| `CHESS_AGENT_RUN_MAX_RECORDS` | `runs.jsonl` 最多记录数 | `1000` |

官方 OpenAI 路径不需要本地 compatibility certificate。自定义 endpoint 必须先使用相同 runtime、
fixture tools、dataset 和 scorer 完整通过 portfolio；证书绑定 endpoint SHA-256 指纹、模型、SDK、
policy、response schema、dataset 和 scorer 版本。缺失、损坏或不匹配时 capability 返回
`agent_endpoint_incompatible`，不会回退到 Chat Completions 或自建 tool loop。

Credential 只由后端读取，不返回浏览器，不写 prompt、conversation、artifact、run log 或 eval
report。完整 custom 认证命令见 [Operations](docs/operations.md)。

## 其他配置

| 环境变量 | 用途 | 默认值 |
| --- | --- | --- |
| `CHESSCOACH_DATA_DIR` | 所有个人数据和 managed Engine 的根目录 | 操作系统用户数据目录 |
| `CHESS_DATA_DIR` | 旧数据目录变量兼容 | 未设置 |
| `STOCKFISH_PATH` | Stockfish 路径或命令名 | 自动发现 |
| `CHESS_WEB_HOST` / `CHESS_WEB_PORT` | loopback 地址和端口 | `127.0.0.1` / `8765` |
| `CHESS_WEB_OPEN` | 启动时打开浏览器 | `1` |
| `CHESS_ENGINE_POOL_SIZE` | Stockfish pool 大小 | `2` |
| `CHESS_SWEEP_DEPTH` | Stage 1 扫描深度 | `16` |
| `CHESS_DEEP_ANALYSIS_DEPTH` | Stage 2 深度 | `22` |
| `CHESS_DEEP_ANALYSIS_MULTIPV` | Stage 2 候选数 | `3` |
| `CHESS_ANALYSIS_PRESET` | `fast` / `balanced` / `deep` | `balanced` |
| `CHESS_ENGINE_CACHE` | 版本化 Engine cache | `1` |
| `CHESS_PERSONALIZE_HISTORY` | canonical learning memory/训练个性化 | `1` |

设置页只保存仍有效的 Web 设置到 `<DATA_DIR>/settings.json`。旧 `coach_ai_*`、`local_llm_*` 和
`claude-cli` 值可以被旧文件读取，但会被忽略，且下次保存时不会重写。

## 数据与 API

主要 artifact：

```text
<DATA_DIR>/games/<game_id>/analysis.json
<DATA_DIR>/games/<game_id>/explanations.json
<DATA_DIR>/history/games.jsonl
<DATA_DIR>/history/attempts.jsonl
<DATA_DIR>/learning/observations.jsonl
<DATA_DIR>/learning/estimates.json
<DATA_DIR>/agent/conversations.sqlite3
<DATA_DIR>/agent/sessions/<session_id>.json
<DATA_DIR>/agent/runs.jsonl
<DATA_DIR>/agent/compatibility/custom-responses.json
```

`analysis.json`、`explanations.json`、history、attempt 和 learning schema 保持兼容。旧 analysis
cache 中的 `coach_ai_text` 可加载但被忽略。

Agent API 包括 session create/get/context/message/delete、validated start-training action，以及：

```text
GET    /api/agent/metrics?limit=100
DELETE /api/agent/runs
```

run log 只记录 version、run/session/generation、去敏 task/activity、model/endpoint type、usage、
status/error、latency 和 tool 摘要。位置只保存 game/critical reference 或 FEN fingerprint；不保存
完整 prompt、FEN、PV、base URL、credential、reasoning 或 traceback。清理 runs 不会触碰 session、
learning、棋局、解释、attempt 或 Engine cache。

## Eval 与验证

Phase 0 的 26 个 case、ID 和 v1 baseline 保持不变；`agent-portfolio-v2` 在其上增加 summary、
reference/action、recent improvement、training diversity/stale source、storage、stale/cancel、
malformed output 和 custom incompatibility cases。

默认离线验证：

```bash
npm run test:frontend
.venv/bin/python -m unittest discover -s tests/backend -p 'test_*.py'
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -c \
  'from server.web.app import create_app; create_app()'
.venv/bin/python -m tests.evals.run_portfolio --source deterministic
```

真实 OpenAI/custom eval 是显式网络操作，不属于默认测试，也不把 credential 作为开发前提。当前
提交包含 deterministic v2 report；live OpenAI/custom reports 需提供 credential 后生成。详细命令、
降级语义和清理规则见 [Operations](docs/operations.md)。架构决策见 [ADR](docs/adr/)，Agent 需求
与阶段状态见 [Agent requirements](docs/requirements/agent-design.md)。
