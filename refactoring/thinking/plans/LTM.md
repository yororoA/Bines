# 目标

为当前 LangGraph Agent 搭建一个本地长期记忆系统（Long-Term Memory Architecture），采用“分层向量记忆”结构。

---

## 总体架构

系统采用：

1. STM（Short-Term Memory）
2. Summary Memory（摘要记忆）
3. Knowledge Memory（知识库记忆）
4. Persona Memory（用户画像记忆）

其中：

* STM 不进入向量库
* 后三者进入向量库
* 所有向量记忆支持 semantic retrieval
* retrieval 时按 memory type 分层召回，而不是单一 top-k

---

## 一、STM（短期记忆）

STM 使用：

* LangGraph Checkpointer
* SQLite backend

职责：

* 保存最近 raw conversation
* graph runtime state
* workflow state

特点：

* 不进行 embedding
* 不进入向量库
* 仅保存最近 4~10 轮消息
* token budget 控制在 4k~6k

STM 只负责：
“当前工作上下文”

而不负责长期记忆。

---

## 二、向量库整体设计

创建本地向量数据库。

要求：

* 支持 embedding similarity search
* 支持 metadata filtering
* 支持 memory type 分类 retrieval
* 支持后续扩展 rerank
* 支持本地持久化
* 支持 Python 调用

推荐：

* Chroma
  或：
* SQLite + FAISS

优先使用：

* Chroma（本地 persistent）
* sentence-transformers embedding

---

## 三、向量库 Memory Types

向量库内必须分为三类 memory：

```text
summary
knowledge
persona
```

每条 memory 必须带：

```python
memory_type
```

metadata。

---

## 四、Summary Memory（摘要记忆）

Summary Memory 的职责：

“阶段性事件记忆”

用于保存：

* 某阶段对话总结
* 某任务完成摘要
* 某 topic 的阶段性讨论
* 某 workflow 的抽象结果

不是简单聊天压缩。

而是：

semantic event abstraction。

---

Summary Memory 示例：

```text
用户设计了 LangGraph memory architecture，
决定：
- STM 使用 SQLite checkpointer
- summary 与 checkpoint 隔离
- summary 进入向量检索
```

---

Summary metadata 要求：

```python
{
    "memory_type": "summary",
    "topic": "...",
    "created_at": "...",
    "importance": float,
}
```

---

## 五、Knowledge Memory（知识库记忆）

Knowledge Memory 的职责：

“稳定知识”

用于保存：

* 技术知识
* 项目知识
* 用户长期工作知识
* conversation 中提炼出的 reusable knowledge

不是用户画像。

不是阶段性事件。

而是：

可复用知识。

---

Knowledge 示例：

```text
LangGraph SQLite checkpointer
需要安装：
langgraph-checkpoint-sqlite
```

---

Knowledge metadata：

```python
{
    "memory_type": "knowledge",
    "domain": "...",
    "source": "...",
    "created_at": "...",
}
```

---

## 六、Persona Memory（用户画像记忆）

Persona Memory 的职责：

“用户是谁”

用于保存：

* 长期偏好
* 长期习惯
* 技术栈
* 风格偏好
* 长期项目
* 稳定身份特征

必须是：

长期稳定信息。

不能保存：

* 临时问题
* 一次性消息
* 普通聊天

---

Persona 示例：

```text
用户长期开发 Agent 系统
用户偏好结构化输出
用户使用 LangGraph + FastAPI
```

---

Persona metadata：

```python
{
    "memory_type": "persona",
    "category": "...",
    "confidence": float,
    "stability": float,
    "updated_at": "...",
}
```

---

## 七、Retrieval Pipeline

实现：

分层 retrieval。

不要：

单一 top-k retrieval。

必须：

分别 retrieval：

1. Summary Memory
2. Knowledge Memory
3. Persona Memory

然后：

* merge
* rerank
* context assemble

---

推荐 retrieval 数量：

Summary：
top 1~3

Knowledge：
top 2~5

Persona：
top 2~4

---

## 八、Memory Retrieval API

实现统一接口：

```python
retrieve_memories(
    query: str,
    summary_k: int = 2,
    knowledge_k: int = 3,
    persona_k: int = 3,
)
```

返回：

```python
{
    "summaries": [...],
    "knowledge": [...],
    "persona": [...],
}
```

---

## 九、Memory Write Pipeline

实现：

memory judge / consolidation pipeline。

支持：

1. 判断是否值得写入 memory
2. 判断写入哪种 memory type
3. 自动生成 embedding
4. 自动写入向量库

不要直接把所有消息写入。

---

## 十、Memory Lifecycle

Summary Memory：

* 可衰减
* 可归档
* 可 merge

Knowledge Memory：

* 可更新
* 可去重
* 可修订

Persona Memory：

* 支持 confidence 更新
* 支持 preference 覆盖
* 支持长期稳定性评估

---

## 十一、推荐目录结构

```text
memory/
├── stm/
│   └── sqlite checkpoint
│
├── vector_store/
│   ├── chroma/
│   └── embeddings/
│
├── summary_memory/
├── knowledge_memory/
├── persona_memory/
│
├── retrieval/
├── consolidation/
└── rerank/
```

---

## 十二、目标

最终系统需要具备：

* 长期上下文连续性
* 分层语义记忆
* Retrieval-on-demand
* Token 可控
* Memory 不污染 STM
* 可扩展
* 可长期运行
* 可支持复杂 Agent Workflow
