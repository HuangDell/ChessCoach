# Execution-Aware Tool Orchestration：P0 实施计划

**日期：** 2026-09-08；P0 完成于 2026-09-09

**状态：** Implemented；离线 fixture 与 SDK fake-model 门禁已完成，未运行 live 模型实验
**上级计划：** [研究分析与实施计划](execution-aware-tool-orchestration-plan.md)  
**前置约束：** [README](../../README.md)、[Agent 共享设计](../requirements/agent/README.md)、
[现有 Eval](../../tests/evals/README.md)、[开发约定](../../AGENTS.md)

## 1. 目标与阶段边界

P0 的交付是一个能够验证动态工具编排接线的离线实验基础：在隔离 fixture 上，通过现有
OpenAI Agents SDK 跑出可回放、可评分、可检查逐轮工具集合的确定性轨迹。

P0 按任务与评分合同、状态投影与回放、SDK 动态工具接线三个步骤实施，每步有独立测试和交付。
预计约 4–6 人日，按实际进展调整；不以压缩验证来满足工期。

| 阶段 | 负责内容 | live pilot 安排 |
| --- | --- | --- |
| P0 | 24 个开发任务、scorer、状态投影、fixture executor、deterministic runner、SDK spike | 不要求真实模型调用；fake usage 仅验证计量合同。 |
| P1 | R0/A/B/B0 静态基线及独立词法对照，先完成原生 7 工具实验 | 方法实现后测真实 token、请求数及延迟，再冻结正式实验预算。 |
| P2 | 逐个加入 C/H，再加入 D；配对回放、端到端实验与消融 | 每加入方法补测其新增规划和检索成本。 |

方法 ID 以总计划第 8 节为准：B0 是 query＋初始 checkpoint，不是词法检索。
P0 的预设候选序列只用于接线测试，不命名为 D，也不产生方法优劣结论。

P0 不实现 embedding、planner、依赖图训练、压力工具库、多 Agent 或第二套 runtime。
不调整前端、生产默认路由、生产预算、个人 artifact 或兼容性证书。
本研究 P0 与既有已完成的 [Agent Phase 0](../requirements/agent/phase-0-contracts-and-baseline.md)
是不同阶段，研究数据集和 scorer 独立版本化。

## 2. 现有实现与最小改动落点

| 现有位置 | P0 复用方式与需要验证的边界 |
| --- | --- |
| [models.py](../../server/core/agent/models.py) | 复用 7 工具名称、权限、输入/结果 DTO、`ToolResult`、`AgentResponse`；不复制棋类 schema。 |
| [policy.py](../../server/core/agent/policy.py) | 保留 `allowed_tools_for` 作为 R0；复用 `validate_agent_run_result`。其当前只检查 run 固定允许集合，研究另加逐轮 snapshot 检查。 |
| [runtime_openai.py](../../server/core/agent/runtime_openai.py) | 扩展 `_LocalRunContext` 的研究注入点；复用工具装配、预算与实际 Engine 计数，不自写模型循环。 |
| [tools.py](../../server/core/agent/tools.py) | fixture 对齐 `execute`、执行元数据及成功结果合同；合法性、reference 和训练规则继续由现有 Core 确定。 |
| [sessions.py](../../server/core/agent/sessions.py) | 复用 generation guard 和隔离 conversation session，验证取消后无旧结果提交。 |
| [run_portfolio_live.py](../../tests/evals/run_portfolio_live.py) | 参考既有 fixture executor 和生产验收接线；研究 runner 不复用其签发 certificate 的执行入口。 |

实现文件按职责最小拆分：

| 位置 | 内容 |
| --- | --- |
| `server/core/agent/execution_state.py` | 框架无关的 run-local 状态、事件及确定性投影。 |
| `server/core/agent/routing.py` | 原生 capability 元数据、研究授权计算、窄候选接口及不可变 snapshot。 |
| `tests/evals/orchestration/` | 数据/trace/manifest 合同、开发 fixture、独立 gold 标注、scorer、runner 和使用说明。 |
| `tests/backend/test_agent_orchestration_*.py` | 数据合同、评分、回放、SDK 动态接线与隔离测试。 |

SDK 类型留在现有 adapter 内；Core 不导入 `tests/evals`。研究配置通过显式构造参数注入，
默认缺省时执行现有路径。研究运行仍接受生产 grounding 校验；实验评分不能取代该校验。
如果实现需要改变生产权限、DTO 或错误语义，先说明具体影响并由用户决策，再同步需求与版本。

## 3. P0.1：任务与评分合同

**入口：** 已阅读上述约束，记录当前代码版本和工作区变更状态。  
**交付：** 冻结的基线清单、原生 registry、24 个开发任务、gold 合同、scorer 及正反例测试。

### 3.1 冻结基线与 registry

记录现有 26 baseline＋11 hardening 的 case ID、dataset/scorer/policy/response schema/SDK
版本及文件 hash；保持原有 case、scorer 和报告不变。工作区非干净时同时记录 diff hash，
不能只用 commit 标识包含未提交修改的实验代码。

registry 只包含当前 7 个 callable schema。工具名称、参数与结果类型引用生产定义；冻结实际
SDK 暴露的 schema/description hash，P0.3 校验它们与登记内容一致。每项补充用途、前置条件、
产物类型和成本类别；`analyze_position.purpose` 仍是参数枚举，不计为新工具。

### 3.2 开发任务清单

使用人工短局及 synthetic fixture；有公开来源时保存许可与来源。每类 4 个任务，共 24 个
`task_id`。一个任务可包含多个状态变体；变体与 replay state 数另计，不膨胀任务数量。

| 切片 | 任务 ID 与内容 |
| --- | --- |
| 无需工具 / 已有证据 | N01：一般概念直接回答；N02：已有 facts 足以解释实战着；N03：已有证据足以解释最佳着；N04：缺失或含糊局面时澄清，不猜 FEN。 |
| 顺序依赖 | S01：profile → candidates → draft；S02：明确 skill → candidates → draft；S03：按用户过滤条件取候选再做草案；S04：已选关键局面 → review facts → 回答。 |
| 参数依赖与位置绑定 | A01：当前 FEN 的合法 what-if；A02：非法着被拒绝；A03：调用携带另一 FEN 被拒绝；A04：开局查询的 recent moves 与 checkpoint 一致/不一致。 |
| 结果改变后续需求 | O01：相同 profile 调用返回有/无可用 evidence；O02：相同候选调用返回非空/空集合；O03：候选覆盖/不覆盖请求的 skill；O04：profile 支持/不支持“近期改善”声明。 |
| 恢复与预算 | R01：可恢复 Engine 失败后的有界替代；R02：artifact/cache hit 与 miss 的实际 Engine 计数；R03：Engine 余额不足时复用已有证据或部分完成；R04：总调用/turn/deadline 到限后终止。 |
| 陈旧、取消与不一致 | X01：执行中 generation 改变；X02：请求取消；X03：候选引用在草案前失效；X04：storage consistency error 不提交成功结果。 |

O01–O04 各建立同 query、初始 checkpoint、调用名称/参数前缀的配对状态，只改变符合领域
合同的返回。O02 至少形成明确 routing 分支：有可用候选可创建草案；空集合不能创建草案。
O04 作为 grounding/response 分支单列，不冒充工具选择差异。其余配对在标注时按实际变化分类。
预算、cache、非法参数等变体另标因子，不混入 observation-only 因果对比。

全部任务标为 dev，保存 source game、近重复局面和模板家族分组键；同一配对不得跨 split。
P0 不创建冻结测试集，也不以这 24 个任务推断泛化或统计显著性。

### 3.3 运行数据与 gold 分离

| 合同 | 最小字段 / 读取方 |
| --- | --- |
| 运行输入 | task ID、query、显式 checkpoint、可用 fixture 引用、来源、预算；执行器使用。 |
| fixture 环境 | 工具输入匹配、typed result、实际调用元数据、故障触发点；仅 executor 持有完整表。 |
| replay 输入 | 初始 checkpoint＋已发生的有序事件；投影器只能看到截至当前决策的前缀。 |
| gold 标注 | 可接受动作集合、依赖 DAG、参数谓词、事实/reference 约束、目标产物与终止条件；仅 scorer 读取。 |

gold 动作区分 `tool_call`、`answer`、`clarify`、`partial` 和运行终止 `abort`。这些是研究评分标签，
不新增生产响应枚举。合法替代路径、前置依赖满足后的不同顺序、提前完成及 artifact 复用均可接受。
参数谓词使用固定类型，例如当前 FEN 相等、合法 UCI、已返回 reference 子集、数量上限；
不从 fixture 加载任意 Python 表达式。

scorer 接受运行记录与独立 gold，输出逐状态诊断及任务结果。下一工具正确不等于成功：
还必须满足参数、产物、事实及终止条件。预期取消/澄清另计 protocol correctness，不计完整成功。

最小评分集包含 Recall@K、Precision@K、AnyValid@K、下一动作/参数正确性、完整成功、协议正确性、
无效调用、冗余调用和逻辑/Engine 成本。分母沿用总计划第 9 节；无需工具状态不计满分 recall，
空候选与 policy-blocked 单列。故障保留首个可观测分歧和后续多标签，无法归因时明确 unknown。

**验收：**

- 24 个任务均可解析；FEN、UCI/SAN 重放、reference ownership 与 ToolResult DTO 校验通过。
- 每个任务有可接受轨迹；每类至少有一个只改变关键错误的负例，scorer 能拒绝该负例。
- 覆盖错误参数、缺失证据、提前停止、虚假成功、合法替代顺序和正确澄清。
- O02 配对的可接受下一动作确实不同；grounding 分支不计入 routing 配对结果。
- 原有 dataset、scorer 与 deterministic 26＋11 门禁通过。

未通过数据/评分验收前不进入 SDK 接线；先修正合同，避免用 runtime 行为反向定义正确答案。

## 4. P0.2：状态投影与回放

**入口：** P0.1 通过。  
**交付：** 状态投影器、fixture executor、候选接口、deterministic runner 和可重放报告。

### 4.1 状态与事件

采用小型 typed DTO，复用已有棋类类型；以类似 `project(state, event) -> new_state` 的纯函数更新。
时钟、预算变化和取消均作为显式输入，回放时不读取当前时间、真实 storage 或环境变量。

| 状态组 | P0 内容 |
| --- | --- |
| 身份 | state schema version、run/session ID、generation、state revision、最后事件序号。 |
| 目标 | user goal；plan/subgoal 可空。P0 不生成模型计划，计划也不能作为已完成步骤或事实。 |
| 执行 | tool/call ID、参数 fingerprint、执行状态、结果引用及顺序。 |
| 证据 | 已验证 facts/reference、局面 identity、来源及 Engine provenance。合法、已分析、证据足够分别表达。 |
| 观察与缺项 | typed observation 的有界字段投影；由 checkpoint/合同能确定的缺项。无法确定时留空，不读取 gold。 |
| 恢复 | 错误类别、失败参数 fingerprint、替代次数、无进展次数及终止状态。 |
| 资源 | 当前预算账本的投影：调用/Engine 余额、deadline、usage；未知 token 为 null。 |

事件至少覆盖初始化、候选提交、调用尝试/拦截、结果验证成功/失败、generation 变化、取消、
预算终止和最终验收。候选提交不改变执行事实 revision；单独记录 decision/snapshot ID，
避免记录 snapshot 又触发检索导致循环刷新。

状态更新规则：

1. 只有合同及引用校验成功的结果增加证据与完成产物；工具失败只更新错误、资源和恢复字段。
2. 同一事件 ID 重放不重复更新状态或扣费；模型再次发起同参数调用是新的尝试，仍计调用与成本。
3. 预算由现有 runtime 账本拥有，投影器只读事件中的资源快照，不建立另一套扣费规则。
4. 相同参数再次返回已有证据且无新产物/状态进展时记冗余；错误分类与有界替代分别记录。
   不以“命中 gold 下一步”定义进展。
5. 迟到的旧 generation 结果可进入取消诊断，但不能增加可用证据、写 conversation 或提交成功。
6. storage consistency error 必须保留真实失败类别；不包装成普通成功或伪造可用产物。

fixture executor 使用真实输入 DTO、确定性参数匹配和 typed result，支持合法参数范围；
不能只按调用序号返回预定成功值。成功候选及草案的来源约束必须成立，未匹配调用明确失败。
cache hit、Engine 实际调用数、失败调用与未知使用量分别表示，不按工具名称粗略计费。

### 4.2 授权与候选接口

保留生产 R0 的 `allowed_tools_for`；研究方法共享确定性授权集合 `P_t`，由个性化开关、
checkpoint scope、reference ownership、资源与当前可验证前置条件计算。query 相关性不作为
研究方法的共同权限上界。引用和训练业务判断调用现有检查，不复制一套领域规则。

候选接口只接收显式构造的 routing view 和授权 registry，返回有序工具名；不把完整 case、
fixture 表或 gold 交给检索器。P0 用固定返回值或按已发生事件变化的 fake 实现，缓存属于
snapshot 管理层；此时无需实现 embedding 或可扩展检索框架。

snapshot 保存 decision ID、state revision、授权工具、候选工具、过滤原因、检索版本和累计
已暴露工具。所有候选必须是授权集合子集，实际执行仍核对参数与 generation。
Engine 余额不足不等于所有分析 schema 都不可用：可走 artifact 的合法调用保留，具体参数的
Engine 需求沿用现有预估与实际结算。

### 4.3 最小 runner 与数据边界

P0 runner 提供固定状态回放入口，并在 P0.3 接入 SDK fake-model 运行。manifest 区分：

| source kind | P0 行为 |
| --- | --- |
| `deterministic` | 实现 state replay 和 SDK fake-model 两种执行模式，分别标注。 |
| `live-fixture` | 预留来源标识，P0 显式返回尚未支持；P1 再实现真实模型调用。 |
| `engine-system` | 预留来源标识，尚未实现时显式拒绝；真实 Engine 接线检查使用独立系统测试。 |

manifest 最少记录 experiment/task/variant/split/source kind、代码与数据 hash、registry/scorer/
trace/state 版本、seed、方法/模型/SDK/prompt 标识、预算及缓存配置。fake 模型标明为 fake，
provider 与价格留空；fixture 模拟 Engine 次数与实际启动 Engine 次数分开。

trace 保存事件顺序、snapshot、fixture 参数、结构化 observation、状态变化、最终响应尝试、
生产验收与 scorer 结果、usage/latency/error。每份文件携带 fixture 来源；模型侧只收到当时
可见的运行数据。评分在运行后执行，不把 scorer 反馈回灌模型或检索器。

输出默认使用临时实验目录；显式保留时只写配置数据目录下独立 research 目录或指定隔离目录。
会话和日志不共享生产 SQLite、`runs.jsonl`、learning 或个人 artifact。禁止写入凭据、原始
endpoint URL、authorization header、隐藏 reasoning、完整系统 prompt 或 traceback。
中断运行保留非成功状态；不完整事件尾部不能被读取为完整成功报告。资源关闭放在 finally。

**验收：**

- 同一输入与事件前缀重放后状态及评分一致；耗时/运行 ID 等非确定字段不参与相等断言。
- 成功、空结果、错误、重复事件、重复调用、无进展、cache、预算、取消与陈旧结果均有测试。
- 修改 gold 标签或未来 observation 不改变此前状态及候选；测试用隔离输入对象证明访问边界。
- 未匹配参数、非法 reference 和空候选不会静默变为成功；产物与真实调用前缀一致。
- 临时会话、运行输出、资源关闭均可检查；两个尚未实现的 source kind 明确失败。

## 5. P0.3：SDK 动态工具接线

**入口：** P0.2 通过。  
**交付：** 现有 adapter 的最小研究接线、真实 SDK＋fake model 自动化测试、逐轮请求证据及说明。

### 5.1 接线顺序

1. 在当前锁定的 `openai-agents==0.22.0` 上，使用公开 `FunctionTool.is_enabled` 接口；
   通过现有 `OpenAIAgentsRuntime` 和真实 `Runner` 运行，fake model 在 SDK 的模型边界记录请求。
   仅 mock 整个 Runner 的测试不能证明动态 schema 生效。
2. 研究注入点在 `_LocalRunContext` 持有状态和候选提供器；原生工具复用现有 `_sdk_tools`。
   没有研究注入时保持原有静态装配与响应合同。
3. 同一执行 revision 的候选单次计算、缓存为不可变 snapshot；多个 `is_enabled` 并发或重复
   调用读取同一结果。记录候选计算次数，不能每个工具回调都触发检索。
4. 模型响应中的同批工具调用绑定到产生该响应的 snapshot。保持现有串行工具执行；
   每次调用重新检查权限、参数、generation 和余额，批内结果统一进入下一决策的状态。
   第一调用完成后不能用新候选集合解释同批第二调用的权限。
5. 最终输出走 `validate_agent_run_result` 及研究逐轮调用检查。run 授权上界与逐轮候选记录
   分开保存；不靠改写固定 `request.allowed_tools` 掩盖历史调用或放宽生产验收。

P0 候选为空或失败时验证明确停止/澄清/部分完成，以及预算内最多一次扩大候选的共同回退合同。
扩大范围仍在授权集合内，使用独立 decision ID 记录首次与回退候选；不实现依据 gold 选工具
的恢复策略。scripted fake 的指定序列作为测试输入单列，不进入可用于方法比较的数据输入。

### 5.2 必须通过的接线检查

| 场景 | 自动化断言 |
| --- | --- |
| 多轮刷新 | profile → candidates → draft 的指定序列中，每次模型请求实际携带期望 schema；旧 schema 不被反复附加。 |
| 同轮 snapshot | 回调重复/并发只计算一次；同批多个调用引用相同 snapshot，预算仍逐次核对。 |
| 历史保留 | 下一轮不再暴露旧工具，但旧调用/结果仍能由真实 SDK 解析；累计已暴露工具数正确。 |
| 越权与旧工具调用 | 个性化关闭、越过 checkpoint、调用已移出候选的工具都不能产生有效证据；被拦截尝试纳入研究计数。 |
| 回退与无进展 | 首次空候选、工具错误、同参数无效重复不会无限循环；共同回退次数和总预算受限。 |
| 资源 | cache hit/miss、失败、总工具、Engine、turn 和 timeout 边界与现有账本一致；缺失 usage 为未知。 |
| 取消与一致性 | generation 更新、取消、候选失效和 storage error 后无过期成功响应或 conversation 提交；资源关闭。 |
| 最终验收 | 合法 response 通过；伪造 evidence、错误 reference、错误 full/partial 声明被生产验收或任务 scorer 拒绝并分别归类。 |
| 默认路径 | 无研究配置时工具暴露、生产日志字段及原 26＋11 评分不变。 |

SDK 缺失时 P0.1/P0.2 仍可独立通过；P0.3 的 SDK 测试可明确 skip，但不能据此标记 P0 完成。
若公开接口无法可靠绑定决策 snapshot、处理历史或取消，停止依赖该能力的工作，保留最小复现
及已通过结果，由用户决定适配范围；不导入 SDK `run_internal` 或增加手写 Responses loop。

## 6. 验证命令与提交顺序

以下命令用于回归验证。运行前在外层
设置临时 `CHESSCOACH_DATA_DIR`，并关闭自动开浏览器；测试 bootstrap 必须早于配置相关导入。
反复执行的隔离初始化和检查要沉淀到 `tests/`。

```bash
.venv/bin/python -m unittest discover -s tests/backend -p 'test_*.py'
.venv/bin/python -m tests.evals.run_portfolio --source deterministic
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m tests.evals.orchestration.runner \
  --mode replay --output-dir /tmp/chesscoach-orchestration-p0-replay
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m tests.evals.orchestration.runner \
  --mode sdk-fake --variant S01-main --output-dir /tmp/chesscoach-orchestration-p0-sdk
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -c \
  'from server.web.app import create_app; create_app()'
```

研究 runner 的输出合同和逐步测试命令见
[orchestration eval README](../../tests/evals/orchestration/README.md)。
涉及 session 提交、API 或 storage 接线时运行对应 FastAPI 集成测试；如实际改动 Engine/facts/
训练判定，则增加固定短局、低深度系统测试并验证 Engine pool 关闭。仅有模拟 Engine 计数不能
代替这类系统测试。前端不在本次范围；实际发生前端改动时运行 `npm run test:frontend`。

| 提交单元 | 预计工作量 | 可审查证据 / 进入下一步条件 |
| --- | --- | --- |
| P0.1 | 1.5–2 人日 | registry 和数据版本、24 任务及 gold、scorer 正反例报告、原门禁结果。 |
| P0.2 | 1.5–2 人日 | 状态转换测试、标签隔离测试、fixture 回放及 manifest/trace 示例、临时数据隔离结果。 |
| P0.3 | 1–2 人日 | 真实 SDK 的 fake-model 请求记录、动态/取消/预算测试、默认路径及必要集成回归。 |

每步只推进下一项已满足前置条件的工作。发现需要扩大生产改动范围、放宽事实验收或新增运行时
时停止并提交具体问题给用户决策；不把这些选择藏进常规实现。

## 7. P0 完成清单与 P1 交接

- [x] P0.1：24 个开发任务、7 工具 registry、独立 gold/scorer 通过验收。
- [x] P0.2：状态回放、参数匹配、错误/资源投影、标签与个人数据隔离通过验收。
- [x] P0.3：真实 SDK＋fake model 的逐轮 schemas、snapshot、历史、取消和预算检查实际通过。
- [x] 运行产物可追溯到版本与 manifest；失败/拦截/中断均保留，无未来标签输入。
- [x] 生产响应验收仍生效，现有 26＋11 门禁和后端相关回归通过，资源已关闭。
- [x] 生产默认路径、去敏日志、个人 artifact 与 certificate 未被研究入口扩展。
- [x] 交付真实复现命令、已知限制和未运行测试；不把 fake 数据解释为 live 质量或费用。

2026-09-09 的完成验证：P0 专项 19 项测试通过；后端全量 307 项通过、1 项按既有条件跳过；
原 26＋11 deterministic portfolio 全部通过；30 个 P0 replay 变体的 complete success、候选
Recall@K/Precision@K、下一动作、参数与协议指标均为 1.0；应用创建导入检查通过。未运行
`live-fixture`、真实模型或 `engine-system`，它们不属于 P0 完成条件；本次没有前端改动，未重复
运行前端测试。

交给 P1 的输入是上述已验证设施、开发任务、版本化 registry/scorer 和接线限制。
P1 再选择并实现静态检索配置，先做有上限的 live pilot，依据实测确定 K、正式实验规模与费用。
P0 全部完成不要求方法取得正收益，也不自动开启付费实验或生产晋升。
