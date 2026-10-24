"""Safe starter configuration. Copy to config.py and set credentials in the environment."""
import os
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent


def _load_dotenv(path: Path) -> None:
    # Existing process/system environment variables take precedence over .env.
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


_load_dotenv(ROOT_DIR / ".env")
PRESENCE_STATE_PATH = ROOT_DIR / "presence_state.json"
BROWSER_AUTOMATION_USER_DATA_DIR = str(ROOT_DIR / "static" / "Bines Data Automation")

# One local PUB/SUB bus plus the small set of actual request/reply services.
ZMQ_HOST = os.environ.get("BINES_ZMQ_HOST", "127.0.0.1")
ZMQ_PORTS = {
    "BUS_PUBLISH": int(os.environ.get("BINES_BUS_PUBLISH_PORT", "5550")),
    "BUS_SUBSCRIBE": int(os.environ.get("BINES_BUS_SUBSCRIBE_PORT", "5551")),
    "VISUAL_REQREP": int(os.environ.get("BINES_VISUAL_PORT", "5557")),
    "RAG_SERVER_REQREP": int(os.environ.get("BINES_RAG_RPC_PORT", "5560")),
    "MODULE_READY_REP": int(os.environ.get("BINES_MODULE_READY_PORT", "5570")),
    "START_THINKING_REP": int(os.environ.get("BINES_THINKING_START_PORT", "5571")),
}

HTTP_HOST = os.environ.get("BINES_HTTP_HOST", "127.0.0.1")
HTTP_PORTS = {
    "MODULE_MANAGER": int(os.environ.get("BINES_MANAGER_PORT", "5000")),
    "RAG_MEMORY": int(os.environ.get("BINES_RAG_WEB_PORT", "5001")),
}

_all_ports = {
    **{f"ZMQ.{name}": port for name, port in ZMQ_PORTS.items()},
    **{f"HTTP.{name}": port for name, port in HTTP_PORTS.items()},
}
_invalid_ports = [f"{name}={port}" for name, port in _all_ports.items() if not 1 <= port <= 65535]
_port_owners = {}
_duplicate_ports = []
for _name, _port in _all_ports.items():
    if _port in _port_owners:
        _duplicate_ports.append(f"{_port} ({_port_owners[_port]} / {_name})")
    else:
        _port_owners[_port] = _name
if _invalid_ports or _duplicate_ports:
    raise ValueError(
        "Invalid Bines port configuration: "
        + "; ".join([*_invalid_ports, *_duplicate_ports])
    )

# Hearing / FunASR model configuration.
HEARING_ASR_MODEL = os.environ.get(
    "HEARING_ASR_MODEL",
    "iic/speech_paraformer-large-contextual_asr_nat-zh-cn-16k-common-vocab8404",
)
HEARING_ASR_MODEL_REVISION = os.environ.get("HEARING_ASR_MODEL_REVISION", "v2.0.4")
HEARING_LM_MODEL = os.environ.get(
    "HEARING_LM_MODEL",
    "iic/speech_transformer_lm_zh-cn-common-vocab8404-pytorch",
)
HEARING_LM_MODEL_REVISION = os.environ.get("HEARING_LM_MODEL_REVISION", "v2.0.4")
HEARING_LM_WEIGHT = float(os.environ.get("HEARING_LM_WEIGHT", "0.15"))
HEARING_BEAM_SIZE = int(os.environ.get("HEARING_BEAM_SIZE", "10"))
HEARING_PUNC_MODEL = os.environ.get("HEARING_PUNC_MODEL", "ct-punc")
HEARING_PUNC_MODEL_REVISION = os.environ.get("HEARING_PUNC_MODEL_REVISION", "v2.0.4")
HEARING_ASR_DEVICE = os.environ.get("HEARING_ASR_DEVICE", "cuda")
HEARING_VAD_MODEL = os.environ.get("HEARING_VAD_MODEL", "fsmn-vad")

# All text tasks use this single credential and model. Vision/ASR/TTS remain separate modalities.
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-flash")
DEEPSEEK_API_TIMEOUT = int(os.environ.get("DEEPSEEK_API_TIMEOUT", "60"))
# deepseek-flash reasons by default; "disabled" keeps short classification calls from returning empty text.
DEEPSEEK_THINKING = os.environ.get("DEEPSEEK_THINKING", "disabled")

TTS_HTTP_BASE_URL = os.environ.get("BINES_TTS_URL", "http://127.0.0.1:9880")
# Optional local GPT-SoVITS install; when set, the module manager starts its api_v2.py.
GPT_SOVITS_DIR = os.environ.get("GPT_SOVITS_DIR", "")

# Singing: separate vocals -> so-vits-svc -> remix, cached per song (see singing/README.md).
SOVITS_SVC_DIR = os.environ.get("SOVITS_SVC_DIR", "")
SINGING_PYTHON = os.environ.get("SINGING_PYTHON", "")
SINGING_WORK_DIR = Path(os.environ.get("SINGING_WORK_DIR", str(ROOT_DIR / "singing" / "work")))
SINGING_LIBRARY_DIR = Path(os.environ.get("SINGING_LIBRARY_DIR", str(ROOT_DIR / "static" / "music" / "library")))
SINGING_SVC_MODEL = os.environ.get("SINGING_SVC_MODEL", "logs/44k/G_36000.pth")
SINGING_SVC_DIFFUSION_MODEL = os.environ.get("SINGING_SVC_DIFFUSION_MODEL", "logs/44k/diffusion/model_100000.pt")
SINGING_SPEAKER = os.environ.get("SINGING_SPEAKER", "ATRI")
NAPCAT_WS_URL = os.environ.get("BINES_NAPCAT_WS_URL", "ws://127.0.0.1:6101")
NAPCAT_ACCESS_TOKEN = os.environ.get("BINES_NAPCAT_ACCESS_TOKEN", "")
MOMENTS_UID = os.environ.get("MOMENTS_UID", "")
MOMENTS_TOKEN = os.environ.get("MOMENTS_TOKEN", "")
MOMENTS_API_BASE_URL = os.environ.get("MOMENTS_API_BASE_URL", "")
MOMENTS_API_TIMEOUT = int(os.environ.get("MOMENTS_API_TIMEOUT", "15"))
TOGGLE_STATUS_TOKEN = os.environ.get("TOGGLE_STATUS_TOKEN", "")

# Ark 独立身份：在 Ark /bines 管理页签发。配置后不会回落旧 REST。
ARK_TRANSPORT = os.environ.get("ARK_TRANSPORT", "auto")  # auto | mcp | legacy
ARK_MCP_URL = os.environ.get("ARK_MCP_URL", "")
ARK_MCP_UID = os.environ.get("ARK_MCP_UID", "")  # 与 arkserver 的 BINES_UID 一致
ARK_MCP_TOKEN = os.environ.get("ARK_MCP_TOKEN", "")
ARK_MCP_TIMEOUT = float(os.environ.get("ARK_MCP_TIMEOUT", "20"))

DASHSCOPE_API_URL = os.environ.get(
    "DASHSCOPE_API_URL",
    "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
)
DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
DASHSCOPE_VISION_MODEL = os.environ.get("DASHSCOPE_VISION_MODEL", "qwen3-vl-flash")
DASHSCOPE_API_TIMEOUT = int(os.environ.get("DASHSCOPE_API_TIMEOUT", "30"))
VISUAL_FACE_MODEL = os.environ.get("VISUAL_FACE_MODEL", "buffalo_l")

_LOCAL_EMBEDDING_DIR = ROOT_DIR / "thingking" / "models" / "all-MiniLM-L6-v2"
RAG_EMBEDDING_MODEL = str(_LOCAL_EMBEDDING_DIR) if _LOCAL_EMBEDDING_DIR.is_dir() else "sentence-transformers/all-MiniLM-L6-v2"
HF_ENDPOINT_DEFAULT = os.environ.get("HF_ENDPOINT", "https://hf-mirror.com")
HF_DOWNLOAD_TIMEOUT = int(os.environ.get("HF_DOWNLOAD_TIMEOUT", "300"))

VOCU_BASE_URL = os.environ.get("VOCU_BASE_URL", "https://v1.vocu.ai")
VOCU_API_KEY = os.environ.get("VOCU_API_KEY", "")
VOCU_MODEL_ID = os.environ.get("VOCU_MODEL_ID", "")


def require_env(name: str, value: str | None) -> str:
    if not value:
        raise RuntimeError(f"Environment variable '{name}' is required but not set.")
    return value
