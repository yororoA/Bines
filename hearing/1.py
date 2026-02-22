import os
import sys
import traceback
from pathlib import Path

# --- 防闪退补丁 ---
def exception_hook(type, value, tb):
    print("!!! Uncaught Exception !!!")
    traceback.print_exception(type, value, tb)
    input("Press Enter to exit...")
sys.excepthook = exception_hook
# -----------------

# 确保可以从项目根目录导入 config
ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import threading
import funasr
import pyaudio
import numpy as np
import logging
import time
import json
import zmq
from voiceprint import VoiceprintManager
from config import ZMQ_HOST, ZMQ_PORTS

# --- ZMQ 配置 ---
context = zmq.Context()

# PUB: 发送 ASR 结果
# topic: "asr"
pub_socket = context.socket(zmq.PUB)
# 【修复】设置LINGER为0，确保消息立即发送，不等待连接建立
pub_socket.setsockopt(zmq.LINGER, 0)
# 【修复】设置IMMEDIATE为1，确保消息立即发送，不等待连接建立
pub_socket.setsockopt(zmq.IMMEDIATE, 1)
pub_socket.bind(f"tcp://*:{ZMQ_PORTS['HEARING_ASR_PUB']}")

# SUB: 接收控制信号 (来自 Display / Thinking)
# topic: "control"
sub_socket = context.socket(zmq.SUB)
# Display 控制信号（播放音频时暂停 / 结束后恢复）
sub_socket.connect(f"tcp://{ZMQ_HOST}:{ZMQ_PORTS['CONTROL_PUB']}")
# Thinking 专用控制信号端口（避免与 Display / 手动输入冲突）
sub_socket.connect(f"tcp://{ZMQ_HOST}:{ZMQ_PORTS['CONTROL_PUB_THINKING']}")
sub_socket.setsockopt_string(zmq.SUBSCRIBE, "control")

def send_payload(text):
    try:
        payload = {
            "user_input": text,
            "sender": "ASR"
        }
        # 【修复】使用send_multipart的DONTWAIT标志，确保立即发送，不阻塞
        # 发送: topic="asr", payload=json
        pub_socket.send_multipart([b"asr", json.dumps(payload).encode('utf-8')], flags=zmq.DONTWAIT)
        print(f"已发送 ASR 数据: {text[:20]}...")
    except zmq.Again:
        # ZMQ缓冲区满，稍后重试（这种情况很少见）
        print(f"ZMQ缓冲区满，稍后重试...")
        # 使用阻塞模式重试一次
        try:
            pub_socket.send_multipart([b"asr", json.dumps(payload).encode('utf-8')])
            print(f"已发送 ASR 数据（重试）: {text[:20]}...")
        except Exception as e:
            print(f"\n发送数据失败: {e}")
    except Exception as e:
        print(f"\n发送数据失败: {e}")

# --- 监听控制信号 ---
IS_PAUSED = False
IS_PAUSED_LOCK = threading.Lock()  # 【修复】添加锁保护IS_PAUSED变量

def control_listener():
    global IS_PAUSED
    print("ZMQ Control Listener started...")
    while True:
        try:
            topic, msg = sub_socket.recv_multipart()
            data = json.loads(msg.decode('utf-8'))
            cough = data.get("cough")
            if cough == "start":
                with IS_PAUSED_LOCK:  # 【修复】加锁保护
                    if not IS_PAUSED:
                        print("\n[系统] 收到开始信号 (cough=start)，暂停录音...")
                        IS_PAUSED = True
            if cough == "end":
                with IS_PAUSED_LOCK:  # 【修复】加锁保护
                    if IS_PAUSED:
                        print("\n[系统] 收到结束信号 (cough=end)，恢复录音...")
                        IS_PAUSED = False
        except Exception as e:
            print(f"Control Listener Error: {e}")
            time.sleep(1)

# 启动监听线程
t_control = threading.Thread(target=control_listener, daemon=True)
t_control.start()

# 1. 设置日志级别
logging.getLogger('funasr').setLevel(logging.ERROR)
logging.getLogger('modelscope').setLevel(logging.ERROR)

# 热词微调
try:
    with open("./hotwords.txt", "r", encoding="utf-8") as file:
        lines = file.readlines()
        # 去除换行符和空行，并将列表转换为用空格分隔的字符串 (FunASR 格式要求)
        hotwords = " ".join([line.strip() for line in lines if line.strip()])
        print(f"已加载热词: {hotwords}")
except FileNotFoundError:
    print("未找到 hotwords.txt，跳过热词加载")
    hotwords = ""

# --- 模型选择 ---
# 方案A: 实时流式模型 (速度快，但精度稍低)
# MODEL_NAME = 'paraformer-zh-streaming'
# 方案B: SenseVoiceSmall (高精度，但不支持外挂语言模型)
# MODEL_NAME = 'iic/SenseVoiceSmall'
# 方案C: Paraformer-Large-Contextual + LM (高精度 + 热词增强 + 语言模型纠错)
# 注意：标准版 Paraformer 不支持热词，必须使用 Contextual 版本
MODEL_NAME = 'iic/speech_paraformer-large-contextual_asr_nat-zh-cn-16k-common-vocab8404'

print(f"正在加载 ASR 模型: {MODEL_NAME} ...")

if MODEL_NAME == 'paraformer-zh-streaming':
    # 流式模型初始化
    model = funasr.AutoModel(
        model=MODEL_NAME,
        device="cuda",
        disable_update=True,
        verbose=False
    )
elif MODEL_NAME == 'iic/SenseVoiceSmall':
    # SenseVoiceSmall 初始化
    model = funasr.AutoModel(
        model=MODEL_NAME,
        device="cuda",
        disable_update=True,
        verbose=False,
        vad_model="fsmn-vad",
        vad_kwargs={"max_single_segment_time": 30000},
    )
else:
    # Paraformer-Large-Contextual + LM 初始化
    print("正在加载语言模型 (LM) 用于纠错...")
    model = funasr.AutoModel(
        model=MODEL_NAME,
        model_revision="v2.0.4",
        # 外挂语言模型
        lm_model="iic/speech_transformer_lm_zh-cn-common-vocab8404-pytorch",
        lm_model_revision="v2.0.4",
        lm_weight=0.15,  # 语言模型权重
        beam_size=10,  # 束搜索宽度，越大越准但越慢
        
        device="cuda",
        disable_update=True,
        verbose=False,
    )

# 标点模型配置
if MODEL_NAME == 'iic/SenseVoiceSmall':
    punc_model = None
else:
    print("正在加载 PUNC 模型...")
    punc_model = funasr.AutoModel(
        model="ct-punc",
        model_revision="v2.0.4",
        disable_update=True,
        verbose=False
    )

# 2. 音频配置
SAMPLE_RATE = 16000  # ASR模型最佳采样率
CHANNELS = 1  # 单声道
CHUNK_SAMPLES = 1600  # 100ms (原 9600/600ms，改小以提高响应速度)
FORMAT = pyaudio.paInt16  # 16位深度

# 优化录音参数
RECORD_PARAMS = {
    'format': FORMAT,
    'channels': CHANNELS,
    'rate': SAMPLE_RATE,
    'input': True,
    'frames_per_buffer': CHUNK_SAMPLES,
    'input_device_index': None,  # 使用默认设备
    'start': False
}

p = pyaudio.PyAudio()

try:
    default_device = p.get_default_input_device_info()
    print(f"使用默认音频输入设备: {default_device['name']}")
    # 尝试获取设备支持的最佳参数
    try:
        # 检查设备是否支持当前配置
        p.is_format_supported(
            rate=SAMPLE_RATE,
            input_format=FORMAT,
            input_channels=CHANNELS,
            input_device=default_device['index']
        )
    except ValueError:
        print(f"当前设备不支持配置，使用设备默认配置")
except IOError:
    print("未找到默认音频输入设备")

stream = p.open(**RECORD_PARAMS)
stream.start_stream()  # 启动音频流

# 初始化声纹识别模块
print("\n正在初始化声纹识别模块...")
voiceprint_manager = VoiceprintManager(voiceprint_dir="voiceprints")
# 声纹相似度阈值，可调整（0-1，越高越严格）
# MFCC特征的相似度通常较低（0.4-0.8），所以阈值设置较低
# resemblyzer: 0.75, librosa MFCC: 0.65
# 声纹相似度阈值，可调整（0-1，越高越严格）
# resemblyzer: 0.70（降低阈值，0.744 应该能通过）
# librosa MFCC: 0.65
VOICEPRINT_THRESHOLD = 0.65 if not voiceprint_manager.use_resemblyzer else 0.70

print("\n" + "=" * 60)
print("模型加载完毕，正在监听麦克风...")
if voiceprint_manager.has_registered_voiceprints():
    print(f"声纹识别已启用 (已注册 {len(voiceprint_manager.registered_voiceprints)} 个声纹)")
    print(f"   相似度阈值: {VOICEPRINT_THRESHOLD}")
else:
    print("未注册声纹，声纹识别功能已禁用（所有语音都会识别）")
print("请说话... (按 Ctrl+C 停止)")
print("=" * 60 + "\n")

try:
    from common.module_ready import notify_module_ready
    notify_module_ready("Hearing")
except Exception as e:
    print(f"[Hearing] 就绪上报失败: {e}", flush=True)

# 缓冲区，用于存储整句音频 (仅用于非流式模型)
audio_buffer = []
# 预读取缓冲区 (Pre-roll)，保留最近 15 个 chunk (约 1.5s)，更好地捕捉句子开头
from collections import deque

pre_roll_buffer = deque(maxlen=15)

# 优化能量检测参数
# 动态能量阈值，根据环境噪音自动调整
ENERGY_THRESHOLD = 1000  # 初始阈值
ENERGY_THRESHOLD_ADJUSTMENT = True  # 启用动态阈值调整
NOISE_FLOOR = 0  # 噪音地板，用于动态调整
NOISE_UPDATE_WEIGHT = 0.05  # 噪音更新权重（降低，更稳定）
MIN_ENERGY_MULTIPLIER = 2.5  # 最小能量倍数（降低，更容易触发）
ADJUSTED_THRESHOLD = ENERGY_THRESHOLD  # 初始化调整后的阈值（修复作用域问题）
# 【修复】注意：ADJUSTED_THRESHOLD和NOISE_FLOOR当前只在主循环线程中使用，是线程安全的
# 如果未来扩展为多线程，需要添加锁保护

# 优化沉默检测参数
silence_frames = 0
SILENCE_FRAME_THRESHOLD = 8  # 8帧 = 0.8s，更合理的沉默判定
SILENCE_TIMEOUT = 1.5  # 1.5秒无新识别内容则认为一句话结束，提高准确性

# 音频增益参数
AUDIO_GAIN = 1.5  # 适当提高增益，增强弱声音
MAX_AUDIO_VALUE = 32767  # 16位音频最大值

cache = {}
current_text = ""
last_update_time = time.time()

try:
    while True:
        data = stream.read(CHUNK_SAMPLES, exception_on_overflow=False)

        # 暂停检测
        with IS_PAUSED_LOCK:  # 【修复】加锁保护
            is_paused = IS_PAUSED
        if is_paused:
            # 清空之前的缓存，防止恢复时立刻识别旧内容
            audio_buffer = []
            pre_roll_buffer.clear()
            silence_frames = 0
            cache = {}
            current_text = ""
            continue

        audio_chunk = np.frombuffer(data, dtype=np.int16)
        
        # 音频预处理：优化音频质量
        # 1. 应用增益，增强弱声音
        audio_chunk = audio_chunk.astype(np.float32)
        audio_chunk = np.clip(audio_chunk * AUDIO_GAIN, -MAX_AUDIO_VALUE, MAX_AUDIO_VALUE)
        audio_chunk = audio_chunk.astype(np.int16)

        # 2. 计算当前音频块的能量
        energy = np.abs(audio_chunk).mean()
        
        # 3. 动态调整能量阈值，适应环境噪音
        if ENERGY_THRESHOLD_ADJUSTMENT:
            # 更新噪音地板（只在沉默时更新，避免被语音影响）
            if energy < ADJUSTED_THRESHOLD:
                NOISE_FLOOR = NOISE_FLOOR * (1 - NOISE_UPDATE_WEIGHT) + energy * NOISE_UPDATE_WEIGHT
            # 调整能量阈值为噪音地板的倍数
            ADJUSTED_THRESHOLD = max(NOISE_FLOOR * MIN_ENERGY_MULTIPLIER, ENERGY_THRESHOLD * 0.8)  # 降低最小阈值
        else:
            ADJUSTED_THRESHOLD = ENERGY_THRESHOLD

        # --- 分支处理 ---
        if MODEL_NAME == 'paraformer-zh-streaming':
            # === 方案A: 原有的流式处理逻辑 ===
            # 优化：将预处理后的音频块转换为float32并归一化，适合模型输入
            processed_chunk = audio_chunk.astype(np.float32) / 32768.0
            
            res = model.generate(
                input=processed_chunk,
                cache=cache,
                is_final=False,
                disable_pbar=True,
                disable_log=True,
                hotword=hotwords
            )

            if res:
                text = res[0]['text']

                # 如果识别到了新内容
                if text.strip():
                    current_text += text
                    last_update_time = time.time()
                    # 实时显示当前识别结果（未加标点）
                    print(f"\r识别中: {current_text}", end="", flush=True)

                # 检查是否超时（断句）
                if current_text.strip() and (time.time() - last_update_time > SILENCE_TIMEOUT):
                    # 调用标点模型
                    try:
                        punc_res = punc_model.generate(
                            input=current_text,
                            disable_pbar=True,
                            disable_log=True
                        )
                        final_text = punc_res[0]['text']
                    except Exception as e:
                        final_text = current_text.replace("操你妈", "我爱你")

                    print(f"\r{final_text}" + " " * 20)
                    
                    # 发送数据
                    send_payload(final_text)

                    # 识别结束，暂停录音
                    print("\n[系统] 语音命令已发送，暂停录音等待系统响应...")
                    with IS_PAUSED_LOCK:  # 【修复】加锁保护
                        IS_PAUSED = True

                    # 重置状态
                    cache = {}
                    current_text = ""
                    last_update_time = time.time()

        else:
            # === 方案B/C: 伪流式处理 (录音 -> 检测沉默 -> 识别) ===
            if energy > ADJUSTED_THRESHOLD:
                # 如果是刚开始说话（缓冲区为空），先把预读取的内容加进去
                if len(audio_buffer) == 0:
                    audio_buffer.extend(pre_roll_buffer)

                # 有声音，加入缓冲区
                audio_buffer.append(audio_chunk)
                silence_frames = 0
                # 只在调试模式下显示能量信息
                print(f"\r正在聆听... (能量: {energy:.0f}, 阈值: {ADJUSTED_THRESHOLD:.0f})", end="", flush=True)
            else:
                # 沉默
                if len(audio_buffer) > 0:
                    # 正在录音中的沉默，也需要加入缓冲区，保持语速连贯
                    audio_buffer.append(audio_chunk)

                    silence_frames += 1
                    # 只在调试模式下显示沉默计数
                    # print(f"\r...沉默中 ({silence_frames}/{SILENCE_FRAME_THRESHOLD})", end="", flush=True)

                    # 如果沉默超过阈值，且缓冲区有足够数据，开始识别
                    if silence_frames >= SILENCE_FRAME_THRESHOLD and len(audio_buffer) > 10:  # 确保有足够的音频数据
                        print(f"\r正在识别...                  ", end="", flush=True)
                        
                        # 【修复】录音完成后暂停录音，等待声纹识别结果
                        print("\n[系统] 录音完成，暂停录音等待声纹识别...")
                        with IS_PAUSED_LOCK:  # 【修复】加锁保护
                            IS_PAUSED = True

                        # 拼接音频
                        full_audio = np.concatenate(audio_buffer)

                        # 转换为 float32 并归一化到 [-1, 1]
                        full_audio = full_audio.astype(np.float32) / 32768.0

                        # 优化：使用更平滑的增益处理
                        full_audio = np.clip(full_audio * AUDIO_GAIN, -1.0, 1.0)

                        # === 声纹识别：先进行声纹比对 ===
                        if voiceprint_manager.has_registered_voiceprints():
                            is_match, similarity, matched_user = voiceprint_manager.verify_voiceprint(
                                full_audio, 
                                sample_rate=SAMPLE_RATE,
                                threshold=VOICEPRINT_THRESHOLD
                            )
                            
                            if not is_match:
                                # 声纹不匹配，不进行识别，恢复录音
                                print(f"\r声纹不匹配，拒绝识别 (相似度: {similarity:.3f})")
                                print("   恢复录音，继续监听...")
                                
                                # 重置缓冲区，恢复录音状态
                                audio_buffer = []
                                silence_frames = 0
                                pre_roll_buffer.clear()
                                # 【修复】声纹不通过，恢复录音
                                with IS_PAUSED_LOCK:  # 【修复】加锁保护
                                    IS_PAUSED = False
                                continue  # 跳过识别，继续监听

                        # === 声纹匹配或未启用声纹识别，继续ASR识别 ===
                        try:
                            if MODEL_NAME == 'iic/SenseVoiceSmall':
                                # SenseVoice 识别
                                res = model.generate(
                                    input=full_audio,
                                    cache={},
                                    language="zh",
                                    use_itn=True,
                                    disable_pbar=True,
                                    disable_log=True,
                                    hotword=hotwords
                                )
                            else:
                                # Paraformer + LM 识别
                                # 注意：Paraformer 不需要 language 参数
                                res = model.generate(
                                    input=full_audio,
                                    disable_pbar=True,
                                    disable_log=True,
                                    hotword=hotwords
                                )

                            if res:
                                text = res[0]['text']
                                # 清理 SenseVoice 的标签
                                import re

                                clean_text = re.sub(r'<\|.*?\|>', '', text).strip()

                                # 如果是 Paraformer，需要额外调用 PUNC 模型加标点
                                if punc_model is not None and clean_text:
                                    try:
                                        punc_res = punc_model.generate(
                                            input=clean_text,
                                            disable_pbar=True,
                                            disable_log=True
                                        )
                                        clean_text = punc_res[0]['text'].replace("操你妈", "我爱你")
                                    except:
                                        pass

                                if clean_text:
                                    print(f"\r{clean_text}" + " " * 20)
                                    send_payload(clean_text)

                                    # 【修复】识别完成，保持暂停状态（已在录音完成后暂停）
                                    print("\n[系统] 语音命令已发送，保持暂停状态等待系统响应...")
                                    # IS_PAUSED 已在录音完成后设置为 True，这里不需要重复设置
                        except Exception as e:
                            print(f"\r识别出错: {e}")
                            # 保留音频缓冲区，以便重新尝试识别
                            print("\n[系统] 识别出错，将保留当前音频缓冲区...")

                        # 重置缓冲区
                        audio_buffer = []
                        silence_frames = 0
                        pre_roll_buffer.clear()
                else:
                    # 缓冲区为空，且处于沉默，将当前块加入预读取缓冲区
                    pre_roll_buffer.append(audio_chunk)

except KeyboardInterrupt:
    print("\n\n停止录音...")

finally:
    stream.stop_stream()
    stream.close()
    p.terminate()