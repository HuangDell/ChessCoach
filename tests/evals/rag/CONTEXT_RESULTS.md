# 已验证局面上下文对照（2026-09-14）

本轮完成 40 条检索，14 条带局面。完整棋盘事实直接追加到查询没有解决
P03/P04/P06 的已知证据漏检，因此当前不能据此把该模式作为生产默认。

## 实验口径

- 使用 `zh-en-v3-pooled-live-translation` 的相同 40 条问题、758 条既有判断。
- 固定上轮 40 条真实英文译文；本轮不重新翻译、不调用回答模型或 Stockfish。
- 复用 `index-v3-cu128` 的 670 段、4096 维 Qwen3-Embedding-8B 索引，CUDA 0、batch 1。
- 保持生产检索参数：dense/BM25 各 20 候选，RRF k=60，按正文 hash 去重，返回 5 段。
- 唯一查询变化是 `--context-mode verified_board_v1`：从初始 FEN 合法重放生成行棋方、
  将军状态、双方全部棋子位置和已提供的 SAN 历史。无上下文的 26 题不追加文本。
- 不注入候选着、开局标签、expected points、证据正文或参考答案；FEN-only 输入不编造历史。
  棋盘事实不等于 Engine 评价，也没有自动识别开局或推断最佳计划。

这是检索层的上下文消融，不是 Agent 端到端 benchmark，也不改变独立的 26 条 Agent suite。

## 结果

以下为相同旧标签下的 **known-direct Hit@5 下界**，分母为 36 条 supported 问题。
上下文新增 57 个未标注 query/chunk 对；没有把未知段落判为不相关，也没有对新池补标。
因此表格只能说明已知证据的排名变化，不能视为完整相关性评测后的质量差值。

| 检索方式 | 仅真实译文 | 译文＋棋盘上下文 | 新运行 Top5 未标注比例 |
| --- | ---: | ---: | ---: |
| BM25 | 63.89%（23/36） | 55.56%（20/36） | 22.0% |
| Dense | 83.33%（30/36） | 77.78%（28/36） | 3.5% |
| Hybrid | 83.33%（30/36） | 75.00%（27/36） | 11.0% |

Hybrid 未新增已知直接命中，失去 P05/P08/R01；Dense 失去 P08/R02。
Hybrid MRR@5 从 0.6653 变为 0.5671。40/40 检索成功，首题含模型加载约 8.43 秒，
其余 39 题检索耗时中位数约 92.7 毫秒。复用的译文耗时不属于本轮新发生的请求。

## 三题定位

`null` 表示未进入该路记录的前 20 个候选，不表示语料库不存在该段落。
下面排名取自完整 trace，而不是只看最终 Top5。

| 问题 | 已知直接证据 | 原 Dense 排名 | 加上下文 Dense 排名 | 两次 BM25 |
| --- | --- | ---: | ---: | --- |
| P03 | Chess Strategy，ordinal 48，`0e43a6b9…` | 18 | 10 | 均未进前 20 |
| P04 | ordinal 62，`9ea2578d…`；ordinal 72，`ee1f7956…` | 均未进前 20 | 均未进前 20 | 均未进前 20 |
| P06 | ordinal 65，`d829613c…` | 未进前 20 | 未进前 20 | 均未进前 20 |

**P03：召回已有目标，最终排序仍不足。** 目标正文直接讨论 c 兵支持中心推进、给象退路及马的
发展选择。但书中使用 `P-QB3`、`Kt-B3`、`P-Q4` 等描述记谱，问题和历史使用 c3、Nc3、d4。
上下文把目标从 Dense 第 18 位提升到第 10 位，证明它能影响表示，但没有进入最终前五。
重排现有候选对该题有可测的空间；增加候选数不是本轮这条证据的必要条件。

**P04：问题中的开局意向仍主导结果。** 已提供历史是 e4 c5 Nf3 Nc6，但最终五段中四段仍讨论
西班牙，另一段是无关残局。目标 ordinal 62 把西西里例子放在较长的卡罗康讨论末尾；
ordinal 72 讲偏离背谱时应用发展和中心原则。正文混合主题、错误继承的章节路径
（含 SCOTCH GAME）与记谱差异，是值得单独验证的语料因素。本轮没有证明是哪一个因素主导。
目标只能支持计划适应的一部分，不能把古书的绝对判断当作现代 Bb5 合法性或强弱结论。

**P06：目标在索引中，但查询没有定位它。** ordinal 65 包含与给定历史对应的旧式着法表，
并讨论用 a 兵挑战 b 兵的守兵链。该段反而成为本轮 P03 的 Hybrid 第一名，排除了这条证据
完全未入索引的解释。P06 自身的前五仍是其他局面的发展、弃兵或残局内容；仅重排当前 20＋20
候选无法找回这条未入池的已知证据。

三题的 Hybrid Top5 正文已逐段检查，未见针对各自核心问题的直接解释；这属于本次作者诊断，
不是新增冻结标签，也不是独立模型或人工验收。新候选仍保留 unknown 状态。

## 解释与下一步

证据支持的结论是：**完整棋子列表的朴素拼接不足以补齐上下文缺口**。棋盘词汇重复、
SAN 与旧式记谱不匹配、混合主题分块都可能影响结果。记谱说明段进入 P03/P06 的前五，
无关残局进入 P04/P06 的前五，与查询被这些额外词汇干扰的解释一致；尚未做组件消融，
不能把这一解释当作已证明的因果结论。

下一轮优先测试简短的合法历史/关键棋盘事实投影，与完整列表做固定标签对照。
同时检查章节边界与描述记谱的检索表示；不能为这三题手工填入答案关键词或伪造图中 FEN。
P03 可单独测候选重排；P04/P06 应先解决候选召回。任何新方案在宣称质量提升前，
需要补审新候选池并统一重评分。当前继续保持模型辅助评测，不要求人工验收。

## 复现与产物

在新的输出目录运行（输出不会覆盖已有结果）：

```bash
.venv/bin/python -m tests.evals.rag.runner run \
  --dataset-dir tests/evals/rag/datasets/zh-en-v3-pooled-live-translation \
  --data-dir reports/rag/index-v3-cu128 \
  --output-dir reports/rag/v3-en-translated-context \
  --translations-dir reports/rag/v3-live-translation \
  --model-path /path/to/Qwen3-Embedding-8B \
  --device cuda:0 --batch-size 1 --context-mode verified_board_v1

.venv/bin/python -m tests.evals.rag.runner diagnose \
  --dataset-dir tests/evals/rag/datasets/zh-en-v3-pooled-live-translation \
  --run-dirs reports/rag/v3-en-translated reports/rag/v3-en-translated-context \
  --query-ids P03 P04 P06 --output reports/rag/v3-context-diagnosis.json
```

`diagnose` 保存共用标签下的评分、目标段落完整候选排名及候选正文，便于重复检查。
普通 `compare` 仍拒绝不同上下文；显式 `--allow-context-change` 才允许上下文对照，且要求
问题语言、冻结译文 hash、问题 hash、corpus、索引及 embedding 一致。
`results.json` 保存实际追加后的查询及上下文，所有含问题/正文的报告继续 Git 忽略。

验证：临时 `CHESSCOACH_DATA_DIR` 下运行
`.venv/bin/python -m unittest discover -s tests/backend -p 'test_rag_*.py'`，28 项通过；
应用 `create_app()` 导入检查通过。测试使用 fake 检索器，无模型或网络调用。
全后端 `.venv/bin/python -m unittest discover -s tests/backend -p 'test_*.py'`
执行 411 项，4 项失败、1 项跳过；失败仍是此前的工具注册表、开局全历史识别、
portfolio 冻结报告和 explanation prompt 版本契约。未修改这些无关模块。
