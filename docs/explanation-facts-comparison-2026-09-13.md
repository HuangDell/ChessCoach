# 共享 facts 投影：2026-09-13 配对实测

本次满足预设的相对验收标准：输入减少超过 30%，总 miss tokens、每条有效讲解的输入消耗未恶化，
生产有效率和局部内容检查指标未下降。**缓存命中率本身没有提高**。这是同一棋局的八个局面，
不是长期收益保证，也不是内容正确性的认证。机器可读脱敏统计见
[JSON 报告](explanation-facts-comparison-2026-09-13.json)。

## 实现与边界

`server/core/facts_projection.py` 是不读取配置/存储、不调用 Engine 的纯函数。Explanation 请求、
Ask Coach 初始上下文、`get_review_context` 模型返回使用同一投影；Agent 本地工具结果保持完整，
用于事实验收。删除 `_bounded_facts()`、原 Explanation 全量 facts 拼装和固定 evidence 基表。
不保留旧投影开关或旧 prompt builder。

默认省略三个完整 snapshots 和合法 checks/captures 清单，保留全部 motifs、分类证据、
move effects、deltas、双方变化线结果及对手 Engine 最佳回复。起始背景包含双方子力、活动性、
王安全、兵型、阶段、行棋方和将军状态；changed_values 表示一着后改变的叶子值，省略项表示
不变，列表保持原顺序。变化线结果/已有 deltas 表示其重放终点。所有 motif/classification 引用
均解析并恢复精确源节点，引用分支则保留整个分支，包括 `signals.allowed_mate` 列表成员。
源证据缺失明确失败；无 motif 局面不剥除背景。

Explanation prompt v4、Agent policy v6 保留固定规则公共前缀，局面/实际 evidence/memory 放末尾。
analysis 不迁移，旧讲解继续展示，重新生成按现有版本/hash 失效。公共 `model_usage.py` 分别解析
Chat Completions / Responses 原始 usage；Agent 使用 SDK preserve_raw_usage，避免把默认补零
当成供应商明细。缺失明细保持未知，命中率按完整明细输入加权；reasoning 已含在 output 中。

## 配置、冻结与预算

- Provider：配置的 DeepSeek OpenAI-compatible Explanation API；模型 `deepseek-flash`。
- 协议：Chat Completions；`zh-CN`；temperature 0.2；独立 system + user 请求；不复用会话。
- 修改前只读匹配原基线八个局面，其中两个无 motif；冻结完整 analysis、逐局面相关 memory、
  参数和 v3 请求。相关 memory 不重新检索、不合并为批次并集。
- 从同一冻结数据生成 v4 输入，引用审计全部通过，输入对象不变。动态上下文字符数
  169,436 → 98,680，减少 41.8%；字符数不是 token 数。
- 顺序：旧版八次、新版八次、新版八次、旧版八次；32 次真实调用，无模型重试。
- 初次沙箱执行有 32 条毫秒级连接失败记录、无 HTTP 响应；获准在沙箱外执行真实测量。
  这些连接失败单独留在临时目录，不混入供应商 usage/有效率统计。
- 供应商缓存无法清空，首次测量不是严格冷缓存。历史 4.38% 仅作参考。

冻结数据 SHA-256：
`7469991a7160104754b5464a404f7cbbdcc3ab0aa9619000a257468f0d5b54b0`

新版请求 SHA-256：
`165111049624acf2dc70a48b8bacca2052fd4ee208772b6382ff2e936f96bf72`

原始请求、HTTP 响应、个人数据、质量批注仅保存在 `/tmp/chesscoach-facts-comparison/`，仓库不含
上述正文或凭据。用户原有 `explanation-context-baseline-2026-09-12.md` 未修改。

## Token、缓存与生产校验

| 指标（各 16 次） | 旧版 v3 | 新版 v4 |
| --- | ---: | ---: |
| 总 input | 128,492 | 81,072 |
| cache hit | 88,704 | 43,390 |
| cache miss | 39,788 | 37,682 |
| 加权命中率 | 69.03% | 53.52% |
| 每条有效讲解 input | 8,566.1 | 5,067.0 |
| output（含 reasoning） | 136,364 | 126,038 |
| reasoning | 120,715 | 110,511 |
| 总延迟 | 651.980 秒 | 613.849 秒 |
| 生产校验通过 | 15/16 | 16/16 |

总 input 减少 **36.9%**，miss 减少 **5.3%**，每条有效讲解 input 减少 **40.8%**。
所有 32 次均有 input/cache/output/reasoning 明细。旧版唯一 schema 失败是 why_recommended
多出第六个空列表项；没有删项修复响应或重试。有效率指生产 schema/权威字段/evidence/SAN 前缀
校验，不等于正文准确率。

| 轮次 | input | miss | 加权命中率 | output | reasoning | 延迟（秒） | 有效 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 旧版首次测量 | 64,246 | 38,262 | 40.44% | 76,694 | 69,018 | 360.377 | 8/8 |
| 新版首次测量 | 40,536 | 36,312 | 10.42% | 61,750 | 53,987 | 302.409 | 8/8 |
| 新版重复轮 | 40,536 | 1,370 | 96.62% | 64,288 | 56,524 | 311.440 | 8/8 |
| 旧版重复轮 | 64,246 | 1,526 | 97.62% | 59,670 | 51,697 | 291.603 | 7/8 |

| 分组 | input 旧→新 | miss 旧→新 | 生产有效 旧→新 |
| --- | ---: | ---: | ---: |
| 有 motif（每版 12 次） | 99,424 → 62,970 | 38,880 → 29,436 | 11/12 → 12/12 |
| 无 motif（每版 4 次） | 29,068 → 18,102 | 908 → 8,246 | 4/4 → 4/4 |

无 motif 组的旧请求在首次测量时已高度命中供应商缓存，因此该组本次 miss **明显增加**。
不能把总体改善描述成每个局面都更省，也不能将新的公共前缀当成命中率保证。

## 匿名交错内容检查

32 条输出去掉版本标签、按固定顺序交错，使用同一完整源证据核对，再解码版本。
检查者是本次实施的 Codex，**不是独立人工盲评**；输出自身的 evidence 引用或措辞可能透露版本。
布尔评分针对主要教学结论、具体棋盘理由，以及实质性错误/超出证据的推论；不把轻微措辞问题
一律判为编造。schema 失败的输出也检查正文。

| 指标 | 旧版 | 新版 |
| --- | ---: | ---: |
| 核心问题解释正确 | 14/16 | 16/16 |
| 有具体且正确的棋盘理由 | 16/16 | 16/16 |
| 存在实质性证据外声明 | 5/16 | 3/16 |
| 有 motif：证据外声明 | 4/12 | 2/12 |
| 无 motif：证据外声明 | 1/4 | 1/4 |

两版仍存在质量风险：把等价交换说成白丢一子、把八半回合的临时子力差解释成整条线的损失、
把另一方的马步归给己方、混淆走前背景与最佳着后才打开的攻击线。新版部分回答能明确区分
检查点与后续回吃，但不能保证每次都做到。保留全部 motifs 和可解析引用也不能自动保证自然
语言因果推论正确；本次结论仅是该小样本上相对未下降，仍需要独立人工复核。

## 实际执行与剩余检查

```bash
# 修改前冻结；此时 builder 尚为 v3。现在运行 freeze 只会冻结当前版本。
.venv/bin/python -m tests.evals.explanation_compare freeze \
  --directory /tmp/chesscoach-facts-comparison

CHESSCOACH_DATA_DIR=/tmp/chesscoach-verification \
.venv/bin/python -m tests.evals.explanation_compare prepare \
  --directory /tmp/chesscoach-facts-comparison

CHESSCOACH_DATA_DIR=/tmp/chesscoach-verification \
.venv/bin/python -m tests.evals.explanation_compare run \
  --directory /tmp/chesscoach-facts-comparison

CHESSCOACH_DATA_DIR=/tmp/chesscoach-verification \
.venv/bin/python -m tests.evals.explanation_compare report \
  --directory /tmp/chesscoach-facts-comparison

CHESSCOACH_DATA_DIR=/tmp/chesscoach-backend-check \
.venv/bin/python -m unittest discover -s tests/backend -p 'test_*.py'

CHESSCOACH_DATA_DIR=/tmp/chesscoach-import-check PYTHONDONTWRITEBYTECODE=1 \
.venv/bin/python -c 'from server.web.app import create_app; create_app()'

CHESSCOACH_DATA_DIR=/tmp/chesscoach-portfolio-check \
.venv/bin/python -m tests.evals.run_portfolio --source deterministic \
  --output /tmp/chesscoach-deterministic-report.json
```

后端 346 项运行通过（其中真实 Engine 用例默认跳过 1 项）；应用导入成功；deterministic portfolio
37/37 task completion、37/37 tool selection、30/30 applicable grounding 通过。没有新增自动化
测试用例，只同步版本和投影结构相关的现有断言；新增的是显式 eval 命令。

已尝试以下固定棋局 Engine 测试，但沙箱内长时间无进展后被停止；沙箱外重跑的审批被用户中断。
因此**未确认该次 Engine pool 清理断言通过**，仍需在本机执行（setUp/tearDown 使用独立临时目录）：

```bash
CHESSCOACH_DATA_DIR="$(mktemp -d /tmp/chesscoach-engine-XXXXXX)" \
CHESSCOACH_RUN_ENGINE_SYSTEM=1 \
.venv/bin/python -m unittest tests.backend.test_engine_system
```

本次未改前端，未运行前端测试；未运行 Ask Coach live portfolio。对应本地需求文档已同步，但
`docs/requirements/` 是仓库现有忽略目录，投影约束同时记录在本报告和 README 中。
