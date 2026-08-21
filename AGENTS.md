# Chess Review Coach 开发约定

## 项目定位

这是一个仅在本机运行的单用户国际象棋复盘应用。主运行路径是一个 Python 进程：
FastAPI 同时提供 JSON API 和 `frontend/` 下的无构建 Web 前端。Stockfish 负责权威分析，
Agent/LLM 只基于已验证的 Engine facts 生成讲解；模型不可用时不得影响棋盘、分析、历史和训练。

开始改动前先阅读 `README.md`。涉及产品行为或数据结构时，再阅读对应的
`docs/requirements/` 文档；涉及前端时同时遵守 `frontend/modules/README.md`。

## 代码边界

- `server/core/`：领域逻辑、Stockfish 编排、事实提取、训练、历史和外部平台适配。
  Core 不应依赖 FastAPI，也不应把 HTTP request/response 类型带入领域函数。
- `server/core/explanation/`：讲解 schema、prompt 构建、Provider 和服务编排。Provider 是可选
  边界；不要让凭据进入前端、prompt、日志或持久化 artifact。
- `server/core/storage/`：持久化边界。个人数据只写到配置的数据目录，不写进源码树；多文件或
  索引更新应保持原子性，并兼容已存在的数据。
- `server/web/`：路由、请求校验、后台 job 和应用装配。路由保持薄层，把可复用业务逻辑放到
  Core；进程级资源通过生命周期统一创建和关闭。
- `frontend/modules/api/`：唯一知道 endpoint 路径和 HTTP method 的位置。
- `frontend/modules/core/`：无业务依赖的共享基础设施，不得反向导入 feature/controller。
- `frontend/modules/{games,puzzles,review,settings,system}/`：按功能拥有状态和渲染。controller 只
  协调本功能；跨功能调用通过 `frontend/modules/app.js` 注入窄接口，controller 之间不得互相导入。

保持依赖单向，优先扩展现有模块和接口。只有在确实减少重复、隔离外部依赖或形成稳定边界时
才新增抽象。不要为了单次调用创建 helper，也不要把相同业务判断复制到路由和前端。

## 实现原则

- 棋局事实必须由 FEN、合法着法重放和 Stockfish 结果确定性地产生。LLM 文本不能成为评分、
  合法性、分类或训练判定的事实来源。
- Stockfish、网络平台和 Explanation Provider 都视为昂贵或不稳定的 I/O 边界；业务代码通过
  小接口调用它们，测试中使用 fake/stub，避免真实网络和模型调用。
- 配置集中在 `server/config.py` 或现有 settings 边界，不在功能模块散落读取环境变量。
- 保留 `analysis.json`、`explanations.json`、history 和 attempt 的向后兼容。改变 schema 时应
  明确版本/默认值、同步生产者与消费者，并更新 README 或对应需求文档。
- 前端继续使用浏览器原生 ES modules，不引入 bundler 或框架，除非任务明确要求架构迁移。
- 异步 UI 必须处理取消和过期响应；复用 `createLatestRequestScope`，跨 timer/animation/response
  的流程保留 generation 检查。
- 共享代码按职责命名，函数输入输出保持清晰。大文件被修改时只做与任务相关的局部拆分，避免
  顺手重写整个模块。
- 错误信息应可操作；降级路径必须保留 Engine Review。不要静默吞掉会导致数据不一致的异常。

## 测试约定

每次修复 bug 都添加能先复现问题的回归测试。优先测试纯领域逻辑和模块契约，再测试路由/存储
集成；只有验证真实 Engine 接线时才启动 Stockfish。

反复使用的手工验证、临时脚本或系统检查必须沉淀到 `tests/`，不能长期只存在于 `/tmp`、聊天
记录或人工步骤中。通常按以下层次放置：

- `tests/frontend/*.test.js`：前端行为、请求契约、竞态和依赖边界，使用 Node 内置 test runner。
- `tests/backend/test_*.py`：Core 单元测试和 FastAPI 集成测试；使用临时 data directory，隔离
  全局配置、Engine pool、文件系统和后台线程。
- 需要真实 Stockfish 的系统测试应使用很短的固定 PGN/FEN、显式较低深度和独立标记/入口，
  结果断言采用合法性、schema 和稳定区间，不绑定易随 Engine 版本变化的精确 centipawn 数值。

测试不得读取或修改用户的真实 `CHESSCOACH_DATA_DIR`。涉及持久化时使用临时目录，并在导入
依赖配置的模块之前设置环境。外部 HTTP、Claude CLI/OpenAI-compatible Provider 默认 mock；
凭据不能成为测试前提。

## 验证矩阵

- 任意前端改动：`npm run test:frontend`
- Python 语法/导入检查：
  `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -c 'from server.web.app import create_app; create_app()'`
- 已有后端测试时：
  `.venv/bin/python -m unittest discover -s tests/backend -p 'test_*.py'`
- 改动 API、存储、分析 job 或跨前后端流程：除单元测试外，增加/运行对应 FastAPI 集成测试。
- 改动 Engine 接线、两阶段分析、facts 或训练判定：增加/运行固定棋局系统测试，并确认测试完成后
  Engine pool 和后台资源被关闭。

当前仓库已具备前端自动化测试，但尚未建立正式的 Python 测试集。新增后端行为时应同时建立
相应的 `tests/backend/` 覆盖，而不是继续只做 import smoke test。

## 常用命令

```bash
# 安装
uv sync
# 或
.venv/bin/pip install -r requirements.txt

# 启动（不要自动打开浏览器）
CHESS_WEB_OPEN=0 .venv/bin/python -m server.web.runner

# 前端测试
npm run test:frontend
```

开发和测试启动时优先使用临时 `CHESSCOACH_DATA_DIR`。Web 只能监听 loopback；不要为了测试
绕过 `server/web/app.py` 中的 Host/Origin 防护。

## 完成标准

交付前确认：改动保持模块边界；没有复制已有能力；异常与降级行为清晰；新增行为有与风险相称
的自动化测试；相关测试实际通过；没有把个人棋局、缓存、Engine 二进制、模型凭据或临时产物
加入仓库。最终说明应列出关键改动、验证命令，以及未能运行的测试或剩余风险。
