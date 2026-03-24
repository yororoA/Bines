# Bines 环境变量清单

本文档汇总当前项目代码中**已实际读取/使用**的环境变量（按代码扫描口径），并按配置位置区分：

- Python 主工程：`config.py`
- TS AI 网关：`.env.local / .env / .env.example`（网关启动时按顺序自动加载：`.env.local` -> `.env` -> `.env.example`）

---

## 1) `config.py`（Python 工程侧）

> 说明：仓库中的 Python 模块大量通过 `from config import ...` 读取配置；其中部分键会再通过 `require_env(...)` 或 `os.environ.get(...)` 与系统环境变量联动。

| 环境变量 | 作用 | 主要使用位置 | 默认/回退 |
|---|---|---|---|
| `DEEPSEEK_API_KEY` | 主模型（Thinking/Main）调用 DeepSeek 的鉴权 Key | `thingking/src/agents.py`, `thingking/src/handle_zmq.py` | 必填（`require_env`） |
| `DEEPSEEK_THINKING_API_KEY` | 工具模型/思考模型（ToolAgent、ThinkingModelHelper）鉴权 Key | `thingking/src/thinking_model_helper.py`, `tools/music_tool.py` | 必填（`require_env`） |
| `DEEPSEEK_SUMMARY_API_KEY` | 摘要模型（记忆摘要、评分、改写）鉴权 Key | `thingking/src/layered_memory.py` | 必填（`require_env`） |
| `DASHSCOPE_API_KEY` | 视觉多模态（VLM）调用鉴权 Key | `tools/screen_tool.py`, `tools/moments_tool.py`, `thingking/src/handle_zmq.py`, `visual/0.py`, `realtime_screen_analysis_standalone.py`, `video_analysis_standalone.py` | 部分路径必填（`require_env`） |
| `MOMENTS_API_BASE_URL` | 动态（moments）服务基础地址（覆盖 `config` 中默认值） | `tools/moments_tool.py` | 未设置时回退到 `MOMENTS_API_BASE_URL`（config 常量） |
| `AI_SDK_GATEWAY_BASE` | Python 侧访问 TS AI 网关的地址 | `thingking/src/agents.py`, `thingking/src/thinking_stream_runner.py`, `thingking/src/layered_memory.py`, `thingking/src/bored_detector.py` | 默认 `http://127.0.0.1:3100` |
| `TS_AI_SDK_GATEWAY_URL` | TS AI 网关地址（在 `config.py` 中读取的网关 URL 配置项） | `config.py` | 默认 `http://127.0.0.1:3100` |
| `MAIN_TOOL_SELECTION_MODE` | 主模型后处理的工具选择模式（`filter` 或 `main`） | `thingking/src/thinking_stream_runner.py` | 默认 `filter` |
| `RAG_EMBEDDING_MODEL` | RAG 向量化模型名（覆盖 config 默认模型） | `thingking/src/rag_server_core.py` | 回退 `RAG_EMBEDDING_MODEL`（config 常量） |
| `HF_ENDPOINT` | HuggingFace 下载镜像地址 | `thingking/src/rag_server_core.py` | 未设时程序可能自动写入默认镜像 |
| `HF_MIRROR` | HuggingFace 备用镜像标识/地址 | `thingking/src/rag_server_core.py` | 与 `HF_ENDPOINT` 联合判断 |
| `HF_HUB_DOWNLOAD_TIMEOUT` | HuggingFace 下载超时 | `thingking/src/rag_server_core.py` | 未设时由程序 `setdefault` |
| `HF_HUB_ETAG_TIMEOUT` | HuggingFace ETag 请求超时 | `thingking/src/rag_server_core.py` | 未设时由程序 `setdefault` |
| `DASHSCOPE_VISION_MODEL` | 独立视觉脚本使用的视觉模型名 | `realtime_screen_analysis_standalone.py`, `video_analysis_standalone.py` | 默认 `qwen3-vl-flash` |
| `DASHSCOPE_API_URL` | 独立视觉脚本调用 DashScope 的接口地址 | `realtime_screen_analysis_standalone.py`, `video_analysis_standalone.py` | 默认 `https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions` |
| `DASHSCOPE_API_TIMEOUT` | 独立视觉脚本调用超时时间（秒） | `realtime_screen_analysis_standalone.py`, `video_analysis_standalone.py` | 默认 `60` |
| `USERNAME` | Windows 本机用户名（用于推断本地应用安装路径，如 VSCode） | `tools/app_tool.py` | 系统环境变量，缺失时按空处理 |

### 运行时自动改写/注入（了解即可）

这些变量在程序启动/运行时会被代码设置，通常无需手工配置：

- `NO_PROXY`：`start_modules.py`, `server/module_manager.py`
- `PATH`：`server/gui_display.py`（追加 ffmpeg 路径）
- `HF_ENDPOINT`：`thingking/src/rag_server_core.py` 在特定场景可能自动设置

---

## 2) `.env.local / .env.example`（TS AI 网关侧）

> 目录：`ts_ai_sdk_gateway/`  
> 代码入口：`ts_ai_sdk_gateway/src/index.ts`

| 环境变量 | 作用 | 网关角色 | 默认/回退 |
|---|---|---|---|
| `PORT` | 网关监听端口 | 网关进程 | 默认 `3100` |
| `DEEPSEEK_API_KEY` | 通用 DeepSeek Key（各角色兜底） | main/tool/summary/bored | 空字符串（未配会告警） |
| `DEEPSEEK_API_KEY_MAIN` | 主模型专用 Key | main | 回退 `DEEPSEEK_API_KEY` |
| `DEEPSEEK_API_KEY_TOOL` | 工具模型专用 Key | tool | 回退 `DEEPSEEK_API_KEY` |
| `DEEPSEEK_API_KEY_SUMMARY` | 摘要模型专用 Key | summary | 回退 `DEEPSEEK_API_KEY` |
| `DEEPSEEK_API_KEY_BORED` | 无聊检测模型专用 Key | bored | 回退 `DEEPSEEK_API_KEY` |
| `DEEPSEEK_MODEL` | 通用模型名（各角色兜底） | main/tool/summary/bored | 默认 `deepseek-chat` |
| `DEEPSEEK_MODEL_MAIN` | 主模型模型名 | main | 回退 `DEEPSEEK_MODEL` |
| `DEEPSEEK_MODEL_TOOL` | 工具模型模型名 | tool | 回退 `DEEPSEEK_MODEL` |
| `DEEPSEEK_MODEL_SUMMARY` | 摘要模型模型名 | summary | 回退 `DEEPSEEK_MODEL` |
| `DEEPSEEK_MODEL_BORED` | 无聊检测模型模型名 | bored | 回退 `DEEPSEEK_MODEL` |

---

## 3) 运行分级（必填 / 可选）

下面按“能不能跑起来”给出一版部署优先级。

### A. Python 主工程（`config.py` 侧）

**运行必填（核心对话链路）**

- `DEEPSEEK_API_KEY`：主模型调用必需
- `DEEPSEEK_THINKING_API_KEY`：工具模型调用必需
- `DEEPSEEK_SUMMARY_API_KEY`：摘要/记忆链路必需

**功能可选（按模块启用）**

- `DASHSCOPE_API_KEY`：视觉相关模块（屏幕分析、图片分析）启用时必需
- `DEEPSEEK_BORED_API_KEY`：无聊检测专用 Key（未配置时会回退主 Key）
- `MOMENTS_API_BASE_URL`：仅动态工具链路使用（未配置则回退 config 默认）

**调优可选（不配也可运行）**

- `AI_SDK_GATEWAY_BASE`：Python 调 TS 网关地址，默认 `http://127.0.0.1:3100`
- `TS_AI_SDK_GATEWAY_URL`：网关 URL 配置项，默认 `http://127.0.0.1:3100`
- `MAIN_TOOL_SELECTION_MODE`：工具选择模式，默认 `filter`
- `RAG_EMBEDDING_MODEL`：向量模型覆盖
- `HF_ENDPOINT` / `HF_MIRROR` / `HF_HUB_DOWNLOAD_TIMEOUT` / `HF_HUB_ETAG_TIMEOUT`：RAG 下载与镜像调优
- `DASHSCOPE_API_URL` / `DASHSCOPE_VISION_MODEL` / `DASHSCOPE_API_TIMEOUT`：独立视觉脚本调优
- `USERNAME`：Windows 本机用户名（应用路径推断辅助）

### B. TS AI 网关（`.env.local/.env.example` 侧）

**运行必填（至少满足一套 Key）**

- 至少提供以下之一：
  - 通用：`DEEPSEEK_API_KEY`
  - 或角色专用：`DEEPSEEK_API_KEY_MAIN` / `DEEPSEEK_API_KEY_TOOL` / `DEEPSEEK_API_KEY_SUMMARY` / `DEEPSEEK_API_KEY_BORED`

> 实务建议：直接配 `DEEPSEEK_API_KEY`，再按需覆盖角色 Key。

**调优可选**

- `PORT`（默认 `3100`）
- `DEEPSEEK_MODEL` 及角色模型覆盖：
  - `DEEPSEEK_MODEL_MAIN`
  - `DEEPSEEK_MODEL_TOOL`
  - `DEEPSEEK_MODEL_SUMMARY`
  - `DEEPSEEK_MODEL_BORED`

---

## 4) 建议

- Python 侧：把上述 `config.py` 关联变量集中在 `config.py` 内管理，并在部署环境导出同名环境变量。
- TS 网关侧：维护 `ts_ai_sdk_gateway/.env.example` 作为模板，推荐优先使用 `.env.local`（网关会自动按 `.env.local` -> `.env` -> `.env.example` 顺序加载）。

