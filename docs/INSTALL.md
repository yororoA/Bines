# 一键安装依赖

在项目根目录执行：

```bash
python3 install.py
```

Windows 可用 `python install.py`，也可双击 `install.bat`；macOS 可双击 `install.command`。默认安装完整的 6 组 Python 环境。安装脚本自身只依赖标准库，需要电脑上已有 Python 3.10 或更新版本用于启动；实际运行环境统一使用 64 位 Python 3.11/3.12。安装器也会离线查找 uv 已下载的 Python，不要求它出现在 PATH。找不到兼容版本时，通过 uv 和南京大学公开镜像下载 Python 3.12 到项目 `.tools/python`，不修改系统 Python、PATH 或 Windows Python 注册项。

安装结束后填写 `.env` 中的密钥和主人 QQ，再运行 `python bines.py`。默认自动构建并启动 Docker QQ 链路；完整模式再启动宿主机桌面模块。需要纯宿主机运行时加 `--host`。

## 安装范围与命令

```bash
# 只装 QQ、DeepSeek、MCP 和 Web 管理器，不装摄像头/ASR/Torch
python install.py --profile chatbot

# 重试或补装某些环境，会自动包含 launcher
python install.py --only visual hearing
python install.py --only singing

# NVIDIA GPU；需 CUDA 12.8 兼容驱动，仅 Windows/Linux x86_64
python install.py --device cuda

# 已有 Python / 自定义包镜像（默认已使用清华 PyPI）
python install.py --python /path/to/python3.12
python install.py --index-url https://pypi.tuna.tsinghua.edu.cn/simple
# 禁止下载 Python，只使用已安装的兼容解释器
python install.py --no-python-download
# Python 安装包镜像（默认南京大学，与上面的 pip 包镜像不同）
python install.py --python-mirror https://mirror.nju.edu.cn/github-release/astral-sh/python-build-standalone
# 慢速网络可调整 Python 下载的总时限，默认 180 秒
python install.py --python-download-timeout 300

# 可选：通过已有系统包管理器安装音视频依赖
python install.py --system

# 不安装 Docker Desktop（使用外部 NapCat 或稍后自行安装）
python install.py --skip-docker

# 缓存和安装日志目录；默认项目内 .tools/cache，不写系统盘
python install.py --cache-dir D:\BinesCache

# 完整安装时同时构建 Docker thinking 镜像（体积较大，供 full 模式使用）
python install.py --docker-full

# 查看安装计划 / 只验证当前环境
python install.py --dry-run
python install.py --check
python install.py --profile chatbot --check
```

`--device` 默认为 `cpu`，跨平台可用；CUDA 版固定安装配套 Torch 2.7.1、torchaudio 2.7.1、torchvision 0.22.1，ONNX 使用 CUDA/cuDNN 依赖包。Windows/Linux 的 PyTorch 从官方 CPU/cu128 索引安装，macOS 从 PyPI 或指定镜像安装。切换 CPU/CUDA 时会重新安装不匹配的 Torch 构建，思考、听觉和唱歌依赖统一受版本约束，避免音频依赖替换 Torch。CUDA 驱动仍需由系统提供。

| 环境 | 目录 | 覆盖能力 |
| --- | --- | --- |
| launcher | `.venv` | 启动器、Flask 管理器、QQ、DeepSeek、MCP |
| server | `server/server_venv` | PyQt6 桌面显示、消息总线、分类、Speaking HTTP 客户端 |
| thinking | `thingking/thinking_venv` | 桌面 Thinking、Memory Server（SQLite/Chroma/嵌入模型）、记忆管理、屏幕分析、OCR 和当前平台自动化 |
| visual | `visual/envs/runtime_venv` | 摄像头、人脸识别、ONNX Runtime |
| hearing | `hearing/envs/runtime_venv` | FunASR、PyAudio、声纹、Torch |
| singing | `singing/singing_venv` | 人声分离、去混响、混音 |

统一使用 `install.py`；macOS 和 Windows 双击入口也调用它。清单集中在 `requirements/`，环境路径集中在 `runtime_environments.py`，安装器和启动器共同使用。旧 UTF-16 requirements/venv 快照保留备查，不作为新安装源。Memory Server 由 thinking 环境安装 Chroma、LangChain HuggingFace 和嵌入模型依赖；完整模式与纯 QQ 模式都会启动它并提供向量 RAG，纯 QQ 只额外安装 launcher，同时支持新版人格、摘要、长期事实和日记。旧未启用的手势识别、物体检测和 Selenium 浏览器实现不在默认安装范围内。

Windows 上 pip/uv 默认把数 GB 缓存写到 `%LOCALAPPDATA%` 和 `%TEMP%`。安装器现在默认把 pip 缓存、uv 缓存、构建临时文件和 HuggingFace 模型都放在项目目录 `.tools/cache`，也可用 `--cache-dir` 或环境变量 `BINES_CACHE_DIR` 指到其他盘。之前版本留下的 `%LOCALAPPDATA%\pip\Cache`、`%LOCALAPPDATA%\uv\cache` 和 `%TEMP%\bines-install-*` 可以手动删除。Docker Desktop 本身由 winget 安装到 `C:\Program Files\Docker`，镜像与 WSL 虚拟磁盘也默认在 C 盘；不想占用 C 盘时可用 `--skip-docker`，改用已有 Docker 或外部 NapCat。

## 系统依赖

完整环境需要 Python wheel 之外的系统库。默认只安装 Python 依赖，系统缺失项会单独列出；`--system` 会使用已安装的 Homebrew、apt 或 winget，安装器不自动安装这些包管理器。

Docker Desktop 单独处理：macOS 和 Windows 缺少 Docker 时默认自动安装。macOS 从 Docker 官方地址下载约 600 MB 的对应架构 DMG，校验应用签名后弹出一次系统管理员授权，安装器不会代替用户接受 Docker 协议；Docker Desktop 随后自动打开，首次需在其窗口阅读并接受协议。Windows 通过 winget 安装到 `C:\Program Files\Docker`，镜像与 WSL 磁盘也在 C 盘，介意时加 `--skip-docker` 使用已有 Docker 或外部 NapCat。自动安装失败不会中断 Python 依赖，可重新运行续传，或使用 `--skip-docker`。Linux 不自动修改系统软件仓库。

- macOS：先安装 Xcode Command Line Tools（`xcode-select --install`），并通过 Homebrew 准备 `ffmpeg portaudio pkg-config tesseract tesseract-lang cmake libsamplerate`。也可使用 `--system` 安装这些 Homebrew 包。
- Debian/Ubuntu：准备 `build-essential pkg-config cmake portaudio19-dev libsamplerate0-dev libsndfile1 ffmpeg tesseract-ocr tesseract-ocr-chi-sim libgl1 libxcb-cursor0`。`--system` 在交互终端使用 sudo，仅系统包安装提权，Python 环境仍由当前用户创建。
- Windows：InsightFace 等扩展需要 Visual Studio Build Tools 的 C++ 工作负载及 Windows SDK；FFmpeg 必须包含 `ffplay`。`--system` 可通过 winget 安装 FFmpeg 和 Tesseract。安装后的新 PATH 可能需要重新打开终端，中文 OCR 需 `chi_sim` 语言包。

Apple Silicon、Windows/Linux x86_64 是完整安装的主要目标；Intel macOS 没有本项目所用 Torch 2.7 的官方 wheel，思考/听觉/唱歌组会明确报告不支持，其余组可继续安装。macOS 上的 Windows UI Automation/网易云控件操作不会因为安装依赖而变成跨平台能力。

## 配置与重复安装

- 已有兼容虚拟环境会复用，pip 安装满足清单的版本；安装器不删除环境、模型或记忆。请先停止正在运行的 Bines 再升级依赖。
- Python 优先从系统命令、已有项目虚拟环境、`.tools/python` 和 uv 用户缓存查找，重复运行不需要再次下载解释器。用户缓存查找禁用网络和自动下载，兼容自定义 `UV_PYTHON_INSTALL_DIR`。
- 检测到非虚拟环境目录、损坏环境或不兼容 Python 时，该组停止并报告原因，需要用户先备份并移走旧目录。
- 不并发修改同一组环境；每组结束执行 `pip check` 与核心模块导入核验。QQ 组额外核验记忆网关、人格、提示词和摘要模块，确保轻量入口不会提前加载 Chroma、Torch 或桌面驱动。不会打开摄像头/麦克风或发 QQ 消息。
- `.env` 不存在时从 `.env.example` 创建；仅补齐不存在的 `BINES_PROFILE`、`HEARING_ASR_DEVICE` 和 `SINGING_PYTHON`，保留已有凭据、显式空值和用户配置。
- 输出仅展示步骤、进度和成功/失败；pip 和编译器详细日志放在系统临时目录，失败时显示具体日志路径。部分失败时退出码为 1，已成功的环境保留。
- Python 包镜像用 `--index-url` 指定。安装器忽略个人 pip 安装目标配置，避免 `--user`、`--target` 等将包写到虚拟环境之外；网络代理仍遵循进程环境。
- Python 安装包下载与 pip 下载使用独立地址：Python 默认使用南京大学的 `python-build-standalone` GitHub Release 镜像，普通包默认使用清华 PyPI；不使用内部软件源。`--index-url` 不会改变 Python 下载源，也不会替换 Windows/Linux 的 PyTorch 官方 CPU/CUDA 索引。可用环境变量 `BINES_PYTHON_MIRROR`、`BINES_PYPI_INDEX_URL` 或同名命令参数覆盖。下载连接限时 15 秒、网络读取限时 45 秒，最多重试一次，总等待默认不超过 180 秒。

`thingking/tests` 的记忆回归使用 Chroma，需先安装 `thinking` 组再用该组 Python 运行；只安装 QQ 组不足以运行这些向量检索测试。

## 不包含的外部资源

安装 Python 依赖不等于模型和外部应用就绪：

- NapCat 由启动器自动下载 Docker 镜像；Docker Desktop 缺失时由依赖安装器自动安装，启动 Bines 时也会自动打开并等待 Docker 引擎。首次 Docker 协议确认仍需用户完成。QQ 登录二维码位于 `python3 napcat.py logs` 的容器日志中；端口 6099 是需要 token 的管理页，使用 `python3 napcat.py webui` 可自动携带 token 打开。
- 首次 NapCat 镜像拉取与容器创建分开执行：下载默认最多等待 1800 秒，并每 30 秒显示进度；可用 `BINES_NAPCAT_PULL_TIMEOUT` 调整。已有镜像时不会联网检查更新，需更新时执行 `python napcat.py update`。
- GPT-SoVITS、so-vits-svc 整合包及个人音色权重使用自己的运行环境，不往其中批量覆盖依赖；路径分别设置在 `GPT_SOVITS_DIR`、`SOVITS_SVC_DIR`。
- ASR/人脸/记忆嵌入模型沿用模块的首次运行下载流程；唱歌分离模型首次使用时下载，个人换声音色模型不能自动生成。
- 桌面角色图片、TTS 参考音频、操作系统摄像头/麦克风/录屏权限需要按实际环境配置。
