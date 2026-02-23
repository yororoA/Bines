"""
播放 static/music/cover 目录下的本地音频（供 Display 扬声器播放）。
依赖 handle_zmq 绑定的 AUDIO_PLAY_PUB 并已注册到 deps.audio_play_pub_socket。
"""
import base64
import json
import os
import sys
from pathlib import Path

_current_dir = os.path.dirname(os.path.abspath(__file__))
_parent_dir = os.path.dirname(_current_dir)
if _parent_dir not in sys.path:
    sys.path.insert(0, _parent_dir)

from config import ROOT_DIR

COVER_DIR = ROOT_DIR / "static" / "music" / "cover"


def get_sing_list():
    """返回 static/music/cover 下可播放的文件名列表（相对 cover 的路径），供 schema 枚举与模型选择。"""
    if not COVER_DIR.exists():
        return []
    out = []
    for f in COVER_DIR.rglob("*"):
        if f.is_file():
            try:
                rel = f.relative_to(COVER_DIR)
                out.append(str(rel).replace("\\", "/"))
            except ValueError:
                pass
    return sorted(out)


def sing(filename: str) -> str:
    """
    播放 static/music/cover 下的音频文件，通过 AUDIO_PLAY_PUB 发往 Display 播放。
    filename 由工具 schema 的枚举提供，请从枚举中直接选择，不要自行输入。
    """
    if not filename or not filename.strip():
        return "错误：请提供文件名（例如 song.mp3）。"
    filename = filename.strip().replace("\\", "/").lstrip("/")
    if ".." in filename or filename.startswith("/"):
        return "错误：不允许使用路径穿越，仅支持 cover 目录下的文件名或子路径。"
    path = (COVER_DIR / filename).resolve()
    try:
        if not path.is_file():
            return f"错误：文件不存在或不是文件：{path}。"
        if not path.resolve().is_relative_to(COVER_DIR.resolve()):
            return "错误：文件必须在 static/music/cover 目录下。"
    except (ValueError, OSError):
        return "错误：无效路径。"
    try:
        audio_bytes = path.read_bytes()
    except Exception as e:
        return f"错误：读取文件失败：{e}。"
    if not audio_bytes:
        return "错误：文件为空。"
    try:
        from tools.dependencies import deps
        sock = getattr(deps, "audio_play_pub_socket", None)
    except Exception:
        sock = None
    if not sock:
        return "错误：音频播放通道未就绪（Display/Thinking 未启动或未注册 AUDIO_PLAY_PUB）。"
    try:
        payload = {"voice": base64.b64encode(audio_bytes).decode("ascii"), "sender": "sing"}
        sock.send_multipart([b"tts", json.dumps(payload).encode("utf-8")])
    except Exception as e:
        return f"错误：发送音频失败：{e}。"
    return f"已播放: {filename}"
