# 中文查询 / 英文教材 benchmark 草案

本目录保存可复用的数据完整性校验入口。用户教材、corpus、真实问题及带来源的标注保存在用户
指定的本机数据目录，不作为公共 fixture 提交。

这是独立于 Agent 26 条 portfolio 的 **RAG Benchmark（40 条）**，不混入其指标。
v3 语料的首轮标签位于 `tests/evals/rag/datasets/zh-en-v2/`；实际翻译候选补审后的标签位于
`tests/evals/rag/datasets/zh-en-v3-pooled-live-translation/`（均为模型辅助复核，未人工验收）。
历史草案位于 `tests/evals/rag/datasets/zh-en-v1/`，这些目录均被 Git 忽略。
它从 `.chess-review/knowledge/benchmarks/zh-en-v1/` 整体迁入，数据版本及来源指纹不变。
新 checkout 不附带本地数据；校验器仍支持 `--dataset-dir` 指定其他本机目录。

```text
tests/evals/rag/
├── README.md                 # 独立套件入口
├── ANALYSIS.md               # 现状、缺口与完成顺序
├── validate_dataset.py       # 仅数据完整性校验
├── prepare_dataset.py        # 按显式审查决策物化新版标签
├── runner.py                 # 独立索引、40题检索、重评分
├── translate.py              # 显式调用模型并冻结实际英文查询
├── review_pool.py            # 新候选分片、显式模型评审结果合并
└── datasets/zh-en-v1/         # 历史本地素材；zh-en-v2 使用 corpus-v3.sqlite3
    ├── manifest.json
    ├── queries.jsonl         # 模型输入
    ├── labels.json           # gold，仅评分/审查读取
    ├── corpus-v2.sqlite3     # 冻结语料
    ├── review.md
    ├── translation_prompt.md
    ├── README.md
    ├── validation-notes.md   # 历史验证记录
    └── validation.json
```

后续检索报告、译文缓存和 trace 统一写到 `reports/rag/<run-id>/`（Git 忽略）。
准备阶段缺口分析见 [ANALYSIS.md](ANALYSIS.md)，首轮检索见 [RESULTS.md](RESULTS.md)，
最新实际译文对照见 [TRANSLATION_RESULTS.md](TRANSLATION_RESULTS.md)。

`queries.jsonl` 只含查询 ID、中文原文、LLM 起草的英文参考译文和可用局面上下文；`labels.json`
单独保存答案要点、来源判断、类型和来源组。模型/翻译器不得读取 labels。

```bash
.venv/bin/python -m tests.evals.rag.validate_dataset \
  --dataset-dir tests/evals/rag/datasets/zh-en-v1 \
  --corpus tests/evals/rag/datasets/zh-en-v1/corpus-v2.sqlite3
```

校验器只读显式指定的 corpus，不加载项目全局配置，不调用 embedding、Engine 或网络。校验查询
身份、corpus 指纹、段落 hash、引用位置，以及合成局面的 FEN、SAN 重放、轮走方和候选着合法性。
通过不代表相关性标注、英文翻译或棋理已经经过人工验收。

历史标注绑定 v2 语料（444 chunks）。新版已逐题重新定位和复核 v3 语料（670 chunks），
两套快照独立保留，不能交叉使用。新版 reviewer 明确记录 sub-agent 模型辅助审查来源。

## 运行新版检索 Benchmark

先 `uv sync --locked --extra agent --extra rag`，在可访问 GPU 的本机终端运行。
所有参数均为显式评测路径，不读取默认个人数据配置。首次构建使用新目录；已有成功索引直接复用：

```bash
.venv/bin/python -m tests.evals.rag.runner build-index \
  --corpus tests/evals/rag/datasets/zh-en-v2/corpus-v3.sqlite3 \
  --data-dir reports/rag/index-v3-cu128 \
  --model-path /path/to/Qwen3-Embedding-8B --device cuda:0 --batch-size 1

.venv/bin/python -m tests.evals.rag.runner run \
  --dataset-dir tests/evals/rag/datasets/zh-en-v2 \
  --data-dir reports/rag/index-v3-cu128 \
  --output-dir reports/rag/v3-en-reference \
  --model-path /path/to/Qwen3-Embedding-8B --device cuda:0 --batch-size 1 \
  --query-field query_en_reference
```

中文对照使用 `--query-field query_zh` 和独立 `--output-dir reports/rag/v3-zh-direct`。
已有输出目录拒绝覆盖。当前比较问题原文/固定译文，不拼接 FEN 上下文或技能词；
不是生产 Agent 自主构造查询或实时翻译的端到端成绩。
同一次生产混合检索的 trace 提供 BM25、dense 排名与最终 hybrid；各路按 text hash 去重取前5。
报告 @3/@5 的已知直接证据命中、包含背景的命中、直接证据 MRR、未判断比例及分题型结果。
失败题计为未命中，证据不足题不进入正例命中分母。不会输出全库 Recall/Precision。
耗时为生产混合调用的分阶段测量，首题包含模型加载；不能当作单独执行 BM25/dense 的延迟。

`review-pool.json` 将各路 top5 合并、去掉路由/排名并稳定打乱，供补审；
完整 trace 和 results 保留诊断信息，不能交给盲审者。补标后只重评分：

```bash
.venv/bin/python -m tests.evals.rag.runner report \
  --dataset-dir tests/evals/rag/datasets/zh-en-v2 \
  --output-dir reports/rag/v3-en-reference
```

`run.json` 记录查询 hash、索引/embedding 指纹及代码版本，`report.json` 记录评分用 labels hash。
专项验证（所有模型/检索调用使用 fake，数据使用临时目录）：

```bash
.venv/bin/python -m unittest \
  tests.backend.test_rag_dataset_validation tests.backend.test_rag_prepare_dataset \
  tests.backend.test_rag_runner tests.backend.test_rag_translation \
  tests.backend.test_rag_translated_retrieval tests.backend.test_rag_review_pool
```

## 查询翻译约定

用户查询为中文，检索教材为英文。后续实际检索前调用 LLM 将中文问题及必要的已验证上下文转换
为英文查询；不得把答案、gold 段落、章节位置或相关性等级输入翻译器。保留询问语气、不确定性、
棋子颜色、SAN、格名和否定条件；不得把“是不是王翼弃兵”翻译成“这是王翼弃兵”。

草案的 `query_en_reference` 由当前助手起草，是可审查参考译文，不是已配置生产 endpoint 的翻译
调用结果，不含翻译延迟或 usage。生产检索器当前不自动执行这一翻译步骤，本任务不修改生产链路。
`translate.py` 复用项目配置边界中的 Agent endpoint/model/provider/credential，发起独立 Responses
调用。只发送中文问题和固定翻译提示词；不发送参考英文、FEN、标签、答案或教材。运行前配置与
Agent portfolio 相同的模型环境变量；这是显式网络评测，默认单元测试不会调用模型。

```bash
.venv/bin/python -m tests.evals.rag.translate \
  --dataset-dir tests/evals/rag/datasets/zh-en-v2 \
  --output-dir reports/rag/v3-live-translation

.venv/bin/python -m tests.evals.rag.runner run \
  --dataset-dir tests/evals/rag/datasets/zh-en-v2 \
  --data-dir reports/rag/index-v3-cu128 \
  --output-dir reports/rag/v3-en-translated \
  --translations-dir reports/rag/v3-live-translation \
  --model-path /path/to/Qwen3-Embedding-8B --device cuda:0 --batch-size 1
```

已有索引直接复用，不重新编码书籍。两条命令均拒绝覆盖已有输出目录。
每题译文、耗时、usage、错误和模型/输入/prompt hash 原子保存，三路共用同一冻结译文。
检索输出复制译文快照并保存实际查询；查询集不匹配时拒绝运行。翻译失败计作失败/未命中，
不回退参考译文。翻译耗时单列；两个独立阶段逐题耗时相加只是延迟估算，不是实测端到端延迟。

新召回的未标注候选须先模型补审，再用同一新版标签比较三组。比较命令保留原报告，检查查询、
语料、索引、embedding 和上下文模式一致；输出记录统一标签 hash：

```bash
.venv/bin/python -m tests.evals.rag.review_pool prepare \
  --dataset-dir tests/evals/rag/datasets/zh-en-v2 \
  --retrieval-dir reports/rag/v3-en-translated \
  --output-dir reports/rag/v3-translation-review

# 三个审阅者分别填写 shard-A/B/C.json 的 reviewer、decisions、translation_reviews。
# 保留原始 items/translations 及其 hash，不提供参考译文、旧标签或检索排名。
.venv/bin/python -m tests.evals.rag.review_pool merge \
  --dataset-dir tests/evals/rag/datasets/zh-en-v2 \
  --corpus tests/evals/rag/datasets/zh-en-v2/corpus-v3.sqlite3 \
  --reviews reports/rag/v3-translation-review/shard-A.json \
            reports/rag/v3-translation-review/shard-B.json \
            reports/rag/v3-translation-review/shard-C.json \
  --output tests/evals/rag/datasets/zh-en-v3-pooled-live-translation

.venv/bin/python -m tests.evals.rag.runner compare \
  --dataset-dir tests/evals/rag/datasets/zh-en-v3-pooled-live-translation \
  --run-dirs reports/rag/v3-zh-direct reports/rag/v3-en-reference reports/rag/v3-en-translated \
  --output reports/rag/v3-translation-comparison.json
```

## 标注与评测边界

- v1 为 `draft_pending_human_review`；v2 为 `model_reviewed`，不声称已有独立人工标注。
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

检索诊断和实际 LLM 翻译调用已实现；生产路径自动翻译、局面上下文对照和端到端生成评测仍未实现。
当前按用户要求采用 sub-agent 模型评审，暂不安排人工验收；不将模型评审称为人工标注。
## 已验证局面上下文

已完成固定真实译文＋确定性棋盘上下文的 40 条对照，见
[局面上下文结果与 P03/P04/P06 诊断](CONTEXT_RESULTS.md)。
`run --context-mode verified_board_v1` 追加合法重放得到的棋盘事实；默认仍为 question-only。
新增 `diagnose` 可检查已知证据在完整候选中的排名。完整棋子列表未解决三题漏检，
新池还有 57 对未标注，本轮数字仅是旧标签下的已知证据下界。
