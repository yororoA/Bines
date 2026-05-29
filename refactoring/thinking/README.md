# Thinking - AI Agent 对话系统

基于 LangGraph 构建的 AI Agent 对话系统，通过 NapCat WebSocket 协议集成 QQ 聊天。

## 系统架构

```
napcat_server/          tools/                  workflow/
    |                      |                        |
    v                      v                        v
[NapCat WebSocket] --> [Message Queue] --> [LangGraph Workflow]
    |                                           |
    |    memory/                                |
    +--> [Persona / RAG / ChromaDB] <-----------+
```

## 核心特性

- **7 节点工作流管道**: context_builder → manager → performer → advance_reply → final_reply → dynamic_agent → status_trim
- **记忆系统**: 6 个 ChromaDB 集合（summary/knowledge/persona/diary/sliced_diary/buffer），支持 RAG 检索和记忆衰减
- **人设系统**: 基于 SOUL.md 的动态人设，支持热重载
- **工具注册**: 动态工具注册机制，支持 performer/reply/common 三类工具
- **模型管理**: 多 Provider 注册，懒加载模型缓存

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
```

### 运行

```bash
python main.py
```

## 项目结构

```
thinking/
├── main.py                         # 入口
├── thinking_settings.py            # Pydantic Settings 配置
├── thinking.env                    # 环境变量
│
├── Personal/
│   └── SOUL.md                     # AI 人设定义
│
├── memory/                         # 记忆系统
│   ├── persona_state.py            # 人设数据模型 + 缓存
│   ├── consolidation/              # 记忆分类（LLM judge）
│   ├── lifecycle/                  # buffer → diary 整合
│   ├── retrieval/                  # 场景化 RAG 检索
│   └── vector_store/               # ChromaDB 封装
│
├── napcat_server/                  # QQ 消息接入
│   └── napcat_connection.py        # WebSocket + 消息队列
│
├── tools/                          # 工具系统
│   ├── tool_registry.py            # 动态工具注册
│   ├── performer_tools/            # 搜索工具
│   └── napcat_tools/               # QQ 消息发送
│
├── utils/                          # 工具库
│   ├── model_registry.py           # 模型 Provider 注册
│   ├── lazy_model.py               # 懒加载模型缓存
│   └── file_cache.py               # 文件热重载缓存
│
├── workflow/                       # LangGraph 工作流
│   ├── workflow.py                 # StateGraph 编译
│   ├── status/                     # 状态定义
│   └── nodes/                      # 7 个节点实现
│
└── docs/                           # 文档
    ├── architecture.md             # 架构详解
    └── dev_setup.md                # 开发环境配置
```

## 工作流节点

| 节点 | 文件 | 职责 |
|------|------|------|
| context_builder | `workflow/nodes/context_builder_node.py` | 加载人设、RAG 检索、构建上下文 |
| manager | `workflow/nodes/manager_node.py` | LLM 任务规划与路由 |
| performer | `workflow/nodes/performer_node.py` | 执行搜索/动作任务 |
| advance_reply | `workflow/nodes/reply_node.py` | 中间进度通知 |
| final_reply | `workflow/nodes/reply_node.py` | 最终回复（注入人设） |
| dynamic_agent | `workflow/nodes/dynamic_agent_node.py` | 记忆写入、buffer 整合、衰减 |
| status_trim | `workflow/nodes/status_trim_node.py` | 清理临时状态、消息窗口裁剪 |

## 记忆系统

| 集合 | 用途 |
|------|------|
| summary | 事件摘要、话题抽象 |
| knowledge | 稳定知识（技术事实、项目信息） |
| persona | 用户长期特征（偏好、习惯） |
| diary | 日记条目 |
| sliced_diary | 日记段落切片 |
| buffer | 待整合的对话片段 |

**衰减机制**: 半衰期 30 天，基于重要性的指数衰减，每集合上限 500 条。

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
