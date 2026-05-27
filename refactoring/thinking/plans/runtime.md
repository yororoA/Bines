# 目标

你正在为一个基于 LangGraph 的多 Agent Runtime 系统设计「Conversational Memory Layer」。

目标不是简单聊天记录，而是构建一个独立于 Workflow(GraphStatus) 与 Runtime(EventBus) 的 Cognitive Memory System，用于维持：

* 人格一致性（Persona Consistency）
* 对话连续性（Narrative Continuity）
* 用户长期偏好（Long-term User Modeling）
* 多 Agent 共享上下文（Shared Session Context）
* ReplyAgent 的统一人格出口
* Manager 的长期目标理解
* AdvanceReply / FinalReply 的一致表达

请基于以下架构要求，自动生成完整的数据结构、存储设计、检索机制、上下文注入策略以及生命周期管理方案。

---

## 一、整体架构目标

系统存在三套状态系统：

#### 1. Workflow State（GraphStatus）

负责：

* tasks_demanded
* tasks_ended
* artifacts
* retry metadata
* dependency graph
* workflow semantic state

特点：

* durable
* checkpointable
* replayable
* deterministic

注意：

GraphStatus 不负责 conversational memory。

---

#### 2. Runtime State（EventBus）

负责：

* progress events
* tool execution signals
* runtime operational status
* performer execution progress

特点：

* ephemeral
* high-frequency
* non-persistent

注意：

EventBus 不负责长期记忆。

---

#### 3. Conversational Cognitive State（本次需要设计）

负责：

* persona
* STM(short-term memory)
* LTM(long-term memory)
* user preference modeling
* already_said tracking
* narrative continuity
* conversational consistency
* multi-agent shared context

这是本次重点。

---

## 二、系统组件关系

系统中存在：

#### ManagerAgent

职责：

* task planning
* orchestration
* decomposition
* dependency management

需要读取：

* 用户长期目标
* 项目历史
* 工作流历史
* 长期用户偏好

---

#### Performer Agents

职责：

* tool execution
* code execution
* retrieval
* reasoning

要求：

* 尽量最小化上下文注入
* 不直接读取完整 conversational memory
* 只读取 task-specific context slice

---

#### Coordinator

职责：

* runtime orchestration
* event aggregation
* advance reply triggering

特点：

* 本身不具备人格
* 不维护 memory
* 只负责调度与聚合

---

#### ReplyAgent

职责：

* 所有用户可见输出
* advance_reply
* final_reply
* clarification
* retry explanation
* UX continuity

要求：

ReplyAgent 必须：

* 共享统一 persona
* 共享 STM/LTM
* 保持 narrative continuity
* 避免重复表达
* 避免人格漂移

ReplyAgent 是唯一用户可见人格出口。

---

## 三、需要生成的内容

请完整设计以下内容：

---

#### 1. Memory Layer Architecture

输出：

* 分层结构
* 各 memory 的职责
* memory 之间的依赖关系
* session context flow
* retrieval flow
* injection flow

要求：

必须明确区分：

* Workflow State
* Runtime State
* Conversational State

---

#### 2. 数据结构设计

请生成：

###### PersonaState

包括：

* tone
* style
* verbosity
* role identity
* speaking habits
* interaction strategy

---

###### STMState（短期记忆）

包括：

* recent dialogue
* active topics
* unresolved questions
* temporary goals
* already_said tracking
* recent emotional / interaction state

要求：

支持自动摘要压缩。

---

###### LTMState（长期记忆）

包括：

* long-term goals
* project history
* recurring interests
* user preferences
* stable interaction patterns
* persistent semantic knowledge

要求：

支持：

* vector retrieval
* semantic clustering
* memory scoring
* forgetting strategy

---

###### NarrativeState

包括：

* current conversational arc
* already explained concepts
* current workflow phase
* conversation continuity metadata

目标：

避免：

* 重复解释
* 人格断裂
* narrative reset

---

#### 3. Memory Storage Design

请设计：

###### Memory Store 分层

例如：

* Redis
* SQLite/Postgres
* Vector DB
* Hybrid Store

并说明：

* 哪些 memory 适合持久化
* 哪些适合缓存
* 哪些适合向量化
* 哪些适合 KV

---

#### 4. Retrieval System

请设计：

###### ReplyAgent Retrieval

应该如何：

* 获取 persona
* 获取 STM
* 获取 narrative state
* 获取 already_said
* 获取 relevant LTM

---

###### ManagerAgent Retrieval

应该如何：

* 获取长期目标
* 获取 workflow history
* 获取项目上下文
* 获取用户长期偏好

---

###### Performer Retrieval

应该如何：

* 最小化 context
* task-oriented retrieval
* 避免 memory pollution

---

#### 5. Context Injection Strategy

请设计：

###### Prompt Assembly Pipeline

包括：

* system prompt assembly
* memory injection priority
* token budgeting
* dynamic compression
* memory ranking
* context window management

---

###### 不同 Agent 的注入差异

明确：

* ReplyAgent 注入哪些 memory
* Manager 注入哪些 memory
* Performer 注入哪些 memory

---

#### 6. Memory Lifecycle

请设计：

###### STM -> Summary -> LTM 的流转机制

包括：

* summarize trigger
* semantic extraction
* deduplication
* consolidation
* importance scoring

---

###### Forgetting Strategy

包括：

* low-value pruning
* recency decay
* semantic redundancy removal

---

#### 7. Multi-Agent Shared Context

请设计：

* Shared Session Context
* Agent-specific Context View
* Context Isolation
* Permission Boundaries

要求：

避免：

* Performer 获取过量 persona memory
* ReplyAgent 获取过多 workflow noise

---

#### 8. Recommended Production Architecture

请输出：

* 最终推荐架构图
* 各组件职责
* 数据流
* event flow
* retrieval flow
* reply generation flow

---

## 四、核心设计原则（必须遵守）

#### 原则 1

Conversational Memory 不属于 GraphStatus。

---

#### 原则 2

EventBus 不负责长期 memory。

---

#### 原则 3

ReplyAgent 是唯一人格出口。

---

#### 原则 4

Manager 负责 semantic orchestration，而非 conversational narration。

---

#### 原则 5

Coordinator 不维护人格。

---

#### 原则 6

Memory 应独立于 Workflow Runtime。

---

## 五、输出要求

请输出：

* 完整架构
* 数据结构
* 生命周期
* retrieval 机制
* prompt assembly 机制
* memory storage 设计
* production-ready 方案

要求：

* 工程化
* 可扩展
* 支持多 Agent
* 支持 LangGraph
* 支持长期运行 session
* 支持 checkpoint/replay
* 支持 streaming reply
* 支持 future scaling

不要只给概念，请给实际可实现的结构与字段设计。
