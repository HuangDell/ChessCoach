# Chess Review Coach - 产品需求总纲 v0.2

## 1. 文档目的

本文件是项目需求入口，只描述产品目标、全局原则、模块边界和开发顺序。

具体功能拆分到 `docs/requirements/`。开发某个模块时，应以对应子需求为主要依据，避免在一份超长 PRD 中同时修改所有上下文。

## 2. 已确认的产品决策

| 决策项 | 当前结论 |
| --- | --- |
| 产品形态 | 个人本地使用的完整 Web 项目 |
| 开源基线 | `Chess-analysis-mcp/tintins-chess-analysis` v2.0.2 |
| 后端 | Python 3.11+、FastAPI、`python-chess`、原生 Stockfish |
| 前端 | 沿用 base 的静态 Web UI、Chessground、`chess.js` |
| 前端策略 | 先迭代现有页面，不立即迁移 React/Next.js |
| Engine | 本地原生 Stockfish，Engine 是棋局判断的唯一权威 |
| AI | 后端调用可替换的 LLM Provider，只解释结构化 Engine 事实 |
| 数据 | 本地 JSON/JSONL 起步，确有需要后再迁移 SQLite |
| 账号与部署 | 不做账号系统，不做在线多用户部署 |
| MCP/Skill | 不作为产品必需能力，从 Web 主流程中解耦 |

## 3. 产品定位

开发一个面向国际象棋学习者的本地棋局复盘与训练系统。

产品解决的不是“哪一步是坏棋”，而是：

> 为什么这一步错了，我当时漏看了什么，以及以后如何避免同类错误。

核心闭环：

```text
导入棋局
  -> Stockfish 找到关键错误
  -> 程序提取客观棋盘事实
  -> AI 生成有证据的教学解释
  -> 用户在棋盘上重新尝试
  -> 保存错误类型和个人弱点
  -> 从自己的错误生成训练内容
```

## 4. 全局产品原则

### 4.1 Engine 是唯一棋力权威

Stockfish 或确定性棋盘计算负责：

- 最佳着和候选着；
- 局面评价和胜率变化；
- 是否存在将杀、赢子或强制变化；
- 用户着法的直接反驳；
- 关键局面的 Principal Variation。

LLM 不得自行判断上述内容，也不得推翻 Engine 结果。

### 4.2 AI 只负责解释和教学

AI 可以：

- 比较实战着法与 Engine 推荐着法；
- 将已验证变化解释成人类可理解的原因；
- 归纳错误类型；
- 给出可迁移的思考方法。

证据不足时必须明确表达不确定，不能补充未经验证的棋盘事实。

### 4.3 优先形成可用闭环

第一阶段优先完成：

```text
一盘棋
  -> 3-8 个真正重要的问题
  -> 每个问题有变化、有解释、可重试
```

不优先追求全部 Chess.com 标签、复杂图表或完整战术 motif 集合。

### 4.4 Base-first

优先复用 `tintins-chess-analysis` 已有能力，避免重写已经可用的棋盘、Engine pool、历史记录、变化探索和训练页面。

只有在现有实现阻碍核心需求时才替换模块。

## 5. MVP 范围

### 5.1 MVP 必须具备

- 粘贴 PGN；
- 上传 PGN 文件并支持多盘导入；
- 保留按 Chess.com 用户名同步公开棋局；
- 本地 Stockfish 全盘快速扫描；
- 自动选择约 3-8 个关键局面；
- 对关键局面执行 MultiPV 3 深度分析；
- 保存用户实战线、最佳线和候选线；
- 提取一组可靠的棋盘事实；
- 为关键错误生成结构化中文解释；
- 在 Web 棋盘同步显示着法、变化、箭头和评价曲线；
- 支持关键局面导航和 Retry；
- 缓存分析结果，重复打开不重新计算；
- 将棋局和错误记录保存在本地。

### 5.2 MVP 暂不新增

- 用户注册、登录和权限；
- 云端数据库；
- 在线多用户部署；
- 社交和分享系统；
- AI 对弈；
- 语音教练；
- 完整开局训练系统；
- 全量 tablebase；
- 精确 Elo 预测；
- React/Next.js 前端重写；
- 移动 App 或浏览器扩展。

Base 已经存在但不属于当前重点的功能，可以保留或暂时隐藏，不需要优先删除。

## 6. 系统边界

```text
Browser
  Static Web UI / Chessground
          |
          | JSON API
          v
FastAPI Web Backend
  Import Service
  Analysis Orchestrator
  Fact Extractor
  Explanation Service
  Local History Service
          |
          v
python-chess + Native Stockfish
          |
          v
Local JSON / JSONL / Cache
```

LLM 由 FastAPI 后端调用。浏览器不直接访问模型，也不保存模型密钥。

## 7. 统一数据产物

每盘棋使用稳定的本地 `game_id`，至少产生：

```text
data/games/{game_id}/
  source.pgn
  original.pgn
  metadata.json
  analysis.json
  explanations.json
```

其中：

- `source.pgn`：规范化后的 mainline 棋谱；
- `original.pgn`：导入时的原始棋谱、注释和 variation；
- `metadata.json`：headers、来源和逐 ply 的合法 FEN/UCI/SAN；
- `analysis.json`：Engine 结果、关键局面、变化和确定性 facts；
- `explanations.json`：AI 教学解释，不混入 Engine 原始计算；
- 全局历史记录保存在独立 JSONL 文件中，避免扫描所有棋局目录才能统计。

## 8. 子需求索引

| 文档 | 负责范围 |
| --- | --- |
| [01-base-and-architecture.md](docs/requirements/01-base-and-architecture.md) | Base 改造、Web 架构和模块边界 |
| [02-game-import.md](docs/requirements/02-game-import.md) | PGN 导入、规范化、稳定 ID 和错误处理 |
| [03-engine-analysis.md](docs/requirements/03-engine-analysis.md) | 两阶段 Stockfish 分析、关键局面和缓存 |
| [04-fact-extraction.md](docs/requirements/04-fact-extraction.md) | 确定性棋盘事实和错误分类证据 |
| [05-ai-explanation.md](docs/requirements/05-ai-explanation.md) | LLM 输入、输出、Provider 和防幻觉规则 |
| [06-review-web-ui.md](docs/requirements/06-review-web-ui.md) | 复盘页面、棋盘、时间线和交互状态 |
| [07-retry-and-training.md](docs/requirements/07-retry-and-training.md) | Retry、提示和个人错误训练题 |
| [08-local-data-and-profile.md](docs/requirements/08-local-data-and-profile.md) | 本地历史、个人错误画像和存储策略 |
| [09-delivery-roadmap.md](docs/requirements/09-delivery-roadmap.md) | 实施阶段、优先级和阶段完成标准 |
| [agent-design.md](docs/requirements/agent-design.md) | Agent 共享设计、Phase 0-5 需求与阶段完成标准 |

## 9. 模块依赖顺序

```text
Base Web 化
  -> 棋局导入
  -> Engine 分析
  -> Fact Extractor
  -> AI Explanation
  -> Review UI 串联
  -> Retry
  -> 本地画像与训练
```

下游模块不得绕过上游数据合同。例如，AI Explanation 必须消费 `analysis.json` 中的结构化数据，而不是直接解析前端文本或自行分析 FEN。

## 10. MVP 完成标准

输入一份有效 PGN 后，系统能够：

1. 获取并规范化棋谱；
2. 本地完成全盘扫描；
3. 找到约 3-8 个最重要的关键局面；
4. 为关键局面生成 MultiPV 3 和实战反驳线；
5. 保存 `analysis.json`；
6. 基于 Engine facts 生成结构化中文解释；
7. 保存 `explanations.json`；
8. 在 Web 棋盘同步展示关键局面、箭头、变化和评价曲线；
9. 允许用户在错误发生前重新走一步并获得反馈；
10. 再次打开同一棋局时直接读取本地结果。

满足上述闭环后，再进入跨局画像和个人训练增强阶段。
