"""唱歌：播放现成翻唱，或把歌库里的原曲现场换成 Bines 的声音后播放。

- static/music/cover/ 下是手工做好的翻唱，直接播放；
- SINGING_LIBRARY_DIR（默认 static/music/library/）下放原曲，首次演唱时在后台跑
  singing/pipeline.py（分离 → 换声 → 混音），完成后自动播放，结果按歌曲缓存。
音频经 deps.audio_play_pub_socket（线程安全的队列发布器）发往 Display 播放。
"""
import base64
import json
import os
import sys
import threading
from pathlib import Path

_current_dir = os.path.dirname(os.path.abspath(__file__))
_parent_dir = os.path.dirname(_current_dir)
if _parent_dir not in sys.path:
    sys.path.insert(0, _parent_dir)

from config import ROOT_DIR, SINGING_LIBRARY_DIR

COVER_DIR = ROOT_DIR / "static" / "music" / "cover"
LIBRARY_DIR = Path(SINGING_LIBRARY_DIR)

_pending_lock = threading.Lock()
_pending: dict = {}  # 歌名 -> 当前进度说明


def _audio_files(directory: Path):
    from singing.pipeline import AUDIO_SUFFIXES

    if not directory.is_dir():
        return []
    return sorted(p for p in directory.rglob("*") if p.is_file() and p.suffix.lower() in AUDIO_SUFFIXES)


def _relative(path: Path, base: Path) -> str:
    return str(path.relative_to(base)).replace("\\", "/")


def get_sing_list():
    """返回 static/music/cover 下现成翻唱的相对路径列表。"""
    return [_relative(p, COVER_DIR) for p in _audio_files(COVER_DIR)]


def _find(song: str, directory: Path):
    """按相对路径、文件名或不含扩展名的歌名匹配（不区分大小写，允许部分匹配）。"""
    wanted = song.strip().replace("\\", "/").lower()
    files = _audio_files(directory)
    for path in files:
        rel = _relative(path, directory).lower()
        if wanted in (rel, path.name.lower(), path.stem.lower()):
            return path
    matches = [p for p in files if wanted in p.stem.lower()]
    return matches[0] if len(matches) == 1 else None


def _play_file(path: Path, label: str) -> str:
    try:
        from tools.dependencies import deps
        sock = getattr(deps, "audio_play_pub_socket", None)
    except Exception:
        sock = None
    if not sock:
        return "错误：音频播放通道未就绪（Display/Thinking 未启动或未注册 AUDIO_PLAY_PUB）。"
    try:
        audio_bytes = path.read_bytes()
    except OSError as e:
        return f"错误：读取文件失败：{e}。"
    if not audio_bytes:
        return "错误：文件为空。"
    try:
        payload = {"voice": base64.b64encode(audio_bytes).decode("ascii"), "sender": "sing"}
        sock.send_multipart([b"tts", json.dumps(payload).encode("utf-8")])
    except Exception as e:
        return f"错误：发送音频失败：{e}。"
    return f"已播放: {label}"


def _prepare_in_background(src: Path) -> None:
    from singing.pipeline import prepare_cover

    title = src.stem

    def progress(message: str) -> None:
        with _pending_lock:
            _pending[title] = message

    def run() -> None:
        try:
            final = prepare_cover(src, progress=progress)
            print(f"[Sing] 翻唱完成: {title} -> {final}", flush=True)
            print(f"[Sing] {_play_file(final, title)}", flush=True)
        except Exception as e:
            print(f"[Sing] 翻唱失败: {title}: {e}", flush=True)
        finally:
            with _pending_lock:
                _pending.pop(title, None)

    with _pending_lock:
        _pending[title] = "排队中"
    threading.Thread(target=run, name=f"sing-{title}", daemon=True).start()


def list_songs() -> str:
    """列出可以唱的歌：现成翻唱、已缓存的歌库歌曲，以及首次演唱需要先准备几分钟的歌库原曲。"""
    from singing.pipeline import cached_cover

    ready = get_sing_list()
    library_ready, library_new = [], []
    for path in _audio_files(LIBRARY_DIR):
        (library_ready if cached_cover(path) else library_new).append(_relative(path, LIBRARY_DIR))
    with _pending_lock:
        preparing = dict(_pending)
    return json.dumps({
        "ready": ready + library_ready,
        "needs_preparation": [s for s in library_new if Path(s).stem not in preparing],
        "preparing": preparing,
    }, ensure_ascii=False)


def sing(song: str) -> str:
    """唱一首歌。song 取自 list_songs 返回的歌名；歌库原曲首次演唱会先在后台准备几分钟，完成后自动开唱。"""
    if not song or not song.strip():
        return "错误：请提供歌名（可先调用 list_songs 查看）。"
    if ".." in song.replace("\\", "/").split("/"):
        return "错误：不允许使用路径穿越。"

    cover = _find(song, COVER_DIR)
    if cover:
        return _play_file(cover, _relative(cover, COVER_DIR))

    original = _find(song, LIBRARY_DIR)
    if not original:
        return f"错误：没有找到歌曲「{song}」，请先调用 list_songs 查看可唱的歌。"

    from singing.pipeline import cached_cover, check_environment

    final = cached_cover(original)
    if final:
        return _play_file(final, original.stem)
    with _pending_lock:
        status = _pending.get(original.stem)
    if status:
        return f"「{original.stem}」正在准备中（{status}），完成后会自动开唱。"
    problems = check_environment()
    if problems:
        return "错误：翻唱环境未就绪：" + "；".join(problems)
    _prepare_in_background(original)
    return f"「{original.stem}」是第一次唱，需要先练习约 3～5 分钟，准备好后会自动开唱。"
