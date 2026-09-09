# Execution-Aware Tool Orchestration P0

该目录是隔离的离线研究设施，用于验证动态工具候选、执行状态投影和真实 Agents SDK 的
`FunctionTool.is_enabled` 接线。它不调用真实模型或 Stockfish，不写生产 conversation、run log、
learning 或个人棋局数据，也不用于签发 custom endpoint compatibility certificate。

## 运行

完整 deterministic state replay：

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m tests.evals.orchestration.runner \
  --mode replay --output-dir /tmp/chesscoach-orchestration-p0-replay
```

真实 SDK 加 scripted fake model 的多轮动态 schema 检查：

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m tests.evals.orchestration.runner \
  --mode sdk-fake --variant S01-main \
  --output-dir /tmp/chesscoach-orchestration-p0-sdk
```

对应自动化测试：

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest \
  tests.backend.test_agent_orchestration_contracts \
  tests.backend.test_agent_orchestration_state \
  tests.backend.test_agent_orchestration_sdk
```

`sdk-fake` 需要锁定的 `openai-agents==0.22.0`；SDK 缺失时相关测试明确 skip。`live-fixture` 和
`engine-system` 是后续阶段保留的 source kind，P0 会明确拒绝这两个值，不会悄悄改用其他来源。

## 数据与输出

- `tasks.json`：24 个 dev 任务、30 个状态变体及每个任务的预算和 fixture 来源。
- `fixtures.json`：冻结基线 fixture 的文件哈希、导入清单和 P0 专用 fixture。
- `gold.json`：独立于 runner 和候选提供器的可接受路径、依赖、证据与协议标签。
- `scripts.json`：确定性 fake 候选和动作序列，只用于接线与回放测试。
- `registry.json`：7 个生产工具的 DTO、权限、用途、前置条件及实际 SDK schema/description 哈希。

每次运行写出 `manifest.json`、`report.json` 和逐变体 `trace-*.json`。manifest 包含数据和实现代码
哈希、预算、registry/scorer/trace/state 版本及 fake model 标识；trace 包含逐轮 snapshot、事件、
参数、typed observation、资源、终态和去敏的 SDK 请求摘要。默认未指定输出目录时使用新的系统
临时目录。

这些分数只说明 deterministic fixture 与 P0 合同一致。它们不代表 live 模型质量、延迟、token
用量、价格或方法优于静态基线。
