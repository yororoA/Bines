import json
import base64
import zmq
import threading
import time
import queue
from tts import tts_generate, tts_generate_streaming
from config import ZMQ_HOST, ZMQ_PORTS

# --- ZMQ 配置 ---
context = zmq.Context()

# PUB: 发送音频数据 (避免与Thinking的文本端口冲突)
# topic: "tts"
pub_socket = context.socket(zmq.PUB)
pub_socket.bind(f"tcp://*:{ZMQ_PORTS['TTS_AUDIO_PUB']}")

# SUB: 接收 Thinking 结果
# topic: "think"
sub_socket = context.socket(zmq.SUB)
sub_socket.connect(f"tcp://{ZMQ_HOST}:{ZMQ_PORTS['THINKING_TTS_PUB']}")
sub_socket.setsockopt_string(zmq.SUBSCRIBE, "think")

def send_to_display_pub(text, audio_data, cough=None, is_streaming_chunk=False, is_streaming_end=False):
    """
    发送数据到 display 模块
    
    Args:
        text: 文本内容
        audio_data: 音频数据（bytes），如果是流式块则为单个块
        cough: 控制信号
        is_streaming_chunk: 是否为流式数据块（True表示这是流式传输中的一个块）
        is_streaming_end: 是否为流式传输结束标志
    """
    try:
        audio_b64 = ""
        if audio_data:
            audio_b64 = base64.b64encode(audio_data).decode('utf-8')
            
        data = {
            "sender": "TTS",
            "text": text,
            "voice": audio_b64
        }
        if cough is not None:
            data['cough'] = cough
        if is_streaming_chunk:
            data['streaming'] = True
        if is_streaming_end:
            data['streaming_end'] = True
            
        # 发送: topic="tts", payload=json
        pub_socket.send_multipart([b"tts", json.dumps(data).encode('utf-8')])
        
        log_text = text[:10] + "..." if text else "[Empty Text]"
        if is_streaming_chunk:
            print(f"📤 PUB tts chunk | AudioLen: {len(audio_b64)}", end='\r')
        else:
            print(f"✅ PUB tts | Text: {log_text} | Cough: {cough} | AudioLen: {len(audio_b64)}")

    except Exception as e:
        print(f"❌ Failed to PUB tts: {e}")

# TTS任务队列：每个文本片段都异步合成音频
tts_queue = queue.Queue()
current_lang = None
current_lang_lock = threading.Lock()  # 【修复】添加锁保护current_lang变量

def tts_worker():
    """后台TTS合成线程，为每个文本片段异步合成音频（支持流式播放）"""
    while True:
        try:
            # 从队列获取TTS任务
            task = tts_queue.get(timeout=1)
            if task is None:
                continue
            
            text, lang, cough = task
            if not text or not lang:
                continue
            
            print(f"🔊 Generating TTS: {text[:20]}...")
            try:
                # 【重构】TTS不再转发文本，只派发音频
                # 文本已由thinking模块直接发送到display，这里只处理音频
                
                # 定义流式回调函数：每收到一个数据块就立即发送
                chunk_count = 0
                def on_audio_chunk(chunk):
                    """流式音频块回调：立即发送给 display（只发送音频，不包含文本）"""
                    nonlocal chunk_count
                    if chunk:
                        chunk_count += 1
                        # 只发送音频块，不包含文本
                        send_to_display_pub("", chunk, None, is_streaming_chunk=True)
                
                # 使用流式生成，边接收边发送
                audio_data = tts_generate_streaming(text, lang, chunk_callback=on_audio_chunk)
                
                # 发送流式结束标志（携带cough标记）
                send_to_display_pub("", None, cough, is_streaming_chunk=False, is_streaming_end=True)
                
            except Exception as e:
                print(f"TTS Gen Error: {e}")
                # 即使合成失败，也发送文本（无音频）
                send_to_display_pub(text, None, cough)
            
            tts_queue.task_done()
        except queue.Empty:
            continue
        except Exception as e:
            print(f"TTS Worker Error: {e}")
            time.sleep(0.5)

# 启动TTS工作线程
tts_thread = threading.Thread(target=tts_worker, daemon=True)
tts_thread.start()

try:
    from common.module_ready import notify_module_ready
    notify_module_ready("Speaking")
except Exception as e:
    print(f"[Speaking] 就绪上报失败: {e}", flush=True)

def zmq_listener():
    global current_lang
    
    print("🚀 TTS Module Listening (ZMQ)...")
    while True:
        try:
            topic, msg = sub_socket.recv_multipart()
            data = json.loads(msg.decode('utf-8'))
            
            reply_part = data.get('reply_part', "")
            reply_lang = data.get('reply_lang')
            cough = data.get('cough')
            
            if (reply_part and reply_lang) or cough:
                # 【修复】更新当前语言，加锁保护
                if reply_lang:
                    with current_lang_lock:
                        current_lang = reply_lang
                
                # 每个文本片段都异步合成音频，文本和音频一起发送
                # 【修复】如果current_lang为None，使用reply_lang作为默认值，避免任务被跳过
                lang_to_use = None
                with current_lang_lock:
                    lang_to_use = current_lang or reply_lang
                
                if reply_part and lang_to_use:
                    # 将TTS任务加入队列，后台异步合成音频
                    # 音频合成完成后，文本和音频会一起发送
                    tts_queue.put((reply_part, lang_to_use, cough))
                elif cough:
                    # 只有cough信号，没有文本（可能是结束信号）
                    # 【修复】如果是end cough，需要单独发送
                    if cough == "end":
                        send_to_display_pub("", None, "end")
                    else:
                        send_to_display_pub("", None, cough)
            else:
                 pass # ignore
                 
        except Exception as e:
            print(f"ZMQ Listener Error: {e}")

if __name__ == "__main__":
    zmq_listener()