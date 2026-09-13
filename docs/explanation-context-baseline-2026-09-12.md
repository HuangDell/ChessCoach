# Explanation context 基线与 snapshots 精简建议

记录日期：2026-09-12。本文只读分析本机已有 raw trace，没有调用模型，也没有复制棋局、prompt、
模型输出或个性化内容。用途是为后续 Explanation prompt 精简提供可复现的前后对比基线。

## 1. 数据范围与口径

数据来自 `<DATA_DIR>/agent/traces/` 中最新连续 8 个 Explanation 请求，UTC 时间范围为
`2026-09-12T13:27:23Z` 至 `2026-09-12T13:33:09Z`：

```text
20260912T132723.539874Z-explanation-ply-28-b9c726895dab
20260912T132758.484155Z-explanation-ply-36-9bef9c4a7290
20260912T133030.139668Z-explanation-ply-60-73a567f2a75a
20260912T133054.812344Z-explanation-ply-54-e9d84ac43ef3
20260912T133122.700805Z-explanation-ply-44-26aeb9861041
20260912T133226.852482Z-explanation-ply-26-8a68b62e14a6
20260912T133256.071545Z-explanation-ply-48-8bddca57927f
20260912T133309.925658Z-explanation-ply-46-dbfc1f0f4cd9
```

运行配置为 `api.deepseek.com`、`deepseek-flash`、`zh-CN`、personalization 开启。每个请求均为
相同参数的独立 `system + user` Chat Completions 请求，`temperature=0.2`。

字符数按生产代码相同的紧凑 JSON 序列化计算：`ensure_ascii=False`、`sort_keys=True`、
`separators=(",", ":")`。字符数不是 token 数，只用于无 tokenizer 时稳定比较字段体积。Token
统计直接读取 response `usage`。

将以上 8 个目录的 `001-request.json` 和 `001-response.json` 按目录及文件名排序，把相对路径
UTF-8 和原始 bytes 依次送入 SHA-256，得到数据指纹：

```text
0a4460a54433024844d3bd5664bde3e4ad4fe3381e88afcad27766d3cac43994
```

raw trace 只保留最新 20 个目录，后续可能被自动清理；本文不依赖这些文件永久存在。

## 2. 当前 token 与缓存结果

| critical id | input | cache hit | cache miss | output | reasoning |
| --- | ---: | ---: | ---: | ---: | ---: |
| `ply-28` | 7,809 | 0 | 7,809 | 7,188 | 6,285 |
| `ply-36` | 7,535 | 384 | 7,151 | 10,339 | 9,349 |
| `ply-60` | 7,427 | 384 | 7,043 | 5,363 | 4,448 |
| `ply-54` | 8,096 | 384 | 7,712 | 5,738 | 4,918 |
| `ply-44` | 7,916 | 384 | 7,532 | 13,671 | 12,587 |
| `ply-26` | 6,999 | 384 | 6,615 | 6,344 | 5,397 |
| `ply-48` | 9,992 | 384 | 9,608 | 2,655 | 1,905 |
| `ply-46` | 8,483 | 512 | 7,971 | 6,458 | 5,187 |
| **合计** | **64,257** | **2,816** | **61,441** | **57,756** | **50,076** |

总缓存命中率为 `2,816 / 64,257 = 4.3824%`。8 个 system prompt 完全相同：1,072 字符、
1,788 UTF-8 bytes，SHA-256 为：

```text
8b50fac200694506cd326d40e7347493716eaf6eafb277ef76c2833fbf7b101a
```

8 个 user prompt 的共同前缀只有 285 字符 / 365 bytes；规范化完整 HTTP JSON 的共同前缀为
1,471 字符 / 2,267 bytes。首个分叉位于 `allowed_evidence_refs`，因为位置相关的 motif evidence
与固定 evidence 一起排序。后面的 facts 即使结构相同，值也已不再位于可命中的共同前缀。

本批 7/8 响应通过 Explanation schema 和权威字段校验。`ply-48` 缺少必填字段
`why_it_looked_reasonable`，因此没有写入 artifact；后续优化除成本外也应比较有效响应率。

## 3. 当前 position_context 组成

8 个 `position_context` 合计 168,964 字符，单请求平均 21,120，范围 18,085–27,180。

| 字段 | 平均字符 | 范围 | 占 context |
| --- | ---: | ---: | ---: |
| `engine.facts` | 16,832 | 14,404–22,532 | 79.7% |
| `engine.facts.snapshots` | 9,802 | 7,421–13,121 | 46.4% |
| `engine.facts.move_effects` | 2,226 | 1,470–3,171 | 10.5% |
| `engine.variations` | 1,868 | 1,851–1,878 | 8.8% |
| `engine.facts.best_line_result` | 1,543 | 1,141–1,854 | 7.3% |
| `engine.facts.played_line_result` | 1,369 | 901–1,934 | 6.5% |
| `engine.position` | 780 | 772–785 | 3.7% |
| `engine.facts.opponent_direct_replies` | 741 | 547–1,193 | 3.5% |
| `allowed_evidence_refs` | 660 | 446–786 | 3.1% |
| `engine.relevant_memory` | 616 | 0–848 | 2.9% |
| `engine.facts.deltas` | 550 | 472–892 | 2.6% |
| `engine.facts.motifs` | 288 | 2–545 | 1.4% |
| `expected` | 158 | 128–183 | 0.7% |

表中 `snapshots`、`move_effects`、line results、deltas、motifs 均属于 `engine.facts`，不能与
facts 总数相加。少量 JSON key 和容器开销未单列。

### snapshots 内部组成

每个位置发送 `before`、`after_played`、`after_best` 三份完整快照。平均体积分别为 2,974、
3,355、3,432 字符。三份快照按字段合计如下：

| snapshot 字段 | 平均字符 | 占完整 context | 内容 |
| --- | ---: | ---: | --- |
| `safety` | 5,671 | 26.8% | attacked、undefended、hanging pieces 及攻守子列表 |
| `legal_tactics` | 1,342 | 6.4% | 所有合法 checks/captures |
| `structure` | 620 | 2.9% | doubled/isolated/passed pawns、open/semi-open files |
| `material` | 604 | 2.9% | 双方完整棋子计数、点数和 balance |
| `king_safety` | 548 | 2.6% | 双方王位、兵盾格和兵盾数量 |
| `phase` | 345 | 1.6% | 阶段及其判定输入；三份通常重复 |
| `fen` | 179 | 0.8% | 三个局面的 FEN；before 已与 `position.fen_before` 重复 |
| `mobility` | 69 | 0.3% | 双方合法着数量 |
| `turn` + `in_check` | 36 | 0.2% | 行棋方和将军状态 |

facts 随后又发送 `move_effects`、`deltas`、`played_line_result`、`best_line_result` 和 `motifs`。
这些派生结构已经表达了新悬挂子、失去保护、交换结果、子力差、活动性、王安全和主要战术，形成
明显重复。当前 prompt 实际上把适合审计和持久化的完整 facts bundle 直接当成了模型讲解视图。

## 4. snapshots 精简建议

`analysis.json` 和 Fact Extractor 的完整 snapshots 保持不变，只在 `build_request()` 中建立窄的
model-visible projection，避免影响 Engine facts、历史 artifact 和训练。

建议模型上下文固定保留：

```text
expected
position:
  fen_before, side, played_move, best_move
  classification, scores, win_percent, criticality
variations:
  played_line, best_line
  compact alternative candidates
facts:
  signals, primary_category, secondary_categories
  motifs, deltas, opponent_direct_replies
  every exact node referenced by included motifs
memory:
  compact relevant memory
```

用确定性 `snapshot_comparison` 代替三份原始 snapshots：

```text
before:
  phase, in_check
played_vs_before / best_vs_before:
  material_delta, mobility_delta, king_safety_delta
  new_hanging_own_pieces, newly_attacked_pieces, lost_defenses
  pawn_structure_changes, gives_check
```

这些值应由现有 snapshots、move effects 和 deltas 在 Core 中投影，不让模型自行比较或计算。
只保留发生变化的字段和非空棋子列表；`allowed_evidence_refs` 必须由实际发送的投影重新生成，确保
模型不能引用已裁掉的数据。

建议采用有界回退：

- 有 motifs 时，发送公共核心、全部 motifs 及其精确引用节点。
- 没有 motif 或分类证据稀疏时，额外发送 compact snapshot comparison，而不是恢复全部快照。
- 某类讲解确实依赖兵型、王安全或活动性时，只发送该类相关的 before/played/best 子字段。
- 若 projection 无法解析某条 evidence ref，应明确失败或回退到该引用所属的完整小分支，不能静默
  删除证据。

在本批数据上做的只读字符估算：

| 方案 | 平均 context 字符 | 相比当前 |
| --- | ---: | ---: |
| 当前 | 21,120 | 基线 |
| 仅移除原始 snapshots | 11,306 | -46.5% |
| 核心字段 + motifs 引用节点 + deltas | 6,340 | -70.0% |

最后一项是体积上限方向，不是已经通过质量验证的生产方案，也不能直接等同于 token 降幅。

## 5. 对模型讲解质量的风险

精简可能降低讲解丰富度，但风险主要来自“遗漏必要证据”，而不是 snapshots 越完整模型就越强。
本项目要求模型只解释确定性 Engine facts，不应把原始快照当作新的棋力或事实来源。大量受攻击棋子
列表和合法 tactics 也可能稀释主要错误信号。

高风险场景包括：

- 没有 primary motif 的位置；本批有 2/8 个，这些位置更依赖一般局面比较。
- 次要位置因素没有进入现有 motif/delta，例如某些兵型弱点或长期子力协调。
- transferable principle 需要棋盘背景，但 projection 只留下了数值差。
- 过度裁剪 line result 后，模型无法具体解释交换或强制变化的最终结果。

降低风险的方法：保留 `fen_before` 和完整合法 played/best SAN 线；用确定性、可读的变化摘要替代
原始三快照；按 motif/category 选择相关小分支；对无 motif 局面使用 compact comparison 回退。

上线前至少用相同固定棋局、相同顺序和相同 provider/model 比较：

- 合计 input、cache hit/miss 和 output/reasoning tokens；
- schema/权威字段/evidence/SAN 验收率；
- 是否正确指出核心问题、是否给出具体棋盘原因、是否编造未提供事实；
- 有 motif 与无 motif 两组分别统计；
- 人工盲评教学清晰度和 transferable principle，不只比较缓存命中率。

## 6. Memory 的缓存位置

当前 `relevant_memory` 由每个位置的 `primary_category` 和 current facts 单独检索，并不是固定玩家
画像。本批 8 个请求中有 2 个无 memory，共形成 5 种不同序列化结果；只有 exchange-sequence
memory 在 3 个位置完全相同。直接把这份 position-selected memory 放到最前面，可能让不同类别的
请求更早分叉，而且它当前只占 context 的 2.9%。

如果后续要利用 memory 的批内稳定性，应在一次批量生成开始时冻结一份紧凑、稳定排序的
`memory_snapshot`，并保证所有请求使用完全相同的 snapshot：

```text
fixed system/output contract
stable evidence contract
frozen compact batch memory snapshot
position-specific context and relevant_skill_ids
```

snapshot 可取本批目标 category 所需 memory 的并集，而不是发送整个玩家画像。紧凑项优先保留
`skill_id`、`status`、`confidence_level`、`evidence_count` 和 `evidence_refs`；当前自然语言 summary、
完整 examples 和结构化计数之间存在重复。只有真正冻结且逐请求字节一致的 memory 才应进入公共
前缀，否则继续放在动态位置更安全。

## 7. 后续对比要求

实现优化时提升 prompt version，并记录新 trace 的时间范围和相同口径指纹。对比应控制 game、
analysis artifact、provider、model、language、temperature、personalization、请求顺序和冷暖缓存状态。
供应商缓存是 best-effort；离线测试负责验证 projection、evidence 完整性和字段顺序，真实 token
命中与讲解质量仍需显式 live 对比。
