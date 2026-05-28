# Development Environment Setup

## Prerequisites

- Python 3.11+
- NapCat QQ Bot server running and accessible
- API keys for LLM providers (DeepSeek, MIMO, etc.)

## Installation

```bash
cd thinking
pip install -r requirements.txt
```

## Configuration

Create `thinking.env` in the project root:

```env
# Model Configuration
MODEL_LIST=["deepseek-v4-flash","deepseek-v4-pro","mimo-v2.5","mimo-v2.5-pro"]
MODEL_SELECTED=deepseek-v4-flash

# Provider: DeepSeek
DEEPSEEK_API_URL=https://api.deepseek.com/v1
DEEPSEEK_API_KEY=sk-your-deepseek-key

# Provider: MIMO
MIMO_API_URL=https://api.mimo.com/v1
MIMO_API_KEY=sk-your-mimo-key

# Checkpoint
CHECKPOINT_THREAD_ID=["raw_chat","QQ_private","QQ_group"]

# RAG Embedding
RAG_EMBEDDING_MODEL=BAAI/bge-small-zh-v1.5
RAG_PERSIST_DIR=memory_data/chroma_db
HF_ENDPOINT=

# NapCat QQ Bot
NAPCAT_WS_SERVER=ws://127.0.0.1:3001
NAPCAT_WS_TOKEN=your-napcat-token
NAPCAT_WS_RECONNECT_TIMEOUT=5
NAPCAT_WS_API_RESPONSE_TIMEOUT=15
```

## Running

```bash
python main.py
```

The system will connect to the NapCat WebSocket and begin listening for QQ messages.

## Adding a New Model Provider

1. Register the provider programmatically:

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

2. Or add environment variables in `thinking.env` and update `register_default_providers()` in `utils/model_registry.py`.

## Adding a New Tool

Tools are categorized and registered dynamically:

```python
from tools import get_tool_registry, PERFORMER_TOOLS

@tool
def my_tool(query: str) -> str:
    """Description of what this tool does."""
    return result

registry = get_tool_registry()
registry.register_tool(PERFORMER_TOOLS, my_tool, authorized_imports=["json"])

# or for reply tools:
registry.register_tool(REPLY_TOOLS, my_reply_tool)
```

## Customizing Persona

Edit `Personal/SOUL.md` to define the AI character. Changes are detected via file modification time and hot-reloaded automatically.

## Testing Persona Changes

The SOUL file is cached with mtime-based invalidation. After editing:
1. Save `Personal/SOUL.md`
2. The next message will automatically use the updated persona
3. No restart required

## Project Structure Pattern

When adding new modules, follow these conventions:
- Use `from __future__ import annotations` in all files
- Use `LazyLangChainModel`/`LazySmolModel` from `utils.lazy_model` instead of direct model creation
- Register tools through `ToolRegistry` instead of hardcoding
- Add error handling with logging for all node functions
- Use `persona_cache` from `memory.persona_state` for persona reads
- Use `file_cache` from `utils.file_cache` for file reads needing hot-reload