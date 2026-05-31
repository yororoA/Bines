# Thinking - AI Agent 对话系统

基于 LangGraph 构建的 AI Agent 对话系统，通过 NapCat WebSocket 协议集成 QQ 聊天。

## 系统架构

```
                        ┌─────────────────────────────────────────────────────────────┐
                        │                    LangGraph StateGraph                      │
                        │                                                             │
 [QQ Message]           │  context_builder ──→ manager ──→ performer ──┐              │
      │                 │       │               │  ↑                  │              │
      v                 │       │               │  │                  │              │
 [NapCat WS] ──debounce─┼──→ invoke             │  └─────────────────┘              │
                        │                       │                                   │
                        │                       ├──→ advance_reply ──→ manager       │
                        │                       │                                   │
                        │                       └──→ final_reply ──→ dynamic_agent   │
                        │                                    (分段发送)    │          │
                        │                                                  v          │
                        │                                             status_trim ──→ END
                        └─────────────────────────────────────────────────────────────┘
                                         │
                        ┌────────────────┼────────────────┐
                        v                v                v
                   ContextManager    ChromaDB         SQLite
                  (运行时上下文)    (记忆向量库)     (checkpoint)
```

## 核心特性

- **7 节点工作流管道**: context_builder → manager → performer → advance_reply → final_reply → dynamic_agent → status_trim
- **任务驱动路由**: Manager 通过 LLM 结构化输出决定路由（performer / advance_reply / final_reply）
- **分段消息发送**: ReplyAgent 自动将长回复拆分为多条短消息，模拟自然对话节奏
- **情绪系统**: 基于 arousal（0-100）的动态情绪状态，影响回复语气
- **任务上下文传递**: Performer 执行结果自动注入 ReplyAgent 的任务列表和系统提示
- **记忆系统**: 6 个 ChromaDB 集合，支持 RAG 检索、记忆衰减和 buffer → diary 整合
- **人设系统**: 基于 SOUL.md 的动态人设，支持热重载
- **工具注册**: 动态工具注册机制，支持 performer/reply 两类工具
- **模型管理**: 多 Provider 注册，懒加载模型缓存
- **Data Viewer**: Web 界面查看 LangGraph checkpoint 和 ChromaDB 记忆数据

## 快速开始

### 环境要求

- Python 3.11+
- NapCat QQ Bot 服务端
- LLM Provider API Key（DeepSeek、MIMO 等）

### 安装

```bash
pip install -r requirements.txt
```

### 配置

复制 `thinking.env.example` 为 `thinking.env` 并填写配置：

```env
# 模型配置
MODEL_LIST='["deepseek-v4-flash","deepseek-v4-pro","mimo-v2.5","mimo-v2.5-pro"]'
MODEL_SELECTED=deepseek-v4-flash

# Provider: DeepSeek
DEEPSEEK_API_URL=https://api.deepseek.com
DEEPSEEK_API_KEY=sk-your-key

# Provider: MIMO
MIMO_API_URL=https://token-plan-sgp.xiaomimimo.com/v1
MIMO_API_KEY=sk-your-key

# RAG 嵌入模型
RAG_EMBEDDING_MODEL=BAAI/bge-small-zh-v1.5
RAG_PERSIST_DIR=memory_data/chroma_db

# NapCat QQ Bot
NAPCAT_WS_SERVER=ws://localhost:9998
NAPCAT_WS_TOKEN=your-token
BOT_NUMBER=your-bot-qq-number

# 图片识别（可选）
VISUAL_RECOGNITION_API_KEY=sk-your-key
VISUAL_RECOGNITION_API_URL=https://api.example.com/v1
VISUAL_RECOGNITION_MODEL=model-name

# 超时与限制（可选）
WORKFLOW_TIMEOUT_SECONDS=120.0
LLM_REQUEST_TIMEOUT_SECONDS=30.0
LLM_MAX_RETRIES=1
DEBOUNCE_SECONDS=3.0
MAX_INPUT_LENGTH=4096
```

### 运行

```bash
python main.py
```

### Data Viewer

```bash
python -m data_viewer            # 默认 8501 端口
python -m data_viewer --port 9000  # 自定义端口
```

## 项目结构

```
thinking/
├── main.py                         # 入口：asyncio 事件循环 + NapCat 连接
├── thinking_settings.py            # Pydantic Settings 配置（所有参数）
├── thinking.env                    # 环境变量（不入库）
│
├── Personal/
│   └── SOUL.md                     # AI 人设定义（热重载）
│
├── workflow/                       # LangGraph 工作流
│   ├── workflow.py                 # StateGraph 编译 + Workflow.invoke()
│   ├── context_manager.py          # 运行时上下文管理器（线程安全）
│   ├── cancel.py                   # 超时取消事件
│   ├── status/
│   │   ├── graph_status.py         # GraphStatus TypedDict + 状态合并函数
│   │   └── manager_route.py        # ManagerRoute / ReplyInput / TaskItem 模型
│   └── nodes/
│       ├── context_builder_node.py # 加载人设、RAG、构建上下文
│       ├── manager_node.py         # LLM 任务规划与路由决策
│       ├── performer_node.py       # 执行搜索/动作任务（smolagents CodeAgent）
│       ├── reply_node.py           # 生成回复（支持分段发送 + 情绪系统）
│       ├── dynamic_agent_node.py   # 记忆写入、buffer 整合、衰减
│       └── status_trim_node.py     # 消息窗口裁剪 + 状态重置
│
├── memory/                         # 记忆系统
│   ├── persona_state.py            # PersonaState / PersonaCache / PersonaMood
│   ├── store_interface.py          # MemoryStore 抽象接口
│   ├── vector_store/               # ChromaDB 封装（6 个 collection）
│   ├── retrieval/                  # 场景化 RAG 检索（manager/reply/performer）
│   ├── consolidation/              # LLM judge 记忆分类存储
│   └── lifecycle/                  # buffer → diary 整合
│
├── napcat_server/                  # QQ 消息接入
│   ├── napcat_connection.py        # WebSocket + 消息队列 + debounce
│   └── global_client.py            # 全局 NapCatClient 实例
│
├── tools/                          # 工具系统
│   ├── tool_registry.py            # 动态工具注册（PERFORMER_TOOLS / REPLY_TOOLS）
│   ├── performer_tools/
│   │   ├── webSearch.py            # 网页搜索
│   │   └── visualRecognition.py    # 图片识别
│   └── napcat_tools/
│       └── common_msgs/
│           ├── cmsg_tools.py       # send_msg（累积 reply_texts）
│           ├── msg_tools.py        # delete_msg / get_msg / forward_msg 等
│           ├── group_tools.py      # 群信息查询
│           ├── interact_tools.py   # send_poke
│           └── types.py            # Pydantic 消息模型
│
├── utils/                          # 工具库
│   ├── model_registry.py           # 模型 Provider 注册
│   ├── lazy_model.py               # 懒加载模型缓存（LangChain + smolagents）
│   ├── generate_langchain_model.py # LangChain ChatOpenAI 创建
│   ├── generate_sml_model.py       # smolagents OpenAIModel 创建
│   ├── file_cache.py               # 文件热重载缓存
│   ├── time_utils.py               # day_key() 日期工具
│   └── observability.py            # MetricsCollector 性能指标
│
├── data_viewer/                    # 数据查看 Web 工具
│   ├── api.py                      # FastAPI 路由
│   ├── db.py                       # SQLite + ChromaDB 数据访问
│   └── __main__.py                 # CLI 启动入口
│
└── docs/                           # 文档
```

## 工作流详解

### 图结构与边

```
context_builder ──→ manager ──→ performer ──→ manager   (循环直到无待执行任务)
                      │
                      ├──→ advance_reply ──→ manager    (中间进度通知后继续)
                      │
                      └──→ final_reply ──→ dynamic_agent ──→ status_trim ──→ END
```

- `context_builder → manager`: 构建上下文后进入决策
- `performer → manager`: 执行完任务后回到 Manager 重新评估
- `advance_reply → manager`: 发送中间进度后回到 Manager 继续
- `final_reply → dynamic_agent → status_trim → END`: 最终回复后写入记忆、裁剪状态

### ManagerNode 路由逻辑

Manager 通过 LLM 结构化输出（`ManagerRoute`）决定下一步：

```python
class ManagerRoute(BaseModel):
    performer_tasks: list[TaskItem]    # 待执行任务列表
    goto_advance_reply: bool           # 是否发送中间进度
    advance_reply_hint: str | None     # 中间回复提示
    goto_final_reply: bool             # 是否直接最终回复
    final_reply_hint: str | None       # 最终回复提示
    thoughts: str                      # 决策思维链
```

路由优先级：
1. 收敛检测（连续 N 轮无新任务）→ `final_reply`
2. 超过最大迭代次数 → `final_reply`
3. LLM 决定 `goto_final_reply=True` → `final_reply`
4. LLM 决定 `goto_advance_reply=True` → `advance_reply`
5. 有待执行 performer 任务 → `performer`（可并行多个）
6. 无待执行任务 → `final_reply`（fallback）

### ReplyNode 回复机制

**分段发送**: 系统提示要求每条消息约 50-150 字符，长回复自动拆分为多次 `send_msg` 调用。

**任务上下文**: `_build_reply_tasks_from_state()` 从 `state.tasks_done` 收集 performer 结果（排除 reply 反馈），附加 Manager 的 hint 作为回复指令，注入 ReplyAgent 的任务列表。

**系统提示组成**:
- Soul Prompt（人设）
- Mood（情绪状态 + arousal 等级）
- Persona Profile（用户画像）
- RAG Context（记忆检索）
- Task Results（performer 执行结果）
- Conversation History（对话历史）
- Already Said（避免重复）

**情绪系统** (PersonaMood):
| arousal 范围 | 状态 | 回复风格 |
|---|---|---|
| 0-30 | 轻松活泼 | 好奇、调侃 |
| 31-60 | 略带不自在 | 吐槽但藏着慌乱 |
| 61-85 | 傲娇害羞 | 嘴硬否认、语速偏快 |
| 86-100 | 炸毛害羞 | 极度慌乱、可能结巴 |

**多消息存储**: `send_msg` 工具通过 `ctx.append_to_list("reply_texts", ...)` 累积所有发送的文本，ReplyNode 最终将全部文本作为 `AIMessage` 列表写入 checkpoint。

### ContextManager 数据流

`ContextManager` 是线程安全的运行时上下文容器，在每次 `Workflow.invoke()` 时重置：

```
ContextBuilderNode 写入:
  persona_snapshot, rag_recall, soul_prompt,
  conversation_history, thread_id, already_said, persona_mood

ManagerNode 写入:
  thoughts, task_results（仅 performer 结果）

ReplyNode 写入:
  reply_texts（累积）, reply_current_message, reply_tasks
```

### 状态合并策略

`GraphStatus` 使用自定义 reducer 管理状态合并：

| 字段 | 策略 | 说明 |
|------|------|------|
| `messages` | `add` | 追加新消息 |
| `tasks_done` | `merge_tasks` | 按 category 合并，去重 task_id |
| `thoughts` | `cap_list` | 保留最近 10 条 |
| `already_said` | `add_list_str` | 追加 |
| 其他字段 | 覆盖 | iteration_count 等 |

## 记忆系统

| 集合 | 用途 | 来源 |
|------|------|------|
| knowledge | 稳定知识（技术事实、项目信息） | LLM judge 分类存储 |
| persona | 用户长期特征（偏好、习惯） | LLM judge 分类存储 |
| diary | 日记条目 | buffer 整合生成 |
| sliced_diary | 日记段落切片 | diary 自动切片 |
| buffer | 待整合的对话片段 | 每次对话后写入 |

**衰减机制**: 每 5 次对话触发，半衰期 30 天，基于重要性的指数衰减，每集合上限 500 条。

**RAG 检索**: 根据场景（manager/reply/performer）使用不同参数从 knowledge、persona、diary 集合检索相关记忆。

## 工具系统

### 工具分类

| 类别 | 常量 | 用途 | 包含工具 |
|------|------|------|---------|
| Performer | `PERFORMER_TOOLS` | 任务执行 | webSearch, visualRecognition, send_msg, delete_msg, get_msg, forward_msg, 群信息查询, send_poke |
| Reply | `REPLY_TOOLS` | 回复生成 | send_msg |

### send_msg 工具

核心消息发送工具，支持私聊和群聊：

```python
send_msg(msg={
    "message_type": "private",  # 或 "group"
    "user_id": "123456",        # 私聊时填写
    "group_id": "789012",       # 群聊时填写
    "message": [
        {"type": "text", "data": {"text": "消息内容"}}
    ]
})
```

发送成功后自动将文本追加到 `ctx.reply_texts`，ReplyNode 最终将所有文本存入 checkpoint。

## 扩展指南

### 添加模型 Provider

```python
from utils import get_model_registry, ModelProvider

registry = get_model_registry()
registry.register(ModelProvider(
    name="openai",
    base_url="https://api.openai.com/v1",
    api_key="sk-your-key",
    models=["gpt-4o", "gpt-4o-mini"],
))
```

### 添加新工具

```python
from tools import get_tool_registry, PERFORMER_TOOLS

registry = get_tool_registry()
registry.register_tool(PERFORMER_TOOLS, my_tool, authorized_imports=["json"])
```

### 自定义人设

编辑 `Personal/SOUL.md`，保存后自动热重载，无需重启。

## 文档

- [架构详解](docs/architecture.md)
- [开发环境配置](docs/dev_setup.md)
