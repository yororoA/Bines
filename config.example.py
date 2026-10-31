"""Application defaults. Configure this installation in the root .env file."""
import os
from pathlib import Path
from common.env import load_env
from runtime_environments import environment_python

ROOT_DIR = Path(__file__).resolve().parent
load_env()
PRESENCE_STATE_PATH = ROOT_DIR / "presence_state.json"
BROWSER_AUTOMATION_USER_DATA_DIR = str(ROOT_DIR / "static" / "Bines Data Automation")

# One local PUB/SUB bus plus the small set of actual request/reply services.
ZMQ_HOST = os.environ.get("BINES_ZMQ_HOST", "127.0.0.1")
MEMORY_BIND_HOST = os.environ.get("BINES_MEMORY_BIND_HOST", "127.0.0.1")
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

# Text, vision and Search/Fetch share one DeepSeek credential. ASR/TTS remain separate.
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-flash")
DEEPSEEK_API_TIMEOUT = int(os.environ.get("DEEPSEEK_API_TIMEOUT", "60"))
# deepseek-flash reasons by default; "disabled" keeps short classification calls from returning empty text.
DEEPSEEK_THINKING = os.environ.get("DEEPSEEK_THINKING", "disabled")
_DEEPSEEK_API_ROOT = DEEPSEEK_BASE_URL.rstrip("/").removesuffix("/v1")
DEEPSEEK_VISION_API_URL = f"{_DEEPSEEK_API_ROOT}/chat/completions"
DEEPSEEK_VISION_MODEL = os.environ.get("DEEPSEEK_VISION_MODEL", "deepseek-flash")
DEEPSEEK_VISION_TIMEOUT = int(os.environ.get("DEEPSEEK_VISION_TIMEOUT", "60"))
DEEPSEEK_VISION_DETAIL = os.environ.get("DEEPSEEK_VISION_DETAIL", "original")
DEEPSEEK_SEARCH_URL = f"{_DEEPSEEK_API_ROOT}/v1/tools/search"
DEEPSEEK_FETCH_URL = f"{_DEEPSEEK_API_ROOT}/v1/tools/fetch"
DEEPSEEK_WEB_TIMEOUT = int(os.environ.get("DEEPSEEK_WEB_TIMEOUT", "30"))
DEEPSEEK_SEARCH_COUNT = int(os.environ.get("DEEPSEEK_SEARCH_COUNT", "5"))

TTS_HTTP_BASE_URL = os.environ.get("BINES_TTS_URL", "http://127.0.0.1:9880")
# Optional local GPT-SoVITS install; when set, the module manager starts its api_v2.py.
GPT_SOVITS_DIR = os.environ.get("GPT_SOVITS_DIR", "")

# Singing: separate vocals -> so-vits-svc -> remix, cached per song (see singing/README.md).
SOVITS_SVC_DIR = os.environ.get("SOVITS_SVC_DIR", "")
_INSTALLED_SINGING_PYTHON = environment_python("singing", ROOT_DIR)
SINGING_PYTHON = os.environ.get("SINGING_PYTHON") or (
    str(_INSTALLED_SINGING_PYTHON) if _INSTALLED_SINGING_PYTHON.is_file() else ""
)
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

# Historical DashScope constants are aliases for old external consumers.
# All in-repository vision callers use the DeepSeek names below.
DASHSCOPE_API_URL = DEEPSEEK_VISION_API_URL
DASHSCOPE_API_KEY = DEEPSEEK_API_KEY
DASHSCOPE_VISION_MODEL = DEEPSEEK_VISION_MODEL
DASHSCOPE_API_TIMEOUT = DEEPSEEK_VISION_TIMEOUT
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
        raise RuntimeError(f"请在项目 .env 文件中配置 {name}。")
    return value
