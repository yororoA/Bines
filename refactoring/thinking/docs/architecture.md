# Thinking - AI Agent System Architecture

## Overview

Thinking is an AI Agent conversational system built on LangGraph, integrated with QQ via NapCat WebSocket protocol. It features a 7-node workflow pipeline with a comprehensive memory system and persona-based role playing.

## System Architecture

```
napcat_server/          tools/                  workflow/
    |                      |                        |
    v                      v                        v
[NapCat WebSocket] --> [Message Queue] --> [LangGraph Workflow]
    |                                           |
    |    memory/                                |
    +--> [Persona / RAG / ChromaDB] <-----------+
```

## Workflow Pipeline

```
context_builder --> manager --> performer --> manager (loop)
                     |                          |
                     +--> advance_reply --------+
                     |
                     +--> final_reply --> dynamic_agent --> status_trim --> END
```

### Node Responsibilities

| Node | File | Responsibility |
|------|------|---------------|
| context_builder | `workflow/nodes/context_builder_node.py` | Load SOUL.md persona, RAG retrieval, persona snapshot |
| manager | `workflow/nodes/manager_node.py` | LLM-driven task planning & routing (ManagerRoute structured output) |
| performer | `workflow/nodes/performer_node.py` | Execute search/action tasks via smolagents CodeAgent |
| advance_reply | `workflow/nodes/reply_node.py` | Intermediate progress notification |
| final_reply | `workflow/nodes/reply_node.py` | Final response with persona injection |
| dynamic_agent | `workflow/nodes/dynamic_agent_node.py` | Memory write, buffer-to-diary consolidation, memory decay |
| status_trim | `workflow/nodes/status_trim_node.py` | Clear ephemeral state, message window trimming with LLM summarization |

## Memory System

### Collection Architecture

| Collection | Purpose |
|-----------|---------|
| `summary` | Event summaries, topic abstractions |
| `knowledge` | Stable reusable knowledge (tech facts, project info) |
| `persona` | User long-term traits (preferences, habits, tech stack) |
| `diary` | Daily diary entries |
| `sliced_diary` | Diary paragraph slices for granular retrieval |
| `buffer` | Conversation fragments pending diary consolidation |

### Memory Lifecycle

```
Conversation --> judge_and_store (LLM) --> [summary] --> buffer --> daily consolidation --> diary + sliced_diary
                                         --> [knowledge] --> ChromaDB
                                         --> [persona] --> ChromaDB (with cache invalidation)
                                         --> [diary] --> ChromaDB
```

### Decay Mechanism

Knowledge collections (summary, knowledge, diary) decay over time:
- Half-life: 30 days
- Importance-based exponential decay
- Max entries cap: 500 per collection
- Triggered every 5 invocations

## Model System

### Provider Registry

Models are managed through `ModelProviderRegistry` in `utils/model_registry.py`. Each provider registers its base URL, API key, and supported model names.

**Adding a new provider:**

```python
from utils import get_model_registry, ModelProvider

registry = get_model_registry()
registry.register(ModelProvider(
    name="openai",
    base_url="https://api.openai.com/v1",
    api_key="your-api-key",
    models=["gpt-4o", "gpt-4o-mini"],
))
```

### Lazy Loading

`LazyLangChainModel` and `LazySmolModel` in `utils/lazy_model.py` provide thread-safe lazy initialization with structured output caching.

## Tool System

### Tool Registry

`tools/tool_registry.py` provides dynamic tool registration:

| Category | Typical Tools | Consumer |
|----------|--------------|----------|
| `PERFORMER_TOOLS` | webSearch, DuckDuckGoSearch, WebSearch, VisitWebpage | performer_node |
| `REPLY_TOOLS` | send_msg (QQ) | reply_node |
| `COMMON_TOOLS` | get_time | all nodes |

**Adding a new tool:**

```python
from tools import get_tool_registry, PERFORMER_TOOLS

registry = get_tool_registry()
registry.register_tool(PERFORMER_TOOLS, my_new_tool, authorized_imports=["json"])
```

## NapCat QQ Integration

### Message Flow

```
QQ Client --> NapCat WebSocket --> json parse --> message_queue --> _process_loop --> Workflow.invoke()
```

### Connection Management

- Exponential backoff with jitter for reconnection
- Separate process_loop consuming from asyncio.Queue
- API call timeout with connection wait

## State Management

### GraphStatus

Defined in `workflow/status/graph_status.py`:

- `messages` - Conversation messages (with reducer-based window trimming)
- `tasks_done` - Completed tasks (merge dedup)
- `thoughts` - Manager thought chain (capped at 10)
- `iteration_count` - Loop iteration count
- `persona_snapshot` / `rag_recall` / `soul_prompt` - Context snapshots

### Persistence

SQLite checkpoints via LangGraph SqliteSaver, three thread IDs (`raw_chat`, `QQ_private`, `QQ_group`).

## Directory Structure

```
thinking/
├── main.py                         # Entry point
├── thinking_settings.py            # Pydantic Settings config
├── thinking.env                    # Environment variables
│
├── Personal/
│   └── SOUL.md                     # AI persona definition
│
├── memory/
│   ├── persona_state.py            # Persona data model + cache
│   ├── consolidation/
│   │   └── memory_judge.py         # LLM memory classifier
│   ├── lifecycle/
│   │   └── buffer_diary.py         # Buffer-to-diary consolidation
│   ├── retrieval/
│   │   └── retrieve_api.py         # Scene-specific retrieval
│   └── vector_store/
│       └── chroma_store.py         # ChromaDB wrapper (6 collections)
│
├── napcat_server/
│   └── napcat_connection.py        # WebSocket + message queue
│
├── tools/
│   ├── tool_registry.py            # Dynamic tool registry
│   ├── performer_tools/
│   │   └── webSearch.py            # Web search tools
│   └── napcat_tools/
│       └── common_msgs/
│           └── cmsg_tools.py       # QQ message sending
│
├── utils/
│   ├── generate_langchain_model.py # LangChain model factory
│   ├── generate_sml_model.py       # SmolAgents model factory
│   ├── model_registry.py           # Model provider registry
│   ├── lazy_model.py               # Lazy loading model cache
│   ├── file_cache.py               # File content cache with hot-reload
│   ├── observability.py            # Metrics collector
│   └── time_utils.py               # Date key, token estimation
│
└── workflow/
    ├── workflow.py                 # LangGraph StateGraph compilation
    ├── status/
    │   ├── graph_status.py         # GraphStatus TypedDict
    │   └── manager_route.py        # ManagerRoute / TaskItem / ReplyInput
    └── nodes/
        ├── context_builder_node.py
        ├── manager_node.py
        ├── performer_node.py
        ├── reply_node.py
        ├── dynamic_agent_node.py
        └── status_trim_node.py
```

## Key Design Patterns

- **Registry Pattern**: Model providers and tools use registries for dynamic extensibility
- **Lazy Initialization**: Models loaded on first use, thread-safe
- **Convergence Detection**: Manager loop auto-exits when task count stabilizes
- **Exponential Decay**: Memory entries decay based on time and importance
- **Sliding Window**: Messages trimmed at 20 with LLM summarization of earliest 10