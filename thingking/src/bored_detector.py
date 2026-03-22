"""
无聊检测进程：基于无聊值(Entropy)进度条决定是否主动发起对话。
- 无聊值 0-100 随时间线性增长，用户说话瞬间清零；达 100 时可选 LLM 确认后触发。
- 关系越好无聊值涨得越快（粘人），关系越差涨得越慢（高冷）；深夜 2:00-8:00 几乎不涨。
- RL-Lite：根据用户「响应延迟」调整各时段权重，秒回→该时段增长率+10%，1小时未回→-20%。
- 视觉刺激：低概率随机拉一次视觉；若出现新物体/变化则瞬间填满无聊值并以「新发现」为由发起对话。
- 文件读取使用 mtime 缓存，仅文件变更时读盘，降低 IO。
"""
import os
import sys
import json
import time
import zmq
import requests
import datetime
import re
import random
from pathlib import Path
from path_setup import ensure_project_root

# 确保可以从项目根目录导入 config
PROJECT_ROOT = ensure_project_root(__file__, 2)

from config import (
    ZMQ_HOST,
    ZMQ_PORTS,
    PRESENCE_STATE_PATH,
    DEEPSEEK_API_URL,
    DEEPSEEK_BORED_API_KEY,
    DEEPSEEK_API_KEY,
    DEEPSEEK_MODEL,
    DEEPSEEK_API_TIMEOUT,
    TS_AI_SDK_GATEWAY_URL,
)

# 导入短期记忆系统、动态状态和永久记忆
from layered_memory import ShortTermMemory, DynamicMemory, PermanentMemory
# 导入关系状态计算函数
from relationship_state import (
    compute_relationship_weights,
    collapse_relationship_level,
    DEFAULT_SOFTNESS
)
from common.zmq_rpc import zmq_req_json

# --- [修复补丁] 强制设置 Windows 控制台编码，防止 print 中文卡死 ---
if sys.platform.startswith('win'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except (AttributeError, OSError):
        pass
# -------------------------------------------------------------------

# 初始化短期记忆系统、动态状态和永久记忆
memory_system = ShortTermMemory()
dynamic_memory = DynamicMemory()
permanent_memory = PermanentMemory()


class CachedMemory:
    """基于文件 mtime 的缓存：仅当文件被修改时才重新 load，大幅降低重复 IO。"""
    def __init__(self, memory_obj):
        self.memory = memory_obj
        self.last_mtime = 0

    def reload_if_needed(self):
        try:
            current_mtime = os.path.getmtime(self.memory.filepath)
            if current_mtime > self.last_mtime:
                self.memory.load()
                self.last_mtime = current_mtime
                return True
        except (FileNotFoundError, OSError):
            pass
        return False


cached_short_mem = CachedMemory(memory_system)
cached_dynamic_mem = CachedMemory(dynamic_memory)
cached_permanent_mem = CachedMemory(permanent_memory)

# 无聊值(Entropy)系统：0-100 进度条，达 100 时可触发
# 基准：1.0 倍速率下约 20 分钟从 0 涨到 100
BASE_BOREDOM_RATE_PER_SEC = 100.0 / (20 * 60)
boredom_value = 0.0
last_user_message_time = 0.0   # 用于检测「用户刚说话」从而清零
last_check_time = 0.0          # 用于计算 delta_t 增长

# 触发后冷却：基准与边界，实际 CD 由关系分 + 随机扰动计算
BASE_COOLDOWN_SECONDS = 60 * 20
MIN_COOLDOWN_SECONDS = 60 * 5
MAX_COOLDOWN_SECONDS = 60 * 120
next_allowed_trigger_time = 0

# RL-Lite：按小时的学习权重，持久化到同目录 JSON，重启后保留
_BORED_RL_WEIGHTS_PATH = Path(__file__).resolve().parent / "bored_rl_weights.json"
_DEFAULT_HOURLY_WEIGHTS = {str(h): 1.0 for h in range(24)}


def _load_hourly_weights():
    """从磁盘加载 RL-Lite 权重，失败或不存在则返回默认全 1.0。"""
    try:
        if _BORED_RL_WEIGHTS_PATH.exists():
            with open(_BORED_RL_WEIGHTS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                out = dict(_DEFAULT_HOURLY_WEIGHTS)
                for k, v in data.items():
                    if k in out and isinstance(v, (int, float)):
                        out[k] = max(0.1, min(3.0, float(v)))
                return out
    except (json.JSONDecodeError, OSError):
        pass
    return dict(_DEFAULT_HOURLY_WEIGHTS)


def _save_hourly_weights():
    """将当前 hourly_weights 写入磁盘。"""
    try:
        with open(_BORED_RL_WEIGHTS_PATH, "w", encoding="utf-8") as f:
            json.dump(hourly_weights, f, ensure_ascii=False, indent=0)
    except OSError as e:
        print(f"[Bored Detector] RL-Lite 权重保存失败: {e}", flush=True)


hourly_weights = _load_hourly_weights()
last_bored_trigger_time = 0.0  # 上次主动发起对话的时间，用于计算用户响应延迟

# 视觉刺激：低概率调用视觉模块，检测到新物体/变化则直接触发
VISUAL_CHECK_PROBABILITY = 0.02       # 每次 check 时 2% 概率拉一次视觉
VISUAL_CHECK_MIN_INTERVAL = 180       # 两次视觉检查至少间隔 3 分钟
VISUAL_BOREDOM_MIN = 20.0            # 无聊值至少到此才参与视觉随机（避免一上来就拍）
last_visual_check_time = 0.0
last_visual_objects = set()           # 上次看到的物体集合，用于检测「新物体」
last_visual_caption = ""
last_visual_stimulus_reason = None    # 若因视觉刺激触发，填入原因供主进程生成 prompt

# DeepSeek 配置（优先 Bored 专用 Key，未设置时回退到主 Key，便于启用无聊检测）
API_KEY = (DEEPSEEK_BORED_API_KEY or DEEPSEEK_API_KEY) or ""
if not API_KEY:
    raise RuntimeError(
        "Bored Detector 需要 API Key：请设置 DEEPSEEK_BORED_API_KEY 或 DEEPSEEK_API_KEY"
    )
API_URL = DEEPSEEK_API_URL
MODEL = DEEPSEEK_MODEL
TIMEOUT = DEEPSEEK_API_TIMEOUT
TS_GATEWAY_URL = TS_AI_SDK_GATEWAY_URL

# ZMQ 配置
HOST = ZMQ_HOST
BORED_PUB_PORT = ZMQ_PORTS["BORED_PUB"]
VISUAL_REQREP_PORT = ZMQ_PORTS.get("VISUAL_REQREP", 5557)
BORED_TOPIC = "bored"

# 初始化 ZMQ
context = zmq.Context()
pub_socket = context.socket(zmq.PUB)
try:
    pub_socket.bind(f"tcp://*:{BORED_PUB_PORT}")
    print(f"[Bored Detector] ZMQ PUB socket bound to port {BORED_PUB_PORT}", flush=True)
except Exception as e:
    print(f"[Bored Detector] Failed to bind ZMQ socket: {e}", flush=True)
    sys.exit(1)


def _fetch_visual_snapshot():
    """
    向视觉模块发起一次 REQ，获取当前场景（物体、人脸、手势、场景描述）。
    返回 dict 或 None（超时/不可达时返回 None）。
    """
    try:
        response = zmq_req_json(
            context,
            f"tcp://{HOST}:{VISUAL_REQREP_PORT}",
            {
                "command": "look",
                "focus": "简要列出场景中的物体、人物手中的物品、穿着或环境变化，用于检测是否有新出现的内容。",
            },
            recv_timeout_ms=15000,
            send_timeout_ms=5000,
            linger_ms=1000,
        )
        if isinstance(response, dict) and "scene_image" in response:
            response = dict(response)
            del response["scene_image"]
        return response if isinstance(response, dict) else None
    except Exception as e:
        print(f"[Bored Detector] 视觉拉取失败: {e}", flush=True)
        return None


def _check_visual_stimulus(now):
    """
    低概率拉一次视觉；若与上次快照相比出现新物体或场景描述变化，视为「视觉刺激」，
    填满无聊值并设置 last_visual_stimulus_reason，返回 True（调用方将直接触发）。
    """
    global boredom_value, last_visual_check_time, last_visual_objects, last_visual_caption, last_visual_stimulus_reason
    if random.random() >= VISUAL_CHECK_PROBABILITY:
        return False
    if now - last_visual_check_time < VISUAL_CHECK_MIN_INTERVAL:
        return False
    if boredom_value < VISUAL_BOREDOM_MIN:
        return False
    last_visual_check_time = now
    snap = _fetch_visual_snapshot()
    if not snap:
        return False
    objects = set(snap.get("objects") or [])
    caption = (snap.get("scene_caption") or "").strip()[:200]
    faces = snap.get("faces") or []
    gestures = snap.get("gestures") or []
    # 首次有效快照只建立基线不触发；之后与上次有差异（新物体/描述变化）才视为「新发现」
    is_first_baseline = not last_visual_objects and not last_visual_caption
    has_new_objects = bool(objects and objects != last_visual_objects)
    has_caption_change = bool(caption and caption != last_visual_caption)
    last_visual_objects = objects
    last_visual_caption = caption
    if is_first_baseline or not (has_new_objects or has_caption_change):
        return False
    # 构造「新发现」描述，供主进程生成「我本来在发呆，突然看到…」类 prompt
    parts = []
    if objects:
        parts.append("场景中有: " + ", ".join(list(objects)[:8]))
    if faces:
        parts.append("面前的人: " + ", ".join(faces[:3]))
    if gestures:
        parts.append("手势: " + ", ".join(gestures[:3]))
    if caption:
        parts.append(caption)
    last_visual_stimulus_reason = "；".join(parts) if parts else "场景有变化"
    boredom_value = 100.0
    print(f"[Bored Detector] 视觉刺激：{last_visual_stimulus_reason[:80]}...", flush=True)
    return True


def get_growth_multiplier(relationship_score):
    """
    性格动态速率：关系越好无聊值涨得越快（粘人），关系越差涨得越慢（高冷）。
    假设 score 约在 [-100, 100]，返回 [0.4, 2.0] 的倍数。
    """
    return max(0.4, min(2.0, 0.5 + (relationship_score + 100) / 200.0))


def update_learning(reply_delay_seconds):
    """
    RL-Lite：根据用户响应延迟更新当前时段的增长率权重。
    秒回(<1分钟)→正反馈，该时段权重 ×1.1；超过1小时才回/没回→负反馈，该时段权重 ×0.8。
    """
    global hourly_weights
    h = str(datetime.datetime.now().hour)
    if reply_delay_seconds < 60:
        hourly_weights[h] *= 1.1
        print(f"[Bored Detector] RL-Lite 正反馈：{int(reply_delay_seconds)}s 内回复，{h} 时权重 ↑", flush=True)
    elif reply_delay_seconds > 3600:
        hourly_weights[h] *= 0.8
        print(f"[Bored Detector] RL-Lite 负反馈：>{reply_delay_seconds//3600}h 未回，{h} 时权重 ↓", flush=True)
    hourly_weights[h] = max(0.1, min(3.0, hourly_weights[h]))
    _save_hourly_weights()


def calculate_next_cooldown(relationship_score):
    """
    根据关系分和随机波动，计算下一次的冷却时间（秒）。
    关系越好 CD 越短（粘人），关系越差 CD 越长（冷淡），并加入 ±30% 随机扰动。
    """
    # 关系分影响：假设 score 约在 -100～100，0 时系数 1.0
    factor = 1.0 - (relationship_score / 200.0)
    factor = max(0.4, min(2.0, factor))
    current_base = BASE_COOLDOWN_SECONDS * factor
    variance = random.uniform(0.7, 1.3)
    final_seconds = max(MIN_COOLDOWN_SECONDS, min(MAX_COOLDOWN_SECONDS, current_base * variance))
    minutes = int(final_seconds / 60)
    print(f"[Bored Detector] 下次冷却: {minutes} 分钟 (关系分: {relationship_score}, 因子: {factor:.2f})", flush=True)
    return final_seconds


def is_sleeping_time():
    """是否处于深夜睡觉时段 (2:00 - 8:00)。"""
    return 2 <= datetime.datetime.now().hour < 8


def check_context_blocking(messages):
    """
    检测最近对话是否有明显「结束对话」意图（晚安、去忙了、闭嘴等）。
    返回 (是否阻断, 建议静默秒数)。
    """
    if not messages:
        return False, 0
    last_msg = messages[-1]
    content = (last_msg.get("content") or "").lower()
    role = last_msg.get("role", "")
    blocking_keywords = ["晚安", "去睡", "睡觉了", "去忙", "闭嘴", "别吵", "再见", "拜拜", "goodnight", "bye"]
    if role == "user" and any(k in content for k in blocking_keywords):
        return True, 60 * 60 * 4  # 阻断 4 小时
    return False, 0


def send_bored_message():
    """向主进程发送 bored 消息，清零无聊值，记录触发时间供 RL-Lite 计算响应延迟，并依关系分计算下次冷却。若因视觉刺激触发则带上 visual_stimulus。"""
    global next_allowed_trigger_time, boredom_value, last_bored_trigger_time, last_visual_stimulus_reason
    try:
        last_bored_trigger_time = time.time()
        payload = {"type": "bored", "timestamp": last_bored_trigger_time}
        if last_visual_stimulus_reason:
            payload["visual_stimulus"] = last_visual_stimulus_reason
            last_visual_stimulus_reason = None
        pub_socket.send_multipart([BORED_TOPIC.encode("utf-8"), json.dumps(payload).encode("utf-8")])
        cached_dynamic_mem.reload_if_needed()
        score = dynamic_memory.state.get("relationship_score", 0)
        cooldown = calculate_next_cooldown(score)
        next_allowed_trigger_time = time.time() + cooldown
        boredom_value = 0.0
        print(f"[Bored Detector] Sent bored message. 无聊值已清零，下次允许: {time.strftime('%H:%M:%S', time.localtime(next_allowed_trigger_time))}", flush=True)
        return True
    except Exception as e:
        print(f"[Bored Detector] Failed to send bored message: {e}", flush=True)
        return False


def _parse_last_message_timestamp(last_message, fallback_time):
    """从最后一条消息解析时间戳。"""
    ts = last_message.get("timestamp")
    if ts is not None:
        try:
            return float(ts)
        except (TypeError, ValueError):
            pass
    content = last_message.get("content", "")
    match = re.search(r"\[(\d{4}/\d{1,2}/\d{1,2})\s+(\d{1,2}:\d{1,2})\]:", content)
    if match:
        try:
            dt = datetime.datetime.strptime(f"{match.group(1)} {match.group(2)}", "%Y/%m/%d %H:%M")
            return dt.timestamp()
        except ValueError:
            pass
    return fallback_time


def _is_user_online():
    """读取用户上下线状态，默认在线。"""
    try:
        if PRESENCE_STATE_PATH.exists():
            with open(PRESENCE_STATE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            return bool(data.get("user_online", True))
    except (json.JSONDecodeError, OSError):
        pass
    return True


def check_bored():
    """
    无聊值(0-100)随时间线性增长，用户说话瞬间清零；达 100 时经 LLM 确认后触发。
    关系越好涨得越快（粘人），关系越差涨得越慢（高冷）；深夜 2:00-8:00 几乎不涨。
    用户下线状态下不触发、不拉视觉。
    """
    global next_allowed_trigger_time, boredom_value, last_user_message_time, last_check_time
    try:
        if not _is_user_online():
            return False
        now = time.time()
        if now < next_allowed_trigger_time:
            return False

        # 仅当文件 mtime 变化时读盘，降低 IO
        cached_short_mem.reload_if_needed()
        cached_dynamic_mem.reload_if_needed()
        cached_permanent_mem.reload_if_needed()

        messages = memory_system.get_messages()
        if not messages:
            last_check_time = now
            return False

        last_message = messages[-1]
        last_ts = _parse_last_message_timestamp(last_message, now)
        role = last_message.get("role", "")
        relationship_score = dynamic_memory.state.get("relationship_score", 0)
        is_sleep = is_sleeping_time()

        # RL-Lite：处理上次主动发起后的用户响应延迟，更新该时段权重
        global last_bored_trigger_time, hourly_weights
        if last_bored_trigger_time > 0:
            if role == "user" and last_ts > last_bored_trigger_time:
                reply_delay = last_ts - last_bored_trigger_time
                update_learning(reply_delay)
                last_bored_trigger_time = 0
            elif now - last_bored_trigger_time > 3600:
                update_learning(3601)
                last_bored_trigger_time = 0

        # 性格动态速率 × 生物钟 × RL-Lite 时段权重
        growth_mult = get_growth_multiplier(relationship_score) * (0.1 if is_sleep else 1.0)
        growth_mult *= hourly_weights.get(str(datetime.datetime.now().hour), 1.0)
        delta_t = max(0, now - last_check_time)
        if role == "user" and last_ts > last_user_message_time:
            boredom_value = 0.0
            last_user_message_time = last_ts
        else:
            boredom_value = min(100.0, boredom_value + delta_t * BASE_BOREDOM_RATE_PER_SEC * growth_mult)
        last_check_time = now

        time_since_last = now - last_ts

        # 上下文阻断：晚安/去忙了等 → 静默一段时间并清零无聊值
        is_blocked, block_time = check_context_blocking(messages)
        if is_blocked and time_since_last < block_time:
            print("[Bored Detector] 检测到话题结束语，保持静默。", flush=True)
            next_allowed_trigger_time = now + (block_time - time_since_last)
            boredom_value = 0.0
            return False

        # 视觉刺激：低概率拉一次视觉，新物体/变化则瞬间填满无聊值并以「新发现」为由触发
        if _check_visual_stimulus(now):
            return True

        # 无聊值未满 100：不调 LLM，直接返回
        if boredom_value < 100:
            return False

        # 无聊值已满 100：可选 LLM 确认后再触发
        recent_messages = messages[-5:] if len(messages) >= 5 else messages
        recent_context = "\n".join([
            f"{msg.get('role', 'unknown')}: {msg.get('content', '')[:200]}"
            for msg in recent_messages
        ])
        minutes_since = int(time_since_last / 60)
        hours_since = int(time_since_last / 3600)
        days_since = int(time_since_last / 86400)
        time_description = f"{days_since}天" if days_since > 0 else (f"{hours_since}小时" if hours_since > 0 else f"{minutes_since}分钟")
        dynamic_state_str = dynamic_memory.to_prompt_str()
        with permanent_memory.lock:
            facts = permanent_memory.data.get("facts", [])
            personality_info = "\n".join([f"- {fact}" for fact in facts]) if facts else "无性格信息"
        relationship_weights = compute_relationship_weights(relationship_score, softness=DEFAULT_SOFTNESS)
        collapsed_level = collapse_relationship_level(relationship_weights)
        mood_map = {
            "敌对": "当前心情：敌对。她可能因'烦躁'而主动说话，而非'寂寞'。",
            "冷淡": "当前心情：冷淡。可能完全沉默，不会主动发起闲聊。",
            "普通": "当前心情：普通。不会主动发起闲聊，主动仅限公事。",
            "亲近": "当前心情：亲近。可能话多，愿意主动发起对话。",
            "非常亲密": "当前心情：非常亲密。很愿意主动发起对话。",
        }
        mood_description = mood_map.get(collapsed_level, f"当前心情：{collapsed_level}。")

        system_prompt = """你是一个情感判断模块。请判断角色在当前是否感到无聊并想要**主动**发起对话。
规则：不要太频繁；深夜尽量安静；告别语后不发起；关系低时冷淡、高时粘人；结合「当下的心情」。
只回答 True 或 False。True=主动发起，False=保持沉默。"""

        user_prompt = f"""当前时间：{datetime.datetime.now().strftime('%H:%M')}
距离上次对话：{time_description}（{int(time_since_last)}秒）
关系分数：{relationship_score}
无聊值已满(100)，请确认是否主动发起。

最近对话：{recent_context}
当前状态：{dynamic_state_str}
【性格】：{personality_info}
【心情】：{mood_description} 等级：{collapsed_level}
是否应该主动发起对话？(True/False)"""

        payload = {
            "model": MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            "temperature": 0.1,
            "max_tokens": 10,
            "stream": False
        }

        answer = ""
        if TS_GATEWAY_URL:
            try:
                gw_payload = {
                    "model": MODEL,
                    "messages": payload["messages"],
                    "temperature": payload["temperature"],
                    "maxTokens": payload["max_tokens"],
                }
                gw_resp = requests.post(
                    f"{TS_GATEWAY_URL}/api/chat/bored",
                    json=gw_payload,
                    timeout=TIMEOUT,
                )
                gw_resp.raise_for_status()
                gw_json = gw_resp.json()
                answer = str(gw_json.get("content", "")).strip().lower()
                print("[Bored Detector] [TSGW] 使用 TS 网关 /api/chat/bored", flush=True)
            except Exception as gw_err:
                print(f"[Bored Detector] TS AI Gateway 调用失败，回退直连模型: {gw_err}", flush=True)

        if not answer:
            print("[Bored Detector] [PY-FALLBACK] 使用 Python 直连模型", flush=True)
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {API_KEY}"
            }
            response = requests.post(API_URL, headers=headers, json=payload, timeout=TIMEOUT)
            response.raise_for_status()
            result = response.json()
            answer = result["choices"][0]["message"]["content"].strip().lower()

        is_bored = "true" in answer

        if is_bored:
            print(f"[Bored Detector] 无聊值 100 + LLM 确认触发 (上次对话 {minutes_since} 分钟前)", flush=True)
            return True
        else:
            boredom_value = 70.0
            next_allowed_trigger_time = now + 300
            print("[Bored Detector] LLM 暂不打扰，无聊值回落到 70", flush=True)
            return False

    except Exception as e:
        print(f"[Bored Detector] Error: {e}", flush=True)
        import traceback
        traceback.print_exc()
        next_allowed_trigger_time = time.time() + 60
        return False


def main():
    """主循环：每 10 秒更新无聊值，达 100 且过冷却后经 LLM 确认触发。"""
    global next_allowed_trigger_time, last_check_time
    print("[Bored Detector] Process Started (无聊值 0-100 + mtime 缓存 + 生物钟).", flush=True)
    print(f"[Bored Detector] API Key: {API_KEY[:10]}...", flush=True)
    if TS_GATEWAY_URL:
        print(f"[Bored Detector] TS 网关已启用: {TS_GATEWAY_URL}", flush=True)
    else:
        print("[Bored Detector] TS 网关未配置，默认走 Python 直连", flush=True)
    next_allowed_trigger_time = time.time() + 60
    last_check_time = time.time()

    while True:
        try:
            time.sleep(10)
            if check_bored():
                send_bored_message()
        except KeyboardInterrupt:
            print("[Bored Detector] Interrupted by user", flush=True)
            break
        except Exception as e:
            print(f"[Bored Detector] Loop Error: {e}", flush=True)
            import traceback
            traceback.print_exc()
            time.sleep(30)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[Bored Detector] CRITICAL ERROR: {e}", flush=True)
        import traceback
        traceback.print_exc()
        input("Press Enter to exit...")
