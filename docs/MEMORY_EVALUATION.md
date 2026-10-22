# 记忆检索评测基线

## 范围与隐私

本次只交付评测脚本、真实标注集、本说明及首次基线输出，不修改生产检索、数据库或 Docker。

- 脚本：`verification/memory_retrieval_eval.py`，Python 3.12，默认词面模式只需标准库。
- 私有标注：`.tools/memory_export/recall_queries.jsonl`。
- 冻结结果：`.tools/memory_export/recall_baseline.json`，包含旧源码快照、逐题候选、证据、延迟、指标及指纹。
- 标注和结果都在已被 Git 忽略的目录，权限为 `0600`。脚本拒绝把这些文件放到其他目录或非忽略路径。
- 本文不包含真实问题、答案、原文、业务来源 ID 或实际 scope 标识。不要把私有 JSONL/JSON 粘入提交、Issue 或外部服务。

## 标注集

共 28 题：原文复述 4、同义改写 4、专名数字 4、多条证据 5、时间 4、无答案 3、跨 scope 隔离 4。21 题可回答，7 题不可回答；其中 6 道可回答题需要多条证据，包含一道时间串联题。

标注前以 SQLite 只读连接查看实际 memory/message，逐条人工核对正例。隔离题使用真实群、账号限定群、私有 scope 的对照，不构造生产记忆。无答案题记录了检索词、已查看记录范围和不能推断的原因。

每行包含：

- `id`、`question`、`scopes`、`category`、`answerable`。
- `relevant_sources`：严格的 `memory:<id>` 或 `message:<id>`。
- `expected_answer`、`required_terms`：人工答案标注，**不用于自动判答案正确**。
- `evidence_quotes`：人工查看过的原文片段，运行时检查它仍是对应记录的原文子串。
- 负例的 `negative_review`；隔离题的 `forbidden_sources`、`paired_with`。

验证器检查来源存在且未删除、正例来源可见、禁止来源不可见、引文匹配、隔离配对问题完全相同。时间题注明采用正文自然时间还是业务日，避免混淆迁移时间、`day_key` 和历史事件时间。

这是小规模、单人标注的回归集，不是统计代表性测试集。`relevant_sources` 是人工指定的证据目标，未穷举全库同义/重复记录；召回另一条未标注但足以回答的记录仍不计分。因此指标衡量**指定证据检索**，不等价于语义答案质量。历史助手的陈述也不被当作经外部核实的事实。

## 运行

首次运行已完成；下面是当时的命令，原输出存在时再次执行会拒绝覆盖：

```bash
.venv/bin/python -B verification/memory_retrieval_eval.py \
  --dataset .tools/memory_export/recall_queries.jsonl \
  --data-dir thingking/memory_data \
  --output .tools/memory_export/recall_baseline.json \
  --backend baseline
```

冻结源码复跑时使用新的输出名：

```bash
.venv/bin/python -B verification/memory_retrieval_eval.py \
  --dataset .tools/memory_export/recall_queries.jsonl \
  --data-dir thingking/memory_data \
  --output .tools/memory_export/recall_baseline_replay.json \
  --backend baseline --baseline-api search
```

`--baseline-api build_context` 单独评测旧上下文 API，不更改其默认窗口、画像数量、召回数量和评分参数。它按 `recalled → profile → bridge → working → other_channels` 导出证据；这是固定的评测顺序，不是服务承诺的全局相关性排名。状态对象没有可标注的 memory/message 来源，不参与检索计分。该模式的无答案误报是“上下文中有候选”的保守口径，尤其不能当作回答错误率。

hybrid API 已接入当前工作区：

```bash
.venv/bin/python -B verification/memory_retrieval_eval.py \
  --dataset .tools/memory_export/recall_queries.jsonl \
  --data-dir thingking/memory_data \
  --output .tools/memory_export/recall_hybrid.json \
  --backend hybrid
```

hybrid 适配点仍为 `MemoryRepository`、`MemorySettings`、`MemoryService(repo, index, settings, clock=...)`；调用 `service.search(query, scopes, k=10)`。如果重构也修改构造函数，只调整脚本的 `evaluate` 接线，不能更换基线源码或标注。

每个 hybrid 候选必须有 `evidence` 数组。优先解析其中的 `source_type/source_id/source_ref`，支持显式 `memory_id/message_id` 及数据库原有 `source_ref`；空数组可回退到候选的显式 ID。裸 `id` 不被擅自解释为 hybrid memory。候选元数据完整留在私有报告；冲突、无法解析或已删除来源不获分，并占据一个未解析证据位置。不会把摘要所关联的全部消息自动算作已召回。

`--vector` 使用复制后的 Chroma index 和本地已有 embedding 模型；不下载模型，不启动服务，不安装依赖。脚本会先调用 `reconcile_index()` 在临时副本中生成检索键向量，再评测；缺少依赖或索引查询异常会终止，不能悄悄退化为词面结果后声称完成向量评测。本次已用本地 `all-MiniLM-L6-v2` 完成端到端运行，结果见上文“混合检索结果”。

## 指标口径

固定 `k=1,5,10`。search 一次最多请求 10 个候选，按候选顺序及各候选内部 evidence 顺序展开并全局去重，再取前 k 个**证据**。所以 hybrid 的 evidence 数与 candidate 数可以不同，不把这两个排名单位混用。

- Evidence Recall@k：前 k 个证据与 gold 的交集大小 / gold 数量；仅对可回答题宏平均。
- MRR@k：第一个 gold 证据的倒数排名，未命中为 0；仅对可回答题宏平均。
- nDCG@k：二值相关性，`DCG = Σ rel_i/log2(i+1)`；理想分母使用 `min(k, gold数量)`，再对可回答题宏平均。
- All-Evidence@k：多证据题的全部 gold 是否同时命中；仅对 gold 数量大于 1 的题平均。
- 无答案检索误报率：不可回答题中，返回至少一个候选的比例。包含隔离负例，不加为本数据集调出的阈值。
- Scope 泄露率：候选/元数据声明 scope 或解析到的真实来源 scope 越界的题目比例。检查所有返回候选，不预先过滤后再计分；检索误报和泄露是不同指标。
- 答案正确率：逐题及汇总均为 `not_evaluated`。没有 LLM 调用、没有生成答案、没有关键词冒充正确率。
- 延迟：每题默认预热 1 次、测量 3 次；记录每次耗时及逐题 P50/P95，汇总分位数覆盖全部 84 次计时调用。分位数采用线性插值，预热、备份、初始化和指标计算不混入计时样本。

同一题重复运行的证据排序若变化，会记录 `unstable`。性能参数保持服务默认值；不要根据这 28 题调召回权重、阈值、缓存、窗口或预热次数后再当成独立验收。

## 冻结与安全

基线固定于提交 `f9bb5ae5415763430cb4c6e516a84ee9560da31a`。首跑从该提交提取 memory 包、配置及环境辅助源码；已核对它与初始已跟踪源码一致。期间出现的其他未跟踪生产模块不混入基线。

源码指纹：`5d0d5fdaa3b41ad3eb16a62def283073f0c26c6c40f2a5777b8a8dfcc92e95bd`。

后续 baseline 从原 JSON 内的 `frozen_source` 加载，并核对硬编码指纹，**不会调用升级后的实现冒充旧基线**。hybrid 才从当前工作区复制源码。参考结果默认取 dataset 同目录的 `recall_baseline.json`，可用 `--baseline` 显式指定。

每次运行：

1. 只读打开原 `memory.db`，使用 SQLite backup API 制作临时副本，包含已提交 WAL 内容。
2. 复制 index；其中 SQLite 文件同样使用 backup。拒绝数据/索引文件符号链接。
3. 所有服务实例只接收临时数据库和索引路径；不启动 worker、不同步身份、不记录业务消息。`build_context` 自身可能写会话，但只写临时副本。
4. 不加载项目 `.env`；设置模型离线变量，并以 Python 审计钩子阻止网络操作、后端子进程和临时目录外写入。原 DB/WAL/index 的运行前后内容 SHA256 必须相同，否则不发布结果；不把 SQLite 的易变 `-shm` 读锁文件计作内容。
5. 标注字节 SHA256 与数据库逻辑内容指纹必须与首跑一致。输出使用独占创建，拒绝覆盖。

比较要求数据集、语料、API、向量开关、k、设置、时钟和评测协议一致；向量模式还核对原始 index 指纹。`search` 与 `build_context` 的结果不能直接声称改善。延迟差值仅供描述，还应核对硬件、依赖、设备和负载；报告记录运行环境差异。

脚本不额外持久化完整数据库副本。生产数据变化后，应给 `--data-dir` 提供同一批保留的离线 DB/index 快照；不可删除校验、改 gold、覆盖基线或把旧数据写回生产来“完成比较”。新增/修正标注须作为新的评测版本，旧结果只和旧标注比较。

## 混合检索结果（2026-10-22）

当前工作区的新实现已可评测：`search` 返回候选及 `evidence`，原文消息和派生记忆都进入同一检索管线。使用同一 28 题标注、同一临时库副本，词面混合模式（不开向量）结果：

| 指标 | @1 | @5 | @10 |
| --- | ---: | ---: | ---: |
| Evidence Recall | 51.59% | 73.81% | **80.95%** |
| MRR | 0.5714 | 0.6563 | **0.6611** |
| nDCG | 0.5714 | 0.6569 | **0.6791** |
| All-Evidence | 0/6 | 3/6 | **4/6** |

相对旧基线（40.48% / 0.2722 / 0.3024，多证据 0/6），主要改善来自原始消息进入检索、中文 FTS 和 RRF 融合。P50 约 **18.3 ms**、P95 约 **23.4 ms**，仍为词面模式。无答案检索误报为 **7/7**，scope 泄露为 **0/28**；这说明“能找到相关证据”和“知道何时没有答案”仍是两个问题，不能用召回提升掩盖误报。

向量模式已用本地 `all-MiniLM-L6-v2` 和临时索引跑通，但同一批题的 Recall@10 为 78.57%、MRR 0.6468、All-Evidence 3/6、P50 约 30.9 ms。当前向量通道没有超过词面混合，因此默认保留混合模式，不根据这 28 题调阈值或权重。下一步应在更大的真实查询集上比较重排器、时间过滤和负例门控；在完成前仍以“检索指标”而非“回答正确率”报告。

复跑命令：

```bash
.venv/bin/python -B verification/memory_retrieval_eval.py \
  --dataset .tools/memory_export/recall_queries.jsonl \
  --data-dir thingking/memory_data \
  --output .tools/memory_export/recall_hybrid.json \
  --backend hybrid
```

向量模式需在已有本地模型环境的解释器中运行，并显式 `--vector`；不要把词面模式结果标记为向量结果。

## 首次结果

2026-10-22，macOS arm64、Python 3.12.14，`baseline/search`，**未启用 vector**。

| 指标 | @1 | @5 | @10 |
| --- | ---: | ---: | ---: |
| Evidence Recall | 23.81% | 33.33% | 40.48% |
| MRR | 0.2381 | 0.2595 | 0.2722 |
| nDCG | 0.2381 | 0.2770 | 0.3024 |
| All-Evidence | 0/6 | 0/6 | 0/6 |

无答案检索误报：5/7（71.43%）；其中普通无答案 3/3、隔离负例 2/4。Scope 泄露：0/28；未解析证据和不稳定查询均为 0。P50 **8.63 ms**、P95 **18.61 ms**。答案正确率：**not_evaluated**。

按分类的 Recall@10：原文复述 75%、同义改写 0%、专名数字 100%、多条证据 10%、时间 25%。这些只是小样本指定证据指标；特别是旧 `search` 不检索原始 message，消息题的失败是保留的真实能力缺口，不应删题抬高分数。

冻结文件 SHA256：`8c9500aa6ab6b3d50f70d7f9b052e1d8a327ad99520484ba28ec93aa65f8a49b`。

标注文件 SHA256：`951c3b299ddd98a6b753cd7c7a1d31ab79d923eb6aaa964baa0d079db254d549`。

验证记录：内置评分/适配/安全测试通过；冻结 baseline 复跑的逐题证据排序完全相同、检索指标差值全部为 0；`build_context` 实际运行通过且被正确标为与 search 不可直接比较。额外运行报告只存在于临时目录，验证后已清理。

```bash
.venv/bin/python -B verification/memory_retrieval_eval.py --self-test
```
