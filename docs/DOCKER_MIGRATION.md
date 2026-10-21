# Docker 部署迁移方案

> 状态：方案评审稿，未修改任何运行代码。
> 目标：在保留现有宿主机能力的前提下，把无硬件依赖的服务迁入 Docker，降低部署与依赖冲突成本。

## 1. 结论

推荐**混合部署**，不做“全部 Docker 化”。

- 适合容器化：NapCat、ChatBot、QQ Thinking、Memory Server、RAG Web、ZMQ Bus、Classification、Speaking
- 必须留在宿主机：Display、Visual、Hearing、桌面 Thinking、Realtime Screen
- 可选容器化：Singing（仅 Linux + NVIDIA 有意义）
- 外部整合包：GPT-SoVITS、so-vits-svc 保持独立 runtime

原因是 macOS/Windows 的 Docker Desktop 运行在 Linux 虚拟机中，无法直接访问宿主摄像头、麦克风、屏幕、窗口系统和 GPU。强行全量容器化会失去桌面助手能力。

## 2. 现状

### 2.1 进程编排

`process_registry.py` 定义全部服务，`ServiceSupervisor` 负责：

- 按依赖顺序启动
- 等待每个服务写入就绪标记
- 失败时停止整棵依赖树
- 退出时逆序停止

就绪标记由 `common/service_ready.py` 写入 `_BINES_RUNTIME_DIR`，只有 supervisor 启动的子进程能写。容器无法共享这个机制，迁移后需要改用容器 healthcheck。

### 2.2 端口

| 用途 | 端口 | 当前暴露 |
| --- | --- | --- |
| ZMQ 总线 PUB/SUB | 5550 / 5551 | 127.0.0.1 |
| Visual REQ/REP | 5557 | 127.0.0.1 |
| RAG Server REQ/REP | 5560 | 127.0.0.1 |
| 模块就绪 REP | 5570 | 127.0.0.1 |
| 启动握手 REP | 5571 | 127.0.0.1 |
| Web 管理器 | 5000 | 127.0.0.1 |
| RAG Web | 5001 | 127.0.0.1 |
| QQ 网关 | 6102 | 127.0.0.1 |
| NapCat WS | 6101 → 容器 3001 | 127.0.0.1 |
| NapCat WebUI | 6099 | 127.0.0.1 |

### 2.3 环境

每个模块已有独立 venv：

- `.venv`：启动器、Flask 管理器、ChatBot、QQ Thinking
- `server/server_venv`：ZMQ Bus、Classification、Display、Speaking
- `thingking/thinking_venv`：Memory Server、RAG Web、桌面 Thinking
- `visual/envs/runtime_venv`
- `hearing/envs/runtime_venv`
- `singing/singing_venv`

Docker 化不是必须的隔离手段，但能减少“换机器重装依赖”的成本。

## 3. 目标架构

### 3.1 容器

| 服务 | 镜像 | 命令 | 依赖 | 宿主端口 |
| --- | --- | --- | --- | --- |
| napcat | `mlikiowa/napcat-docker` | 现有 compose | - | 6099、可选 6101 |
| zmq-bus | bines-server | `python common/zmq_bus_server.py` | - | 5550、5551 |
| classification | bines-server | `python server/classification_server.py` | zmq-bus | 无 |
| speaking | bines-server | `python speaking/main.py` | zmq-bus、外部 TTS | 无 |
| chatbot | bines-launcher | `python chatBot/main.py` | napcat、memory-server（full） | 可选 6102 |
| qq-thinking | bines-launcher | `python thingking/qq_thinking.py` | chatbot | 无 |
| memory-server | bines-thinking | `python thingking/rag_server.py` | 无 | 5560 |
| rag-web | bines-thinking | `python thingking/rag_web_server.py` | memory-server | 5001 |

### 3.2 宿主机

| 服务 | 原因 |
| --- | --- |
| Display | PyQt6 窗口必须显示在宿主桌面 |
| Visual | 摄像头、InsightFace、ONNX Runtime |
| Hearing | 麦克风、PortAudio、FunASR |
| Thinking | 截屏、pyautogui、窗口自动化 |
| Realtime Screen | 截屏与实时桌面状态 |
| Singing | 大模型、GPU、音频文件；macOS 无 CUDA，建议宿主运行 |
| GPT-SoVITS | 自带 runtime，单独进程 |
| Web 管理器 | 可选：继续在宿主启动，便于打开浏览器和管理宿主进程 |

### 3.3 网络

- `bines-net`：容器之间用服务名通信
  - ChatBot 连接 `ws://napcat:3001`
  - QQ Thinking 连接 `http://chatbot:6102`
  - RAG Web、ChatBot 连接 `tcp://memory-server:5560`
- 宿主侧模块通过 127.0.0.1 访问已映射端口：
  - ZMQ Bus：5550/5551
  - RAG Server：5560
  - RAG Web：5001
  - QQ 网关：6102（仅调试需要）

NapCat 的 6101 宿主映射可以保留用于兼容，也可以关闭，让 QQ 链路完全在内部网络通信。

## 4. 数据卷

| 卷 | 容器路径 | 内容 | 说明 |
| --- | --- | --- | --- |
| `bines-memory` | `/app/thingking/memory_data` | `memory.db`、`gateway.db`、Chroma `index/` | SQLite 不建议放在 macOS bind mount |
| `bines-napcat-data` | `/app/deploy/napcat/data` | QQ 登录、配置、媒体 | 沿用现有目录 |
| `bines-models` | `/app/thingking/models` | 嵌入模型 | 首次下载后可复用 |
| `bines-hf-cache` | `/root/.cache/huggingface` | HuggingFace 缓存 | 避免每次重建下载 |
| `bines-static` | `/app/static` | 音乐库、角色素材 | 可选只读 |
| `bines-logs` | `/app/logs` | 服务日志 | 可选 |

原则：

- 数据库、索引、模型放命名卷
- 配置文件和 `.env` 用只读 bind mount
- 不在容器内保存唯一副本

## 5. 就绪与依赖

现有 marker 机制不能跨容器使用，需要新增健康检查。建议：

| 服务 | healthcheck |
| --- | --- |
| napcat | TCP 6099 或 `/webui` 可访问 |
| zmq-bus | TCP 5550 |
| chatbot | `GET /health`（需网关 token） |
| memory-server | ZMQ `ping` |
| rag-web | `GET http://127.0.0.1:5001/` |
| classification | TCP 或进程存活 + 日志关键字 |
| qq-thinking | 进程存活；ChatBot 就绪后再启动 |

`docker compose` 的 `depends_on: condition: service_healthy` 负责容器内顺序；宿主机模块仍由 `ServiceSupervisor` 管理，并在启动 Thinking 前等待 `memory-server` 的 5560 端口可用。

> 注意：Compose 的 `depends_on` 只保证启动顺序，不保证业务真正可用。healthcheck 必须检查真实端点。

## 6. 启动流程改造

### 6.1 建议的新入口

```bash
python bines.py --profile chatbot --docker
python bines.py --profile full --docker
python bines.py --docker-down
```

行为：

1. `--docker`：先执行 `docker compose up -d`，等待容器健康
2. `chatbot` profile：只启动容器，不启动宿主 QQ 进程
3. `full` profile：容器启动后，再由 supervisor 启动 Display/Visual/Hearing/Thinking/Realtime Screen
4. `--docker-down`：停止容器，保留卷
5. 不带 `--docker`：完全保持现有行为

### 6.2 process_registry 改造

给每个服务增加运行时标记：

```python
"runtime": "docker"  # 或 "host"
```

supervisor 只启动 `runtime == "host"` 的服务；容器服务由 compose 管理。依赖关系仍保留一份，用于确认宿主服务需要等待哪些容器端点。

### 6.3 管理器页面

可选增强：

- 读取 `docker compose ps --format json`
- 显示容器状态、健康检查、最近日志
- 宿主进程继续用现有 supervisor

第一阶段可以不做，先用命令行管理。

## 7. 镜像设计

建议三个镜像，避免一个几 GB 的巨型镜像：

1. `bines-launcher`
   - 基础：`python:3.12-slim`
   - 依赖：`requirements/launcher.txt`
   - 用于 ChatBot、QQ Thinking
2. `bines-server`
   - 基础：`python:3.12-slim`
   - 依赖：`requirements/server.txt`
   - 用于 ZMQ Bus、Classification、Speaking
3. `bines-thinking`
   - 基础：`python:3.12-slim`
   - 依赖：`requirements/thinking.txt`，包含 Chroma、sentence-transformers、Torch
   - 用于 Memory Server、RAG Web
   - CPU 镜像较大，必须用多阶段构建和 `.dockerignore`

可选镜像 `bines-visual`、`bines-hearing`、`bines-singing`：仅在 Linux 上评估，需要 `/dev/video*`、`/dev/snd`、X11 socket 或 NVIDIA runtime。

`.dockerignore` 必须排除：

- `**/*_venv/`
- `**/envs/`
- `.venv/`
- `.tools/`
- `thingking/memory_data/`
- `deploy/napcat/data/`
- `static/`
- `__pycache__/`

## 8. 配置注入

新增或明确以下变量：

| 变量 | 作用 | 容器值 |
| --- | --- | --- |
| `BINES_NAPCAT_WS_URL` | NapCat 地址 | `ws://napcat:3001` |
| `BINES_QQ_GATEWAY_URL` | QQ Thinking 访问网关 | `http://chatbot:6102` |
| `BINES_RAG_RPC_HOST` | Memory Server 地址 | `memory-server` |
| `BINES_BUS_HOST` | ZMQ 总线地址 | `zmq-bus` |
| `BINES_HTTP_HOST` | Web 监听 | `0.0.0.0`（容器内） |
| `BINES_MEMORY_DIR` | 记忆目录 | `/app/thingking/memory_data` |
| `HF_HOME` | 模型缓存 | `/root/.cache/huggingface` |

当前 `chatBot/gateway_client.py` 默认连接 `127.0.0.1:6102`，容器内会指向自己，必须改为可配置地址。宿主机仍可用 127.0.0.1。

## 9. 分阶段迁移

### 阶段 0：只写 compose，不改代码

- 构建三个镜像
- 手工 `docker compose up` 启动 QQ 链路
- 验证容器间网络、记忆持久化、NapCat 登录
- 宿主 supervisor 仍按现状运行

风险最低，可先验证镜像和网络假设。

### 阶段 1：接入 `--docker` 启动模式

- 增加 `runtime` 标记
- supervisor 跳过容器服务
- 增加 compose 健康等待
- `--docker-down` 保留卷

### 阶段 2：完整模式混合编排

- 宿主 Display/Visual/Hearing/Thinking 连接容器 ZMQ/RAG
- 管理器页面显示容器状态
- 日志汇总

### 阶段 3：Linux 硬件模块（可选）

- Visual/Hearing 设备透传
- Singing + NVIDIA Container Toolkit
- 仅在有部署需求时做

## 10. 风险与限制

| 风险 | 影响 | 对策 |
| --- | --- | --- |
| macOS 无设备透传 | 摄像头/麦克风/桌面模块不可用 | 混合部署，模块留宿主 |
| macOS 无 CUDA | Singing 无法 GPU 加速 | 留宿主或 Linux 部署 |
| 镜像体积 | thinking 镜像较大 | 多阶段构建、共享基础层 |
| 模型重复下载 | 首次启动慢 | 挂载 HF 缓存和模型卷 |
| SQLite 在 bind mount 上性能差 | 记忆写入变慢/锁问题 | 使用命名卷 |
| 端口冲突 | 5000/5001/5550 等被占用 | 保持 127.0.0.1 映射并支持环境变量覆盖 |
| 文件权限 | 容器写入宿主文件属主异常 | 统一 UID/GID，数据库用命名卷 |
| 时区/locale | 日记日期、日志时间不一致 | 设置 `TZ` 与 `LANG` |
| 就绪判定不一致 | 启动过早/假就绪 | 真实 healthcheck，不使用固定 sleep |
| NapCat 与网关网络不一致 | QQ 无法连接 | 同一 compose 网络，使用服务名 |

## 11. 回滚

1. `docker compose down`（不加 `-v`，保留数据）
2. 继续使用现有 venv 启动方式
3. 容器与宿主共用/复制同一份 `memory_data` 和 `deploy/napcat/data` 时，先停容器再启动宿主
4. 不删除命名卷，除非确认已备份

## 12. 代码改动清单（待确认后实施）

| 文件 | 改动 |
| --- | --- |
| `docker/Dockerfile.*` | 新增三个镜像 |
| `docker-compose.yml` | 新增服务、卷、网络、healthcheck |
| `.dockerignore` | 排除 venv、数据、模型、缓存 |
| `process_registry.py` | 增加 `runtime` 标记 |
| `service_supervisor.py` | 跳过容器服务，等待容器端点 |
| `bines.py` / `start_modules.py` | 增加 `--docker`、`--docker-down` |
| `chatBot/gateway_client.py` | 网关地址改为环境变量 |
| `config.example.py` | 增加容器网络地址变量 |
| `common/service_ready.py` | 可选：容器健康端点 |
| `docs/DOCKER_MIGRATION.md` | 本方案 |
| 测试 | compose 配置校验、容器健康、QQ 链路回归 |

## 13. 验收标准

- `docker compose config` 通过
- `--profile chatbot --docker` 启动后 QQ 私聊/群聊正常
- 关闭再启动，记忆与 NapCat 登录保留
- `--profile full --docker` 下宿主 Thinking 能连接容器 Memory Server
- 现有不带 `--docker` 的启动方式完全不受影响
- 容器日志可定位失败原因，不把日志混进主进程输出

## 14. 建议

先做阶段 0，只验证 QQ 链路的镜像与网络。确认稳定后再改启动器。

如果目标只是“换机器好部署”，也可以先做一件事：把现有 `install.py` 与 requirements 做成可复现构建；它比全量 Docker 化成本低，且不牺牲桌面能力。

## 15. 已落地的最小 Docker 部署

资产位于 `deploy/docker/Dockerfile.launcher`、`deploy/docker/Dockerfile.thinking`、`deploy/docker/compose.yaml` 与仓库根目录 `.dockerignore`。

启动入口不再需要手写 compose 命令，`bines.py` 会调用 `docker_launcher.py` 自动处理 UID/GID、env-file、profile、健康等待和停止：

```bash
python bines.py                      # 完整模式：Docker QQ/记忆链路 + 宿主桌面模块
python bines.py --profile chatbot    # 只跑 QQ：Docker 链路并跟随日志
python bines.py --host               # 纯宿主机模式，不启动 Docker
python bines.py --external-napcat    # 连接已有 NapCat，走宿主机模式
```

- `chatbot` 模式启动 `napcat`、`chatbot`、`qq-thinking`、`memory-worker`，记忆嵌入网关。
- `full` 模式额外启动 `memory-server` 与 `rag-web`，并在容器健康后启动宿主机桌面模块；宿主机不再重复启动 ChatBot、QQ Thinking、Memory Worker、RAG Server 和 RAG Web。
- `bines.py` 启动时自动设置 `BINES_UID`/`BINES_GID`、`BINES_PROFILE` 和 `--env-file deploy/napcat/.env`；镜像存在且内嵌源码不旧于仓库时不追加 `--build`，代码或 Dockerfile 更新后会自动重建。
- Ctrl+C 只停止本次新启动的容器；启动前已在运行的容器保持不动，数据卷始终保留。
- 首次使用前可运行 `python napcat.py init` 同步根 `.env` 中的 NapCat 令牌。
- 首次登录或登录态失效时，二维码在 NapCat 容器日志中：`python napcat.py logs`。网页 `http://127.0.0.1:6099` 需要 WebUI token，不是扫码页，可用 `python napcat.py webui` 打开。
- 首次构建 `thinking` 镜像会下载 PyTorch 与嵌入模型，耗时与镜像体积都明显大于 launcher；`install.py` 默认只构建 launcher，完整模式需要 `python install.py --docker-full` 预构建 thinking 镜像。
- 手动运维仍可使用 compose（调试用，日常启动请用 `bines.py`）：

```sh
docker compose --env-file deploy/napcat/.env -f deploy/docker/compose.yaml ps
docker compose --env-file deploy/napcat/.env -f deploy/docker/compose.yaml logs -f --tail 80
docker compose -f deploy/docker/compose.yaml down --remove-orphans   # 不要加 -v
```

- 当前 `bines-launcher:local` 已构建，QQ 链路容器已启动；NapCat 登录态失效时需按上面的日志命令重新扫码。
