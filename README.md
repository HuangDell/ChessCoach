# Chess Review Coach

Chess Review Coach 是一个仅在本机运行的个人国际象棋复盘 Web 应用。当前代码基于
[`Chess-analysis-mcp/tintins-chess-analysis`](https://github.com/Chess-analysis-mcp/tintins-chess-analysis)
v2.0.2，保留其 FastAPI、Stockfish 进程池、Chessground 棋盘、历史记录、变化探索和
训练能力，并将 Web 作为唯一主运行路径。

## 环境要求

- Python 3.11+
- [Stockfish](https://stockfishchess.org/download/) 可执行文件
- `uv`，或 Python 自带的 `venv` + `pip`

Stockfish 会依次从 `STOCKFISH_PATH`、系统 `PATH`、常见安装目录以及应用数据目录的
`engine/` 子目录中查找。Linux 可使用系统包管理器安装，也可执行：

```bash
python scripts/download_stockfish.py
```

## 启动

使用 `uv`：

```bash
uv sync
uv run python -m server.web.runner
```

或使用标准 Python 环境：

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m server.web.runner
CHESS_WEB_OPEN=0 CHESS_WEB_PORT=8875 .venv/bin/python -m server.web.runner
CHESSCOACH_DATA_DIR=/home/msn/ChessCoach/.chess-review CHESS_WEB_OPEN=0 CHESS_WEB_PORT=8875 .venv/bin/python -m server.web.runner
```

浏览器访问 <http://127.0.0.1:8765>。打开 Games 面板的 `Import`，可粘贴或上传
单盘/多盘 PGN；系统会先规范化并保存棋局，再执行“全盘快速扫描 -> 关键局面选择 ->
MultiPV 深度分析 -> 确定性棋盘事实提取”。事实层从 FEN 和合法 PV 重放生成，不调用
LLM；用户主动请求后，后端才会逐个关键局面生成结构化中文解释。同一 Engine/Facts 输入
和同一 Provider 配置都会在本地复用。

复盘页默认进入 `Key positions`，也可切换到 `My mistakes` 或 `All moves`。棋盘、White POV
评估时间线、关键局面列表和解释面板共用同一个 ply；实战线/最佳线中的 SAN 可点击并播放，
随时可以返回主线。AI 不可用时面板直接展示 Engine facts、推荐着和合法变化，不阻塞复盘。

每个关键局面都可以进入 `Retry`：棋盘先隐藏实战着、Engine 推荐、箭头、评价和解释，用户
直接走一步后再获得 MultiPV/Stockfish 反馈。提示按 Think、Area、First move、Show line
逐级揭示。`Practice` 会把当前局面直接送入 `Puzzles -> From your games`；个人题也可按
fact category 筛选，多个合理候选着均可通过，不要求猜中 Engine 第一选择。

## 本地配置

| 环境变量 | 用途 | 默认值 |
| --- | --- | --- |
| `CHESSCOACH_DATA_DIR` | 历史、设置、缓存和 managed Stockfish 的根目录 | 操作系统用户数据目录 |
| `CHESS_DATA_DIR` | 兼容上游项目的数据目录变量 | 未设置 |
| `STOCKFISH_PATH` | Stockfish 路径或命令名 | 自动发现 |
| `CHESS_WEB_HOST` | Web 监听地址，只允许 loopback | `127.0.0.1` |
| `CHESS_WEB_PORT` | Web 端口 | `8765` |
| `CHESS_WEB_OPEN` | 启动时是否自动打开浏览器 | `1` |
| `CHESS_ENGINE_POOL_SIZE` | 复用的 Stockfish 进程数 | `2` |
| `CHESS_SWEEP_DEPTH` | Stage 1 全盘扫描深度 | `16` |
| `CHESS_DEEP_ANALYSIS_DEPTH` | Stage 2 关键局面深度 | `22` |
| `CHESS_DEEP_ANALYSIS_MULTIPV` | 关键局面候选线数量，最少为 3 | `3` |
| `CHESS_CRITICAL_MIN` / `CHESS_CRITICAL_MAX` | 每盘关键局面目标范围 | `3` / `8` |
| `CHESS_ANALYSIS_PRESET` | Web 分析档位：`fast` / `balanced` / `deep` | `balanced` |
| `CHESS_FACT_LINE_PLIES` | facts 对实战线和最佳线的最大重放 ply 数 | `8` |
| `CHESS_ENGINE_CACHE` | 是否启用版本化局面磁盘缓存 | `1` |
| `CHESS_EXPLANATION_PROVIDER` | 解释 Provider：`auto` / `openai-compatible` / `claude-cli` | `auto` |
| `CHESS_EXPLANATION_BASE_URL` | 专用于解释的 OpenAI-compatible API base URL | 复用设置页本地模型 |
| `CHESS_EXPLANATION_MODEL` | 专用于解释的模型名 | 复用设置页本地模型 |
| `CHESS_EXPLANATION_API_KEY` | 远程兼容 API 的 Bearer Key，仅后端进程读取 | 未设置 |
| `CHESS_EXPLANATION_TIMEOUT` | 单局面模型调用超时秒数 | `600` |
| `CHESS_EXPLANATION_LANGUAGE` | 结构化解释语言：`zh-CN` / `en` | `zh-CN` |
| `CHESS_AGENT_ENABLED` | 是否启用可选 Agent API | `1` |
| `CHESS_AGENT_MODEL` | Agent SDK 使用的模型，必须显式配置 | 未设置 |
| `CHESS_AGENT_BASE_URL` | 可选的 Responses-compatible API base URL | OpenAI 官方 API |
| `CHESS_AGENT_API_KEY` | 自定义 Agent endpoint credential，仅后端读取 | 未设置 |
| `OPENAI_API_KEY` | OpenAI 官方 Agent API credential，仅后端读取 | 未设置 |
| `CHESS_AGENT_MAX_TURNS` | 单次 Agent run 最大 turn | `4` |
| `CHESS_AGENT_MAX_TOOL_CALLS` | 单次 Agent run 最大工具调用 | `6` |
| `CHESS_AGENT_MAX_ENGINE_CALLS` | 单次 Agent run 最大 Engine 工具调用 | `2` |
| `CHESS_AGENT_TIMEOUT` | 单次 Agent run wall-clock timeout 秒数 | `120` |

设置面板会把个人配置保存到数据目录下的 `settings.json`。代码目录不保存个人棋局。

导入棋局的 Engine 结果保存在 `<DATA_DIR>/games/<game_id>/analysis.json`，不同复盘方的
结果同时保存在 `analysis/white.json` 或 `analysis/black.json`。每个 `critical_positions[]`
都内嵌 `facts`，包含三个局面 snapshot、实战/最佳着效果、有限变例结果、material/王安全/
活动性 delta，以及仅在证据充分时产生的 motif 和分类。相关接口：

```text
POST /api/games/{game_id}/analyze
GET  /api/jobs/{job_id}
GET  /api/games/{game_id}/analysis?review_side=white|black
POST /api/games/{game_id}/explanations
GET  /api/games/{game_id}/explanations?review_side=white|black
POST /api/training/attempt
GET  /api/training/hint
GET  /api/training/attempts
GET  /api/profile?days=7|30|0
DELETE /api/games/{game_id}
POST /api/data/engine-cache/clear
POST /api/agent/sessions
GET  /api/agent/sessions/{session_id}
POST /api/agent/sessions/{session_id}/context
POST /api/agent/sessions/{session_id}/messages
DELETE /api/agent/sessions/{session_id}
```

`POST .../explanations` 默认逐个生成所有关键局面，也可传
`{"review_side":"white","critical_id":"ply-33","force":false}` 只处理一处。输入未变化时
直接命中缓存；`force=true` 显式重生成。通过 schema、权威着法/分类和证据引用校验的内容才会
写入 `<DATA_DIR>/games/<game_id>/explanations.json`，各复盘方另存于
`explanations/white.json` 或 `explanations/black.json`。模型失败不会改动 `analysis.json`，也
不会让 Engine Review 不可用。

Retry 和个人题的每次提交都追加保存到 `<DATA_DIR>/history/attempts.jsonl`，记录原棋局、
`critical_id`、复盘方、所选 UCI、判定、提示次数和是否解决；旧版
`<DATA_DIR>/training/attempts.jsonl` 会继续被读取。已存在的 Stage 2 MultiPV/实战线会
直接复用；只有未被预分析覆盖的合法着法才会触发一次按需 Stockfish 分析。

分析成功后，`<DATA_DIR>/history/games.jsonl` 会按 `(game_id, reviewed_side)` 原子更新，
而不是重复追加同一盘。Personal coach 可按最近 7 天、30 天或全部历史聚合胜负、accuracy、
错误级别、Stage 2 category、阶段损失、开局和训练解决率；只有同类错误重复出现且占比或
累计损失达到阈值时才显示为弱点。每条弱点都可回到典型关键局面或直接进入个人训练。
删除单局会同步删除该局 artifact、索引和关联 attempt；清理 Engine cache 只删除可重建缓存，
不会删除棋局、解释、画像或 managed Stockfish。

设置页已有的本地模型地址和模型名会被 `auto` Provider 直接复用。连接远程兼容 API 时，
例如可在启动进程中设置：

```bash
CHESS_EXPLANATION_PROVIDER=openai-compatible \
CHESS_EXPLANATION_BASE_URL=https://api.example.com/v1 \
CHESS_EXPLANATION_MODEL=your-model \
CHESS_EXPLANATION_API_KEY=your-key \
.venv/bin/python -m server.web.runner
```

API Key 只用于后端 Authorization header，不返回浏览器、不进入 prompt，也不写入
`explanations.json`。未配置兼容 API 时，`auto` 会尝试现有 Claude CLI 登录；两者都不可用
时只返回解释服务错误，棋盘和 Stockfish 分析保持正常。

## 可选能力

Web 核心不依赖 MCP。只有需要保留上游 MCP 入口时才安装额外依赖：

```bash
uv sync --extra mcp
uv run python -m server.mcp_server
```

Grounded single Agent 也作为独立 optional extra 安装：

```bash
uv sync --extra agent
CHESS_AGENT_MODEL=your-model OPENAI_API_KEY=your-key \
  .venv/bin/python -m server.web.runner
```

Review chat 始终使用 Agent session/context/message API，不再回退到遗留 `/api/chat` 或 CLI
conversation state。后端未配置 SDK、模型或 credential 时，chat 会显示 Agent 不可用；棋盘、
Stockfish 复盘、历史和训练仍可正常工作。Agent checkpoint（包括 compact conversation
summary）、SDK conversation 和脱敏 run summary 分别保存在
`<DATA_DIR>/agent/sessions/`、`conversations.sqlite3` 和 `runs.jsonl`。Agent 不读取 CLI 登录态，
也不会把 endpoint 或 key 返回浏览器。遗留 `/api/chat` 仅供尚未迁移的其他功能使用。

AI 教练同样不是 Web 启动前提；没有模型时，Stockfish 复盘、棋盘、历史和训练功能仍可
工作。解释业务层通过统一 Provider 接口调用本地/远程 OpenAI-compatible API 或可选的
Claude CLI，浏览器不直接接触模型凭据。

需求入口见 [Requirement.md](Requirement.md)，架构范围见
[docs/requirements/01-base-and-architecture.md](docs/requirements/01-base-and-architecture.md)。
