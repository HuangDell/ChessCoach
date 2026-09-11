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

未载入棋局时，直接在标准初始局面移动棋子会自动进入 `Free analysis` 临时工作区。每步合法着都会
获得 Stockfish 评价，可逐步撤销或整体重置；当前单线路只存在于页面会话，不保存到 Games，也不
导出 PGN。可点击工作区中的 `Import PGN` 直接展开 Games 导入表单，粘贴或上传棋局；仅展开
表单会保留当前自由分析，打开或导入棋局会退出该工作区。

复盘页按棋盘、棋局导航、Analysis 排列。导航栏集中展示对局摘要、胜率图和 Key positions、
My mistakes、All moves；Games 始终通过顶部按钮打开抽屉，打开棋局后自动关闭。Analysis 默认
展示 Engine，可切换到 AI Coach 查看结构化讲解和对话；页签选择在本次页面会话保留，切换不会
发起模型请求或清除聊天草稿。Retry 临时替换分析页签，退出后恢复原选择。

超过 1400px 时三栏并排；901–1400px 时导航和 Analysis 排在棋盘右侧；900px 及以下依次纵向排列。
桌面三栏的导航默认约 400px，关键局面使用纵向列表和暗色滚动条。两处分隔条可拖动或用左右
方向键调整相邻栏宽度，并在本机记忆；双击棋盘分隔条恢复整体默认，双击导航分隔条恢复导航
默认宽度。窗口变窄时会限制宽度，纵向布局不显示分隔条。复盘中可进入 Retry 或 Practice。个人训练会复用
现有 Engine artifact；未覆盖的合法着才触发按需 Stockfish。每次 attempt 会投影为 canonical
observation，再确定性重建 recent/lifetime skill estimate。

AI Coach 中的 `Explain this position` 只生成当前局面的讲解；次级入口
`Explain remaining key positions (N)` 明确批量生成尚未讲解的关键局面，已有讲解可单独重新生成。
讲解保存到棋局，Ask Coach 用于继续追问或探索。打开页签不会自动发起模型请求。
讲解使用独立的 bounded OpenAI-compatible API provider，结果通过 schema、
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
SQLite conversation session 和非流式 bounded runs。response schema v4 要求模型把正文中的关键
棋类事实、个性化声明、合法性声明和降级状态镜像到 `grounding`；后端再用当前 FEN、Engine facts
和本次成功工具结果确定性校验。`suggested_actions` 直接使用按 action kind 区分的窄 JSON schema，
例如 `compare_move` 只能返回 `move_uci` 和可选 `fen`；未经验证的模型输出不会作为成功响应提交。

每次 run 都向 Agent 注册全部七个领域工具，不用问题关键词预先裁剪；当前 FEN、review ownership、
个性化开关、训练候选 allowlist 和调用预算仍由后端强校验。Free analysis 会用服务端生成的 opaque
reference 复用当前棋盘已完成的 live best-moves，浏览器不提交可被信任的评分或 PV；已完成的关键
局面继续使用保存的 Stage 2 artifact，不被交互式 live 搜索覆盖。

| 环境变量 | 用途 | 默认值 |
| --- | --- | --- |
| `CHESS_AGENT_ENABLED` | 启用 Agent surface | `1` |
| `CHESS_AGENT_PROVIDER` | schema 适配规则：`openai` / `deepseek` / `generic` | 无自定义 URL 为 `openai`，否则 `generic` |
| `CHESS_AGENT_MODEL` | 显式模型显示名/ID | 未设置 |
| `OPENAI_API_KEY` | 官方 OpenAI Responses credential | 未设置 |
| `CHESS_AGENT_BASE_URL` | 自定义 Responses-compatible base URL | 官方 OpenAI API |
| `CHESS_AGENT_API_KEY` | 自定义 endpoint credential | 未设置 |
| `CHESS_AGENT_MAX_TURNS` | 单 run 最大 turn | `4` |
| `CHESS_AGENT_MAX_TOOL_CALLS` | 单 run 最大工具调用 | `6` |
| `CHESS_AGENT_MAX_ENGINE_CALLS` | 单 run 最大 Engine 工具调用 | `2` |
| `CHESS_AGENT_TIMEOUT` | 单 run wall-clock 秒数 | `120` |
| `CHESS_AGENT_RUN_MAX_RECORDS` | `runs.jsonl` 最多记录数 | `1000` |
| `CHESS_AGENT_DEBUG` | 在终端输出 Agent SDK 活动和 grounding 拒绝原因 | `0` |

调试 Agent 对话时可运行：

```bash
CHESS_WEB_OPEN=0 CHESS_AGENT_DEBUG=1 uv run python -m server.web.runner
```

该模式默认隐藏模型和工具正文。若需要在本机终端查看完整模型输入、结构化输出和工具参数，可额外
设置 `OPENAI_AGENTS_DONT_LOG_MODEL_DATA=0` 与 `OPENAI_AGENTS_DONT_LOG_TOOL_DATA=0`。这些输出会
包含棋局、对话和个性化上下文，不应重定向到会被提交或共享的文件。

官方 OpenAI 和自定义 endpoint 都按当前后端配置直接创建 runtime，不要求本地 compatibility
certificate。live portfolio 用相同 runtime、生产工具路由、生产预算、生产响应验收、fixture tools、
dataset 和 scorer 衡量模型表现，但结果不控制 Agent 是否可用。

厂家配置只选择 schema 规则，不更改 model、URL、凭据或 Responses 协议。未知厂家值会明确报配置错误。
`openai` / `generic` 原样传递 SDK schema；`deepseek` v1 展开 `anyOf` 分支的本地 `$ref`，
移除 `minLength`、`maxLength`、`minItems`、`maxItems`，保留类型、可空性、联合分支和字段限制。
输出与工具参数仍执行原始模型的完整本地校验。切换适配器或升级规则后建议重跑 live portfolio，
以便比较模型表现。DeepSeek 规则尚需真实 endpoint 评测，离线通过不代表真实 endpoint 的表现。

Responses 请求已通过 `text.format.type=json_schema` 和 `strict=true` 要求原生结构化输出，
不是 Chat Completions 的 `response_format`。policy v4 明确要求完整 JSON、结果复用、预算拒绝后
停止重试，以及 suggested action 不得作为工具调用。

### 运行自定义 endpoint 模型 benchmark

先安装 Agent extra，再使用与 Web 进程完全相同的 model 和 base URL 运行 custom live portfolio。
runner 会完整读取项目根目录的 `.env`；Shell 中已 export 的同名变量仍优先。
配置好 `CHESS_AGENT_MODEL`、`CHESS_AGENT_BASE_URL`、`CHESS_AGENT_API_KEY` 和所需的
`CHESS_AGENT_PROVIDER` 后运行；Web 和 eval 使用相同的厂家配置解析逻辑：

```bash
uv sync --extra agent

.venv/bin/python -m tests.evals.run_portfolio \
  --source custom \
  --output reports/custom-responses.json
```

`--model` 和 `--base-url` 可用于临时覆盖配置。
运行期间会在终端显示每个 case 的开始、结果、错误码和耗时；JSON 报告仍单独写入 `--output`。
每个 case 的默认超时为 120 秒，可通过 `.env` 中的 `CHESS_AGENT_TIMEOUT` 调整。

每次 live eval 还会把每个 case 的所有 Responses 调用按顺序写入
`reports/custom-responses-traces/<run-timestamp>/<case-id>/`。`NNN-request.json` 和
`NNN-response.json` 分别是实际传输的原始 HTTP request/response body bytes，不解析、不重组字段，
因此完整保留模型输入、function-call 往返和模型输出。HTTP header 不属于模型输入输出且可能包含
Authorization，不写入 trace。可用 `--trace-dir` 修改 trace 根目录。

该命令会对 endpoint 运行 26 个 live case，可能产生模型调用费用。报告中的
`benchmark_passed`、`benchmark_checks`、`metrics` 和 `live_case_diagnostics` 只用于比较和诊断
模型，不写入生产数据目录，也不影响 Web 启动或 Agent availability。默认测试和 CI 不会自动执行
真实网络评测；base URL、model、Agents SDK、policy、response schema、dataset、scorer 或 schema
adapter 变化后可重跑，以获得可比较的新报告。

Credential 只由后端读取，不返回浏览器，不写 prompt、conversation、artifact、run log 或 eval
report。完整 benchmark 命令见 [Operations](docs/operations.md)。

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

其中 `custom-incompatibility-037` 是为保持 v2 冻结基线可比性而保留的历史 fixture，不再表示生产
runtime 有 endpoint 证书准入逻辑。

live endpoint 报告只把实际发送给模型的 26 个 baseline case 计入 live 质量和延迟指标，并额外
要求所有返回通过生产响应验收。11 个 hardening fake case 仍用于离线确定性回归，但在 live 报告中
明确标为 static reference，不混入 endpoint 指标，也不声称已由 endpoint 执行。

默认离线验证：

```bash
npm run test:frontend
.venv/bin/python -m unittest discover -s tests/backend -p 'test_*.py'
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -c \
  'from server.web.app import create_app; create_app()'
.venv/bin/python -m tests.evals.run_portfolio --source deterministic
```

真实 OpenAI/custom eval 是显式网络操作，不属于默认测试，也不把 credential 作为开发前提。当前
提交包含 deterministic v2 report；live report 需要对应 credential 显式生成，仅作为模型 benchmark。
详细命令、降级语义和清理规则见 [Operations](docs/operations.md)。架构决策见
[ADR](docs/adr/)，Agent 需求
与阶段状态见 [Agent requirements](docs/requirements/agent-design.md)。
