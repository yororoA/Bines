# Thinking - AI Agent System Architecture

## Overview

Thinking is an AI Agent conversational system built on LangGraph, integrated with QQ via NapCat WebSocket protocol. It features a 4-node workflow pipeline with a comprehensive memory system and persona-based role playing.

**Key Technologies**: LangGraph, LangChain, ChromaDB, SmolAgents, Pydantic Settings

## System Architecture

```text
napcat_server/          tools/                  workflow/
    |                      |                        |
    v                      v                        v
[NapCat WebSocket] --> [Message Queue] --> [LangGraph Workflow]
    |                                           |
    |    memory/                                |
    +--> [Persona / RAG / ChromaDB] <-----------+
```

## Workflow Pipeline

```text
context_builder --> performer --> dynamic_agent --> status_trim --> END
```

### Node Responsibilities

| Node | File | Responsibility |
| ------ | ------ | ----------------- |
| context_builder | `workflow/nodes/context_builder_node.py` | Load SOUL.md persona, RAG retrieval, persona snapshot, conversation history |
| performer | `workflow/nodes/performer_node.py` | Autonomous agent that handles all user requests end-to-end via smolagents CodeAgent |
| dynamic_agent | `workflow/nodes/dynamic_agent_node.py` | Memory write via judge_and_store, buffer-to-diary consolidation, memory decay |
| status_trim | `workflow/nodes/status_trim_node.py` | Clear ephemeral state, message window trimming with LLM summarization |

## Memory System

### Collection Architecture

| Collection | Purpose |
| ------ | ----------------- |
| `knowledge` | Stable reusable knowledge (tech facts, project info) |
| `persona` | User long-term traits (preferences, habits, tech stack) |
| `diary` | Daily diary entries |
| `sliced_diary` | Diary paragraph slices for granular retrieval |
| `buffer` | Conversation fragments pending diary consolidation |

### Memory Lifecycle

```text
Conversation --> judge_and_store (LLM) --> [knowledge] --> ChromaDB
                                         --> [persona] --> ChromaDB (with cache invalidation)
                                         --> [diary] --> ChromaDB
                                         --> [buffer] --> daily consolidation --> diary + sliced_diary
```

### Decay Mechanism

Knowledge and diary collections decay over time:

- Half-life: 30 days
- Importance-based exponential decay
- Min effective importance threshold
- Max entries cap: 500 per collection
- Triggered every 5 invocations (via `DynamicAgentNode`)

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

`tools/tool_registry.py` provides dynamic tool registration with a single `PERFORMER_TOOLS` category:

| Tool | Module | Description |
| ------ | ------ | ----------------- |
| webSearch | `performer_tools/webSearch.py` | DuckDuckGo web search |
| visualRecognition | `performer_tools/visualRecognition.py` | Image content recognition via vision LLM API |
| send_msg | `napcat_tools/common_msgs/cmsg_tools.py` | Send QQ messages |
| delete_msg | `napcat_tools/common_msgs/msg_tools.py` | Recall QQ messages |
| get_msg | `napcat_tools/common_msgs/msg_tools.py` | Get QQ message details |
| send_forward_msg | `napcat_tools/common_msgs/msg_tools.py` | Send merged forward messages |
| send_group_forward_msg | `napcat_tools/common_msgs/msg_tools.py` | Send group merged forward messages |
| send_private_forward_msg | `napcat_tools/common_msgs/msg_tools.py` | Send private merged forward messages |
| get_group_msg_history | `napcat_tools/common_msgs/msg_tools.py` | Get group message history |
| get_friend_msg_history | `napcat_tools/common_msgs/msg_tools.py` | Get private message history |
| get_group_list | `napcat_tools/common_msgs/group_tools.py` | Get all group list |
| get_group_info | `napcat_tools/common_msgs/group_tools.py` | Get group detailed info |
| get_group_member_list | `napcat_tools/common_msgs/group_tools.py` | Get group member list |
| get_group_member_info | `napcat_tools/common_msgs/group_tools.py` | Get group member detailed info |
| send_poke | `napcat_tools/common_msgs/interact_tools.py` | Send poke interaction |

**Adding a new tool:**

```python
from tools import get_tool_registry, PERFORMER_TOOLS

registry = get_tool_registry()
registry.register_tool(PERFORMER_TOOLS, my_new_tool, authorized_imports=["json"])
```

## NapCat QQ Integration

### Message Flow

```text
QQ Client --> NapCat WebSocket --> json parse --> message_queue --> _process_loop --> Workflow.invoke()
```

### Connection Management

- Exponential backoff with jitter for reconnection
- Separate `_process_loop` consuming from `asyncio.Queue`
- API call timeout with connection wait
- Message deduplication via seen message ID tracking (max 1000 entries)
- Debounce cooldown (configurable via `DEBOUNCE_SECONDS`, default 3.0s)

## State Management

### GraphStatus

Defined in `workflow/status/graph_status.py`:

| Field | Type | Description |
| ------ | ------ | ----------------- |
| `messages` | `list[AnyMessage]` | Conversation messages (with replace reducer for window trimming) |
| `tasks_done` | `dict[str, list[TaskItem]]` | Completed tasks per agent (merge dedup) |
| `already_said` | `list[str]` | Previously sent reply texts to avoid repetition |
| `persona_mood` | `dict` | Current persona mood state |
| `diary_triggered_day` | `str` | Last day diary was triggered |
| `invocation_count` | `int` | Total workflow invocation counter |
| `thread_id` | `str` | Conversation thread identifier |
| `cancel_event` | `threading.Event` | Cancellation event for workflow interruption |

### ContextManager

`workflow/context_manager.py` provides thread-safe per-invocation context sharing between nodes:

- `persona_snapshot` - Cached persona state
- `rag_recall` - RAG retrieval results
- `soul_prompt` - Loaded SOUL.md content
- `conversation_history` - Formatted message history
- `reply_texts` - Texts sent during performer execution
- `already_said` - Accumulated sent messages
- `persona_mood` - Current mood state
- `target_type` / `target_id` - Resolved QQ message target

### Configuration

All settings managed via `ThinkingSettings` (Pydantic BaseSettings) in `thinking_settings.py`:

| Setting | Default | Description |
| ------ | ------ | ----------------- |
| `MODEL_SELECTED` | deepseek-v4-flash | Active model name |
| `DEBOUNCE_SECONDS` | 3.0 | Message debounce cooldown |
| `WORKFLOW_TIMEOUT_SECONDS` | 120.0 | Workflow execution timeout |
| `LLM_REQUEST_TIMEOUT_SECONDS` | 30.0 | Per-request LLM timeout |
| `LLM_MAX_RETRIES` | 1 | LLM request retry count |
| `LLM_MAX_TOKENS` | 4096 | Max tokens per LLM call |
| `DEDUP_THRESHOLD` | 0.08 | Memory dedup similarity threshold |
| `MAX_INPUT_LENGTH` | 4096 | Max input message length |
| `RETRIEVAL_KNOWLEDGE_K` | 5 | Knowledge retrieval count |
| `RETRIEVAL_PERSONA_K` | 4 | Persona retrieval count |
| `RETRIEVAL_DIARY_K` | 2 | Diary retrieval count |
| `CONVERGENCE_WINDOW` | 3 | Convergence detection window |
| `DAY_KEY_CUTOFF_HOUR` | 4 | Day boundary hour for day_key |
| `RAG_EMBEDDING_MODEL` | BAAI/bge-small-zh-v1.5 | Embedding model for RAG |

### Persistence

SQLite checkpoints via LangGraph SqliteSaver with WAL mode. Single checkpoint retained per thread (old checkpoints pruned after each invocation).

## Data Viewer

Web-based tool for inspecting LangGraph checkpoint data and ChromaDB memory collections.

### Architecture

```text
data_viewer/
├── __main__.py     # CLI entry (python -m data_viewer)
├── api.py          # FastAPI app with REST endpoints
├── db.py           # SQLite + ChromaDB data access
└── static/
    └── index.html  # Single-page frontend with tab switching
```

### API Endpoints

| Endpoint | Description |
| ------ | ----------------- |
| `GET /` | Frontend page |
| `GET /api/threads` | List all thread_ids with checkpoint counts |
| `GET /api/threads/{thread_id}/checkpoints` | List checkpoints for a thread |
| `GET /api/checkpoints/{checkpoint_id}` | Full deserialized checkpoint detail |
| `GET /api/collections` | List all ChromaDB collections with item counts |
| `GET /api/collections/{name}/items` | List items in a collection (supports `offset`, `limit`, `q` params) |

### Data Flow

```text
SQLite (checkpoints.db) ──┐
                          ├──> db.py ──> api.py (FastAPI) ──> index.html
ChromaDB (chroma_db/) ────┘
```

### Key Design

- **Read-only SQLite**: Uses `file:xxx?mode=ro` URI to prevent accidental data modification
- **Lazy deserialization**: Checkpoint BLOBs deserialized on-demand via LangGraph's `JsonPlusSerializer`
- **Frontend caching**: Thread list and checkpoint details cached in browser memory
- **Tab-based UI**: Separate views for checkpoint and ChromaDB data

## Directory Structure

```text
thinking/
├── main.py                         # Entry point
├── thinking_settings.py            # Pydantic Settings config
├── thinking.env                    # Environment variables
│
├── Personal/
│   └── SOUL.md                     # AI persona definition
│
├── data_viewer/                    # Data inspection tool
│   ├── __main__.py                 # CLI entry point
│   ├── api.py                      # FastAPI routes
│   ├── db.py                       # SQLite + ChromaDB data access
│   └── static/
│       └── index.html              # Frontend page
│
├── memory/
│   ├── persona_state.py            # Persona data model + cache + mood
│   ├── store_interface.py          # MemoryStore Protocol
│   ├── consolidation/
│   │   └── memory_judge.py         # LLM memory classifier (judge_and_store)
│   ├── lifecycle/
│   │   └── buffer_diary.py         # Buffer-to-diary consolidation
│   ├── retrieval/
│   │   └── retrieve_api.py         # Scene-specific retrieval (reply/manager/performer)
│   └── vector_store/
│       └── chroma_store.py         # ChromaDB wrapper (5 collections + decay)
│
├── napcat_server/
│   ├── napcat_connection.py        # WebSocket client, message queue, debounce
│   └── global_client.py            # Global client singleton
│
├── tools/
│   ├── tool_registry.py            # Dynamic tool registry
│   ├── performer_tools/
│   │   ├── webSearch.py            # Web search tools (DuckDuckGo)
│   │   └── visualRecognition.py    # Image recognition via vision API
│   └── napcat_tools/
│       └── common_msgs/
│           ├── base.py             # Async API call helper
│           ├── types.py            # Pydantic message type models
│           ├── cmsg_tools.py       # QQ message sending
│           ├── msg_tools.py        # QQ message operations
│           ├── group_tools.py      # QQ group operations
│           └── interact_tools.py   # QQ interaction (poke)
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
    ├── cancel.py                   # Cancellation event management
    ├── context_manager.py          # Per-invocation context sharing
    ├── status/
    │   ├── graph_status.py         # GraphStatus TypedDict + reducers
    │   └── manager_route.py        # TaskItem model
    └── nodes/
        ├── context_builder_node.py
        ├── performer_node.py
        ├── dynamic_agent_node.py
        └── status_trim_node.py
```

## Key Design Patterns

- **Registry Pattern**: Model providers and tools use registries for dynamic extensibility
- **Lazy Initialization**: Models loaded on first use, thread-safe
- **Exponential Decay**: Memory entries decay based on time and importance
- **Sliding Window**: Messages trimmed at 20 with LLM summarization of earliest 10
- **Debounce**: Rapid consecutive messages buffered and processed together
- **Cancellation**: Thread-safe cancellation via `threading.Event` for interrupting workflows
