# 唱歌（翻唱）流水线

把任意原曲换成 Bines 的声音：

1. `separate.py`（singing venv）：统一格式 → BS-RoFormer 分离人声/伴奏 → Karaoke 模型拆出主唱/和声 → MelBand De-Reverb 去混响，得到主唱干声。
2. `svc_convert.py`（so-vits-svc 自带 Python）：按静音切成乐句逐段换声（RMVPE 音高 + 浅扩散），每段按序号固定随机种子，结果与分段方式无关、可复现。
3. `mix.py`（singing venv）：换声干声对齐原唱响度 → 重新加混响 → 与伴奏、和声混音 → 限幅 → 标准化到 -14 LUFS，输出 MP3。

`pipeline.py` 负责调度和缓存（按原曲内容哈希），`tools/sing_tool.py` 提供 `list_songs` / `sing` 两个动作（`skills/singing/SKILL.md`）。

## 歌曲来源

- `static/music/cover/`：手工做好的翻唱，直接播放。
- `static/music/library/`（`SINGING_LIBRARY_DIR`）：放原曲（mp3/flac/wav/m4a/mp4 等均可）。首次演唱在后台处理，RTX 4060 Laptop 上一首 3～4 分钟的歌约 4～5 分钟，之后直接播放缓存。

## 环境

`.env` 中配置：

```
SOVITS_SVC_DIR=D:\so-vits-svc-cu128-250715\so-vits-svc-cu128
SINGING_PYTHON=D:\bines-singing\venv\Scripts\python.exe
SINGING_WORK_DIR=D:\bines-singing
```

可选：`SINGING_SVC_MODEL`（默认 `logs/44k/G_36000.pth`）、`SINGING_SVC_DIFFUSION_MODEL`、`SINGING_SPEAKER`（默认 `ATRI`）、`SINGING_LIBRARY_DIR`。

singing venv（Python ≥ 3.10，so-vits-svc 整合包是 3.9，不能共用）：

```powershell
py -3.12 -m venv D:\bines-singing\venv
$env:PIP_USER='0'
D:\bines-singing\venv\Scripts\python.exe -m pip install "audio-separator[gpu]" pedalboard pyloudnorm audioread imageio-ffmpeg huggingface_hub
# audio-separator 会把 torch 升级成 CPU 版，装回 CUDA 版：
D:\bines-singing\venv\Scripts\python.exe -m pip install --force-reinstall --no-deps torch==2.7.1 torchaudio==2.7.1 torchvision==0.22.1 --index-url https://download.pytorch.org/whl/cu118
# 预下载分离模型（权重走 Hugging Face 镜像，GitHub Releases 在国内经常卡住）：
cd singing; D:\bines-singing\venv\Scripts\python.exe fetch_models.py --model-dir D:\bines-singing\models
```

工作目录结构：`models/` 分离模型，`covers/<key>/` 每首歌的缓存（`final.mp3`、可重新混音用的 `instrumental/backing/lead_dry/converted.wav`、`meta.json`、`pipeline.log`），`tmp/` 临时文件。

命令行单独跑一首：

```powershell
.\thingking\thinking_venv\Scripts\python.exe singing\pipeline.py "static\music\library\某首歌.mp3"
```
