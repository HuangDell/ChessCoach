# Agent 输入缓存优化前基线

记录日期：2026-09-11。优化前代码 HEAD：`74ab24d568b61f5366e0dc4368a875249d968b6c`。
本记录为已有数据的只读统计，没有发起模型请求。账单与本地 trace 是两个独立数据源，不能假定
属于同一批调用。此次变更为 policy v4 → v5、缓存 usage 可选字段和 metrics 汇总；不改变模型、
工具列表、响应 schema v4、历史窗口或摘要算法。

## 用户提供的账单

以下保留输入顺序；原始数据没有模型、时间范围和分组标签，不推断 request_count 的归属。
price 币种未提供，按原始每 token 单价计算，不使用当前官网价格。

| type | price | amount |
| --- | ---: | ---: |
| input_cache_miss_tokens | 0.000002 | 16661 |
| output_tokens | 0.000008 | 26912 |
| request_count | — | 8 |
| input_cache_hit_tokens | 0.00000004 | 70784 |
| request_count | — | 4 |
| input_cache_miss_tokens | 0.000002 | 84525 |
| input_cache_hit_tokens | 0.00000004 | 55552 |
| output_tokens | 0.000008 | 41011 |
| input_cache_miss_tokens | 0.000001 | 68623 |
| output_tokens | 0.000004 | 50476 |
| request_count | — | 7 |

在条目完整且统计范围一致的假设下：

| 指标 | 值 |
| --- | ---: |
| 输入命中 tokens | 126336 |
| 输入未命中 tokens | 169809 |
| 输入总 tokens | 296145 |
| token 加权命中率 | 42.6602% |
| 输出 tokens | 118399 |
| 输入费用 | 0.27604844 |
| 输出费用 | 0.74528800 |
| 总费用 | 1.02133644 |
| 输出费用占比 | 72.9718% |

命中率 = hit / (hit + miss)，费用 = 各条目 price × amount 之和。request_count 不参与命中率计算。

## 仓库已有 DeepSeek trace

数据范围：

- `reports/ds-flash-traces/20260910T125237.704182Z/`
- `reports/ds-flash-v2-traces/20260910T132335.427081Z/`
- `reports/ds-flash-v2-traces/20260910T143622.135036Z/`

统计各目录全部 `*-response.json` 的顶层 `usage`。`001-response.json` 为 case 首请求，其余为
后续请求；这不是“缓存首次出现”或“用户跨轮追问”的分类。读取 `input_tokens`、
`input_tokens_details.cached_tokens` 和 `output_tokens`，缺失值按 0 处理，输入未命中为总输入减命中。
该历史统计无法区分字段缺失与显式零；后续比较应额外报告明细覆盖率。

| trace | 请求类别 | 请求数 | 输入 tokens | 命中 tokens | 未命中 tokens | 命中率 | 输出 tokens |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| ds-flash | 首请求 | 25 | 158795 | 21632 | 137163 | 13.6226% | 25158 |
| ds-flash | 后续请求 | 26 | 200441 | 192256 | 8185 | 95.9165% | 28774 |
| ds-flash-v2，两批合计 | 首请求 | 50 | 328840 | 187008 | 141832 | 56.8690% | 49275 |
| ds-flash-v2，两批合计 | 后续请求 | 34 | 255066 | 243328 | 11738 | 95.3981% | 46449 |

数据指纹：按相对仓库路径排序，对每个 response 文件依次向 SHA-256 输入 UTF-8 路径和原始 bytes：

- ds-flash：`42091c2abd6de24d6ace4a15254f0af037e865a0fc4a7bc08c18dc102fe25e84`
- ds-flash-v2：`53e0f3caa52c1e7461b86abd79f3a8900bd88e9c6cb9aaaf250fe204af600cfa`

观察：已有同一次 run 内的后续请求约 95%–96% 命中，首请求差异明显。两组数据不是受控 A/B，
不能将差异归因于代码版本；冷启动、跨批预热、模型配置、运行间隔和服务端落盘也会影响结果。

## 优化前结构与本次调整

优化前：固定规则与全部动态 `MODEL_VISIBLE_CONTEXT_JSON` 合并为 instructions，历史在其后。
摘要、局面或证据引用变化会改变 instructions 前缀。每次 run 获取最近 12 个 SDK items。
超过窗口后更新摘要；run usage 仅保存 requests/input/output/total，不能从 runs.jsonl 恢复缓存明细。

本次：instructions 固定；SDK 输入为历史 + developer 当前快照 + user 问题。最新快照覆盖历史
快照，确定性事实校验保持不变。developer 快照占一个历史 item，可能让 12-item 窗口更早滑动；
它也使 session 保存更多上下文。历史窗口优化不属于此次范围。

## 后续对比口径

- 固定模型、endpoint、schema adapter、数据集和问题顺序，记录时间间隔和冷/暖启动条件。
- 分别比较 case 首请求、run 内工具后续请求、同局面连续追问和切换局面后的追问。
- token 命中率使用合计 hit / 合计输入，不平均各请求百分比；同时报告缓存明细覆盖记录数。
- 新 run usage 使用 `input_cache_hit_tokens` / `input_cache_miss_tokens`。metrics 的 `input_cache`
  仅汇总两字段均存在的记录；旧记录保持未知，不计为零命中。SDK 自身可能把 Provider 缺失值转为 0。
- 当前 run 遥测仍以 SDK 正常返回的 usage 为来源；异常中断可能没有明细，不能当成完整费用账本。
- 同时记录输入/输出 tokens、模型请求数、每个成功回答费用、延迟和 grounding 通过率。
- 不将 Engine/tool 的 `cache_hit` 当作供应商输入缓存；不把命中率上升单独视为优化成功。
- 此次没有真实 endpoint 优化后数据；真实收益和 developer 消息兼容性仍需后续 live 对比确认。
