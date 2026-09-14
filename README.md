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
桌面三栏会锁定页面滚动，Analysis 栏独立滚动，因此棋盘和导航不会随长讲解或对话移动；导航默认
约 400px，关键局面使用纵向列表，所有页面与内部滚动区统一使用暗色滚动条。两处分隔条可拖动或用左右
方向键调整相邻栏宽度，并在本机记忆；双击棋盘分隔条恢复整体默认，双击导航分隔条恢复导航
默认宽度。窗口变窄时会限制宽度，纵向布局不显示分隔条。复盘中可进入 Retry 或 Practice。个人训练会复用
现有 Engine artifact；未覆盖的合法着才触发按需 Stockfish。每次 attempt 会投影为 canonical
observation，再确定性重建 recent/lifetime skill estimate。

AI Coach 对话按棋局隔离：打开不同棋局会自动开始新对话，重新打开同一棋局或切换复盘方会保留
当前对话并同步棋盘上下文；也可点击 Chat 标题旁的 `New chat` 随时清空当前对话并新建 session。
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
CLI。explanation prompt v4 将固定教练规则和输出契约保留为可缓存公共前缀，并把局面、合法 SAN、
允许的 evidence 与个性化 memory 集中在末尾 `position_context`。模型失败不会修改
`analysis.json`，也不会影响 Engine Review。

Explanation 和 Ask Coach 的初始上下文、`get_review_context` 模型返回使用同一个纯函数 facts 投影。
保留全部 motifs、分类证据、move effects、deltas、变化线结果和对手最佳回复；三份 snapshots
改为紧凑的起始背景和一着后的变化，按引用补回精确源节点（包含 signals 列表成员）。无 motif
局面同样保留背景。缺少被引用证据时明确报错，不回退完整输入；Agent 内部仍保留完整工具结果。
持久化 analysis 不迁移，旧讲解继续展示，再生成按现有版本/hash 失效。
显式对比命令及测量口径见 [Explanation 对比评测](tests/evals/README.md#explanation-共享-facts-对比)。
本次同源 8 局面、32 次真实调用，总输入减少 36.9%，供应商 miss tokens 减少 5.3%，新版生产校验
16/16 通过；加权命中率本身没有提高。小样本结果及内容质量风险见
[共享 facts 实测](docs/explanation-facts-comparison-2026-09-13.md)。

## Agent 配置

Agent 使用后端 Responses API、一个 Chess Coach Agent、typed function tools、structured output、
SQLite conversation session 和非流式 bounded runs。response schema v4 要求模型把正文中的关键
棋类事实、个性化声明、合法性声明和降级状态镜像到 `grounding`；后端再用当前 FEN、Engine facts
和本次成功工具结果确定性校验。`suggested_actions` 直接使用按 action kind 区分的窄 JSON schema，
例如 `compare_move` 只能返回 `move_uci` 和可选 `fen`；未经验证的模型输出不会作为成功响应提交。

OpenAI 适配器按职责分为 runtime 装配、`runtime_openai_tools.py` 工具执行和
`runtime_openai_session.py` SQLite 会话管理，SDK 类型不进入领域接口。
工具正常返回的 Engine/RAG 不可用结果继续支持降级回答；工具执行、facts 投影、序列化或审计的
内部异常会终止 run，返回 `agent_runtime_error`（HTTP 500，`recoverable=false`），失败阶段为
`tool_execution`。失败不提交会话或摘要，Engine Review 保持可用。诊断只包含工具名、处理阶段和
异常类型，不包含异常正文。run log 新增 `runtime_failure` 状态；未形成终态记录的内部失败以
`tool_execution_failed` 记录，每次调用最多一条终态记录。已有记录兼容读取，无需数据迁移；这些
内部错误不进入模型的 grounding 错误枚举。

每次 run 都向 Agent 注册全部八个领域工具（含 `search_coaching_knowledge`），不用问题关键词预先裁剪；当前 FEN、review ownership、
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
| `CHESS_AGENT_DEBUG` | 在终端输出带时间戳的脱敏 Agent 诊断 | `0` |
| `CHESS_AGENT_RAW_TRACE` | 将两条模型路径最近 20 个 HTTP trace 的原始 body 写入本地文件 | 未设置时继承 `CHESS_AGENT_DEBUG` |
| `CHESS_AGENT_CONTEXT_TOKENS` | endpoint/model 有效上下文容量；`0` 使用模型默认 | `deepseek-flash` 为 `1000000`，其他为 `128000` |
| `CHESS_AGENT_CONTEXT_TRIGGER_RATIO` | 上下文压缩软阈值比例 | `0.9` |
| `CHESS_AGENT_CONTEXT_TARGET_RATIO` | 压缩后输入目标比例 | `0.6` |
| `CHESS_AGENT_MAX_OUTPUT_TOKENS` | 每次教练调用的输出上限（包含 reasoning） | `8192` |
| `CHESS_AGENT_SUMMARY_MAX_OUTPUT_TOKENS` | 每次摘要调用的输出上限 | `8192` |
| `CHESS_AGENT_REASONING_EFFORT` | Agent 思考强度：`low` / `medium` / `high`，留空使用厂家默认 | 留空 |
| `CHESS_EXPLANATION_REASONING_EFFORT` | Explanation 思考强度；按 OpenAI/DeepSeek 适配器映射 | 留空 |

调试 Agent 对话时可运行：

```bash
CHESS_WEB_OPEN=0 CHESS_AGENT_DEBUG=1 uv run python -m server.web.runner
```

该模式输出 run/model/tool 生命周期、耗时、usage、失败阶段，以及不含字段值的结构校验路径；终端
始终不打印模型输入、输出或工具正文。`CHESS_AGENT_RAW_TRACE` 未设置时继承该 DEBUG 开关；显式
设为 `0` 或 `1` 时始终优先。raw trace 同时覆盖 Ask Coach 的 Responses 请求与 Explanation 的 Chat
Completions 请求。原始 body 会写到 `<DATA_DIR>/agent/traces/<timestamp>-<trace_id>/`，不包含 HTTP
header，两个功能合并只保留最近 20 个目录。这些文件可能包含棋局、对话、个性化 memory、模型输出、
工具数据和 reasoning，只能保留在本机。

官方 OpenAI 和自定义 endpoint 都按当前后端配置直接创建 runtime，不要求本地 compatibility
certificate。live portfolio 用相同 runtime、生产工具路由、生产预算、生产响应验收、fixture tools、
dataset 和 scorer 衡量模型表现，但结果不控制 Agent 是否可用。

厂家配置只选择 schema 规则，不更改 model、URL、凭据或 Responses 协议。未知厂家值会明确报配置错误。
`openai` / `generic` 原样传递 SDK schema；`deepseek` v1 展开 `anyOf` 分支的本地 `$ref`，
移除 `minLength`、`maxLength`、`minItems`、`maxItems`，保留类型、可空性、联合分支和字段限制。
输出与工具参数仍执行原始模型的完整本地校验。切换适配器或升级规则后建议重跑 live portfolio，
以便比较模型表现。DeepSeek 规则尚需真实 endpoint 评测，离线通过不代表真实 endpoint 的表现。

Responses 请求已通过 `text.format.type=json_schema` 和 `strict=true` 要求原生结构化输出，
不是 Chat Completions 的 `response_format`。policy v6 明确要求完整 JSON、结果复用、预算拒绝后
停止重试，以及 suggested action 不得作为工具调用。

Agent instructions 仅包含固定规则；每轮在最近历史之后追加服务端 developer context 快照和用户
问题。最新快照覆盖旧局面、证据引用和个性化状态，旧快照不作为当前棋盘事实。快照随 SDK session
一起保存；模型读取上次压缩边界之后的完整历史，不再固定截取 12 items。短程指代解析仍单独查询
最近 12 items。

每次模型调用前检查完整输入预算，包含工具定义、输出 schema 和本次工具结果。以单次响应的
`usage.input_tokens` 校准相同前缀的后续输入；没有匹配测量时采用 UTF-8 字节数加协议余量的保守
估算。缓存命中 tokens 仍占上下文；run 累计 usage 不能当作当前窗口长度。
预留 `R=max(10%×C,32768)`，达到 `min(trigger_ratio×C,C−R)` 时压缩，目标默认不超过 60% 容量。
容量和输出限制必须给预留留出空间；自定义 endpoint 的容量请按实际限制覆盖配置。

压缩复用当前 Agent endpoint/model，发起无工具、无会话的独立摘要请求，与主回答共享 run 超时。
摘要保留目标、约束、教学结论、未解决问题和历史引用，不承担棋类事实。取消原摘要总长 1500 字符
及逐条 360 字符截取，使用 token 输出预算。默认保留最近四个完整 turn，必要时减少；当前 turn
不裁剪，工具调用和结果不拆开。摘要只在历史前注入一次，不重复进入每轮快照。

原始 SQLite 历史保留；摘要、覆盖边界和计数基线随成功 run 在 generation guard 下提交。
失败、取消或 stale run 不推进边界；无法安全容纳请求时返回可重试的
`agent_context_budget_exceeded`（HTTP 413），可重试压缩、缩短问题或新建对话，Engine Review 保持可用。
旧 checkpoint 默认边界为 0，有原始历史时首次压缩重建旧摘要。具体实现与验证见
[v3 上下文预算](docs/agent-cache-v3-and-context-budget.md)。

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

## 本地书籍 RAG

把个人书籍放入 `<DATA_DIR>/knowledge/books/` 后，可用无模型、无网络的 CLI 构建本地 SQLite
corpus。首版支持 EPUB、UTF-8/UTF-8 BOM 的 `.txt`、`.md` 和 `.markdown`；PDF 与其他格式会明确
列为 unsupported，但在同时存在有效书籍时不会阻止构建。

```bash
python -m server.knowledge status
python -m server.knowledge build
python -m server.knowledge books
python -m server.knowledge inspect BOOK_ID --limit 5
python -m server.knowledge inspect BOOK_ID --blocks --limit 20
python -m server.knowledge export-image IMAGE_ID /path/to/data/diagram.png
```

以上命令默认使用 `CHESSCOACH_DATA_DIR`，也可在命令前增加 `--data-dir /path/to/data`。`build` 每次
完整扫描书籍目录，在同目录临时 SQLite 中解析、规范化、分块并完成外键与完整性检查，最后原子替换
`<DATA_DIR>/knowledge/corpus.sqlite3`；任一本受支持书籍失败或构建期间源文件变化时保留旧 corpus。
书籍正文、生成数据库和人工检查输出只留在本机数据目录，不进入仓库。

corpus v3（schema 3）保留 EPUB 正文中引用的原始图片资源、内联 SVG 标记、图号、图注、alt、
按阅读顺序排列的正文块，以及着法表格的单元格和 HTML。图片以 BLOB 存入 SQLite，重复资源只存
一次；`chunk_blocks` 关联检索分块与原始块，便于找回图片前后文。`inspect --blocks` 可查看这些
关联素材，`export-image` 可导出图片（不覆盖已有文件）。这一步不做 OCR 或棋盘识别，也不生成
FEN；图片在正文中保留未识别占位符。缺失或外部图片引用会使构建明确失败并保留旧 corpus。
旧 v2 corpus 仍可查看书籍和分块；使用新素材接口或重建索引前需运行 `build`，随后运行 `index`。
已有 benchmark 的 chunk 引用应绑定旧 corpus 快照，不能直接迁移到新分块。

当前已实现本地 Qwen3-Embedding-8B、LanceDB 向量与全文混合检索、RRF 排名融合，以及两条教练
路径和 Sources 展示。`build` 只构建语料及关联素材；实际检索还需要安装 `rag` extra、准备完整的本地
embedding 模型目录，并执行 `index`：

```bash
# 同时保留 Ask Coach 与 RAG 的可选依赖
uv sync --extra agent --extra rag

# 指向已准备好的本地模型；程序不会自动下载模型
export CHESS_KNOWLEDGE_MODEL_PATH=/path/to/Qwen3-Embedding-8B

.venv/bin/python -m server.knowledge index
.venv/bin/python -m server.knowledge status
.venv/bin/python -m server.knowledge search "如何识别对手的强制着法？" --limit 3
```

`index` 默认先重建 corpus，再生成新的 LanceDB generation，完成后原子切换 `active.json`。
源书籍更新后需再次执行 `index`；仅放入文件不会自动更新索引。`status` 不加载 embedding 模型，
不能单独证明模型可用或召回质量；`search` 才会执行实际 embedding 与混合检索。

| 环境变量 | 用途 | 默认值 |
| --- | --- | --- |
| `CHESS_KNOWLEDGE_ENABLED` | 启用本地知识检索，`0` 关闭 | `1` |
| `CHESS_KNOWLEDGE_MODEL_PATH` | 本地 Qwen3-Embedding-8B 模型目录 | `<DATA_DIR>/knowledge/models/Qwen3-Embedding-8B` |
| `CHESS_KNOWLEDGE_DEVICE` | embedding 推理设备 | `auto`：CUDA 可用时用 CUDA，否则 CPU |
| `CHESS_KNOWLEDGE_BATCH_SIZE` | embedding 模型 batch size | `4` |

Ask Coach 由 Agent 按需检索，每个 run 最多两次、每次最多五段；单局面 Explanation 在缓存未命中时，
由后端根据已验证 facts 固定检索最多三段，然后交给独立 Explanation Provider。两条路径共享
retriever，书籍只提供通用教学背景，不决定评分、合法性、分类或个人弱项。
语料和 embedding 推理在本机；选中的段落会随教练请求发送至所配置的模型 endpoint。

索引或 embedding 不可用时，Agent 返回可恢复的 `knowledge_unavailable`，Explanation 使用空知识
上下文继续生成；Engine Review 不受影响。`found` 只表示召回了候选，当前没有相关性拒答阈值或
reranker，也没有独立检索质量报告。完整流程、引用约束、缓存及已知边界见
[本地书籍 RAG 工作流程](docs/rag-workflow.md)。

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
<DATA_DIR>/agent/traces/<timestamp>-<trace_id>/*.json  # raw trace 开启时
<DATA_DIR>/knowledge/books/*                           # 用户手动放置的原始书籍
<DATA_DIR>/knowledge/corpus.sqlite3                    # 可重建文本语料快照
<DATA_DIR>/knowledge/models/Qwen3-Embedding-8B/         # 默认本地 embedding 模型目录
<DATA_DIR>/knowledge/active.json                       # 当前检索索引 manifest
<DATA_DIR>/knowledge/generations/<id>/lancedb/          # 向量、全文索引及来源元数据
```

`analysis.json`、`explanations.json`、history、attempt 和 learning schema 保持兼容。旧 analysis
cache 中的 `coach_ai_text` 可加载但被忽略。

Agent API 包括 session create/get/context/message/delete、validated start-training action，以及：

```text
GET    /api/agent/metrics?limit=100
DELETE /api/agent/runs
```

run log 只记录 version、run/session/generation、去敏 task/activity、model/endpoint type、usage、
status/error、失败阶段、脱敏 validation path、latency 和 tool 摘要。位置只保存 game/critical
reference 或 FEN fingerprint；不保存完整 prompt、FEN、PV、base URL、credential、reasoning 或
traceback。清理 runs 不会触碰 session、raw trace、learning、棋局、解释、attempt 或 Engine cache。

usage 新增可选 `input_cache_hit_tokens` / `input_cache_miss_tokens`，来自 SDK 的
`input_tokens_details.cached_tokens` 及输入总数之差。旧记录或无缓存明细时字段缺省，不视为零命中。
`GET /api/agent/metrics` 的 `input_cache` 返回有明细的记录数、hit/miss token 合计和加权
`hit_rate`（0–1；无可统计输入时为 null），与 `tools.cache_hits` 的 Engine/tool 缓存分开。
缓存明细使用 SDK 保留的供应商原始 usage；缺失时保持未知，不使用 SDK 补出的零值。
同一 run 中任一调用缺少明细时，该 run 不参与缓存命中率统计。reasoning_tokens 单列且已包含在
output_tokens 中，不能重复相加；这仍不是独立账单核验。
run record schema v1 保留，新增可选字段默认 null；response schema v4 不变。
usage 的 `context_last_input_tokens` / `context_peak_input_tokens` 记录教练请求的最后/峰值实测输入；
`context_estimated_input_tokens`、`context_before_tokens` / `context_after_tokens` 是估算，
`context_compactions` / `context_compaction_failures` 记录本 run 压缩结果。
`summary_requests`、`summary_input_tokens` / `summary_output_tokens` / `summary_total_tokens` 和
`summary_duration_ms` 单列摘要用量与耗时；摘要用量同时计入 run 总量，但不计入教练窗口峰值。
优化前对照数据见 [缓存基线](docs/agent-cache-baseline-2026-09-11.md)。

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

SQLite 会话测试停滞时可运行 `timeout 35s .venv/bin/python -m tests.backend.diagnose_runtime_session`。
该诊断仅使用临时数据库，逐步检查线程调度及 SQLite 读写/清理，不调用模型或 Engine；外层进程
超时也能终止事件循环本身被阻塞的情况。

### RAG 检索诊断记录

`CHESS_AGENT_DEBUG=1` 时，Agent、Explanation 和 CLI 的每次实际书籍检索会原子写入
`<DATA_DIR>/knowledge/traces/<timestamp>-<trace_id>.json`，保留最近 100 次记录。
此开关独立于 `CHESS_AGENT_RAW_TRACE`；关闭 DEBUG 时不写 RAG trace。

schema v1 记录原始与扩展查询、skill 参数、请求/实际 limit、索引和 embedding fingerprint、
向量维度、双路全部候选正文及来源、cosine distance/BM25 score（缺失时为 null）、RRF
排名与分数、重复正文和 limit 排除原因、最终段落，以及各阶段和总耗时。不会保存向量数值。
Agent 关联 run/session，Explanation 关联 game/critical，CLI 标记入口。命中讲解缓存而没有
执行检索时不生成记录；在检索器创建前不可用或被工具预算拒绝时也没有检索 trace。
失败记录保留已完成阶段、失败阶段及安全错误信息；记录失败不会影响原有检索结果或降级。
终端调试摘要只含 ID、状态、候选数量、耗时和文件路径。文件包含查询和书籍正文，只保留在
本机数据目录，不加入仓库。现有 HTTP trace 和 run log 的行为保持不变。
