# 中文查询 / 英文教材 benchmark 草案

本目录保存可复用的数据完整性校验入口。用户教材、corpus、真实问题及带来源的标注保存在用户
指定的本机数据目录，不作为公共 fixture 提交。

当前本机草案位于 `.chess-review/knowledge/benchmarks/zh-en-v1/`，该目录已被 Git 忽略。
`queries.jsonl` 只含查询 ID、中文原文、LLM 起草的英文参考译文和可用局面上下文；`labels.json`
单独保存答案要点、来源判断、类型和来源组。模型/翻译器不得读取 labels。

```bash
.venv/bin/python -m tests.evals.rag.validate_dataset \
  --dataset-dir .chess-review/knowledge/benchmarks/zh-en-v1 \
  --corpus .chess-review/knowledge/benchmarks/zh-en-v1/corpus-v2.sqlite3
```

校验器只读显式指定的 corpus，不加载项目全局配置，不调用 embedding、Engine 或网络。校验查询
身份、corpus 指纹、段落 hash、引用位置，以及合成局面的 FEN、SAN 重放、轮走方和候选着合法性。
通过不代表相关性标注、英文翻译或棋理已经经过人工验收。

这批标注绑定已冻结的 v2 语料（444 chunks）。主 corpus 的 v3 图片/表格保留改变了分块，不能用
旧标注直接评测新语料；需重新定位、复核证据并冻结新版本。

## 查询翻译约定

用户查询为中文，检索教材为英文。后续实际检索前调用 LLM 将中文问题及必要的已验证上下文转换
为英文查询；不得把答案、gold 段落、章节位置或相关性等级输入翻译器。保留询问语气、不确定性、
棋子颜色、SAN、格名和否定条件；不得把“是不是王翼弃兵”翻译成“这是王翼弃兵”。

草案的 `query_en_reference` 由当前助手起草，是可审查参考译文，不是已配置生产 endpoint 的翻译
调用结果，不含翻译延迟或 usage。生产检索器当前不自动执行这一翻译步骤，本任务不修改生产链路。
后续 runner 应记录实际英文查询、翻译模型及版本、耗时、usage，并缓存冻结译文以便 BM25、dense、
hybrid 使用相同输入。分别报告固定参考译文的检索效果与包含实时翻译的完整链路效果。

## 标注与评测边界

- 当前为候选草案，全部 `draft_pending_human_review`，不声称已有独立人工标注。
- 相关性 2 表示段落直接支持所问教学点，1 表示背景帮助，0 表示已检查的干扰项；未列出的段落
  是未判断，不是 0。来源图示缺失等诊断性引用保存在 `diagnostic_sources`，不是正例。
- `qrels_complete=false` 表示只标了已检查的候选，不能据此发布全库 Recall/Precision/nDCG。
  正式评测前混合各检索器候选建立盲审池，并补标相关段落；无法穷尽时明确称为 pooled judgments，
  报告未判断结果比例。现阶段只能用于数据准备及初步已知证据命中诊断。
- 合成局面使用合法 SAN 重放或显式合成 FEN，不能冒充真实用户上下文。棋盘状态由 python-chess
  验证，没有 Stockfish 最佳着、分数或胜负标签；需精确评分的后续样例另补 Engine facts。
- 历史教材中的绝对措辞、旧开局评价和描述式记谱不是现代 Engine 结论。评价 rubric 要区分一般
  教学原则、历史作者观点和当前局面事实。
- 两条真实问题已补入用户提供的 FEN，类型为 `real_position`；仅校验当前棋盘，不从 FEN 编造
  实际着法历史。参考答案和资料判断已随局面更新，仍未提供引擎最佳着或评分。
- 按 `group_id` 将相同教材概念及其局面变体放在同一划分；当前不预称 held-out 测试集。
  近似问题和同一证据组不得跨开发/测试，正式划分与人工复核后再冻结版本。
- 首轮检索对照为 BM25、dense、RRF hybrid，使用相同语料、译文、技能输入与 top-k，报告 @3/@5。
  证据不足样例单独评估，不放入普通 Recall 分母；生成正确性和引用支持关系另行评分。

检索指标 runner、实际 LLM 翻译调用和端到端生成评测尚未实现；此入口只校验候选数据完整性。
