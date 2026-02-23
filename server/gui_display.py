import sys
import os
import json
import base64
import subprocess
import threading
import queue
import time
import zmq
from pathlib import Path

from PyQt6.QtWidgets import QApplication, QWidget, QMenu 
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer, QRectF, QPoint
from PyQt6.QtGui import QPixmap, QPainter, QColor, QFont, QBrush, QAction, QCursor, QScreen

# Windows API 调用，用于设置窗口对截图不可见
if sys.platform == 'win32':
    try:
        import ctypes
        from ctypes import wintypes
        
        # Windows API 常量
        WDA_EXCLUDEFROMCAPTURE = 0x11  # 从截图和屏幕录制中排除窗口
        
        # 定义函数签名
        user32 = ctypes.windll.user32
        SetWindowDisplayAffinity = user32.SetWindowDisplayAffinity
        SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
        SetWindowDisplayAffinity.restype = wintypes.BOOL
        
        def set_window_exclude_from_capture(hwnd):
            """设置窗口对截图不可见"""
            try:
                result = SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)
                if result:
                    print(f"[GUI Display] 窗口已设置为对截图不可见 (HWND: {hwnd})", flush=True)
                    return True
                else:
                    error_code = ctypes.get_last_error()
                    print(f"[GUI Display] 设置窗口截图不可见失败，错误代码: {error_code}", flush=True)
                    return False
            except Exception as e:
                print(f"[GUI Display] 设置窗口截图不可见时出错: {e}", flush=True)
                return False
    except Exception as e:
        print(f"[GUI Display] 无法加载 Windows API，将跳过截图保护: {e}", flush=True)
        def set_window_exclude_from_capture(hwnd):
            return False
else:
    # 非 Windows 系统，不执行任何操作
    def set_window_exclude_from_capture(hwnd):
        return False

# 确保可以从项目根目录导入 config
ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from config import ZMQ_HOST, ZMQ_PORTS

# ============================
# 路径配置
# ============================
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
# 尝试查找ffmpeg，根据你的路径结构调整
FFMPEG_BIN = os.path.join(CURRENT_DIR, "ffmpeg-N-122425-g21a3e44fbe-win64-gpl-shared", "ffmpeg-N-122425-g21a3e44fbe-win64-gpl-shared", "bin")
os.environ["PATH"] += os.pathsep + FFMPEG_BIN

PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
MODEL_DIR = os.path.join(PROJECT_ROOT, "model")
IMAGE_IDLE = os.path.join(MODEL_DIR, "1-gigapixel-standard v2-2x.png")
IMAGE_SPEAKING = os.path.join(MODEL_DIR, "2-gigapixel-standard v2-2x.png")

# ============================
# 后台逻辑：ZMQ 通信线程
# ============================
class ZmqManager(QThread):
    """处理 ZMQ 消息接收与发送的线程"""
    # 定义信号，用于跨线程更新 UI
    update_text_signal = pyqtSignal(str)     # 更新文本
    append_text_signal = pyqtSignal(str)     # 追加文本（如翻译）
    play_audio_signal = pyqtSignal(bytes)    # 播放音频（完整音频）
    play_audio_chunk_signal = pyqtSignal(object, bool)  # 播放音频块（流式，第一个参数可以是bytes或None，第二个参数表示是否结束）
    stop_audio_signal = pyqtSignal()         # 停止播放指令
    send_control_signal = pyqtSignal(str)   # 发送控制信号（start/end）
    check_audio_ready_signal = pyqtSignal(str)  # 【修复】请求检查音频是否就绪，参数为要更新的文本
    add_text_segment_signal = pyqtSignal(str, int, object)  # 【重构】添加文本分段：文本内容、分段索引、cough标记
    
    def __init__(self):
        super().__init__()
        self.context = zmq.Context()
        self.running = True
        
        # PUB Socket (发送控制信号)
        self.pub_socket = self.context.socket(zmq.PUB)
        self.pub_socket.bind(f"tcp://*:{ZMQ_PORTS['CONTROL_PUB']}")
        self.pub_lock = threading.Lock()

    def send_control(self, status):
        """发送控制信号 (start/end)"""
        try:
            data = {"cough": status}
            with self.pub_lock:
                self.pub_socket.send_multipart([b"control", json.dumps(data).encode('utf-8')])
        except Exception as e:
            print(f"⚠️ ZMQ Send Error: {e}")

    def run(self):
        # SUB Socket (接收数据)
        sub_socket = self.context.socket(zmq.SUB)
        sub_socket_audio_play = self.context.socket(zmq.SUB)  # sing 工具等本地音频
        try:
            sub_socket.connect(f"tcp://{ZMQ_HOST}:{ZMQ_PORTS['TTS_AUDIO_PUB']}")   # TTS音频输出
            sub_socket.connect(f"tcp://{ZMQ_HOST}:{ZMQ_PORTS['THINKING_TEXT_PUB']}")  # Thinking文本输出
            sub_socket.connect(f"tcp://{ZMQ_HOST}:{ZMQ_PORTS['THINKING_TTS_PUB']}")   # Thinking音频请求/翻译
            sub_socket.connect(f"tcp://{ZMQ_HOST}:{ZMQ_PORTS['HEARING_ASR_PUB']}")    # Hearing ASR输出
            sub_socket.setsockopt_string(zmq.SUBSCRIBE, "tts")      # 音频（来自TTS）
            sub_socket.setsockopt_string(zmq.SUBSCRIBE, "text")     # 文本（直接来自thinking）
            sub_socket.setsockopt_string(zmq.SUBSCRIBE, "think")    # 翻译等
            sub_socket.setsockopt_string(zmq.SUBSCRIBE, "asr")      # ASR输出

            if "AUDIO_PLAY_PUB" in ZMQ_PORTS:
                sub_socket_audio_play.connect(f"tcp://{ZMQ_HOST}:{ZMQ_PORTS['AUDIO_PLAY_PUB']}")
                sub_socket_audio_play.setsockopt_string(zmq.SUBSCRIBE, "tts")

            poller = zmq.Poller()
            poller.register(sub_socket, zmq.POLLIN)
            poller.register(sub_socket_audio_play, zmq.POLLIN)
            
            print("🚀 ZMQ Listening...", flush=True)
            try:
                from common.module_ready import notify_module_ready
                notify_module_ready("Display")
            except Exception as e:
                print(f"[Display] 就绪上报失败: {e}", flush=True)
            
            while self.running:
                # 使用 poll 设置超时，保证线程可以响应退出信号
                socks = dict(poller.poll(100))
                for sock, _ in list(socks.items()):
                    if sock != sub_socket and sock != sub_socket_audio_play:
                        continue
                    try:
                        topic, msg = sock.recv_multipart()
                        topic_str = topic.decode('utf-8')
                        data = json.loads(msg.decode('utf-8'))
                    except Exception as e:
                        print(f"ZMQ Message Error: {e}")
                        continue
                    # 来自 AUDIO_PLAY_PUB（sing 工具）的只有 tts，仅做播放
                    if sock == sub_socket_audio_play:
                        if topic_str == "tts" and data.get("voice"):
                            try:
                                audio_data = base64.b64decode(data["voice"])
                                self.play_audio_signal.emit(audio_data)
                            except Exception as e:
                                print(f"Decode/play error (audio_play): {e}")
                        continue
                    # 主 SUB：TTS / 文本 / think / asr
                    if topic_str == "tts":
                        # 【重构】TTS只发送音频，不包含文本
                        # 文本已由thinking模块直接发送到display
                        cough = data.get('cough')
                        voice_b64 = data.get('voice', '')
                        is_streaming = data.get('streaming', False)
                        is_streaming_end = data.get('streaming_end', False)
                        
                        # 不在 TTS 侧处理 start cough，避免与 streaming_end 同包时清空队列导致缺开头/只播开头
                        # start cough 仅在 topic "text" 时处理（新回复首句先到达）
                        
                        # 处理音频播放 - 支持流式和完整音频两种模式
                        if voice_b64:
                            try:
                                audio_data = base64.b64decode(voice_b64)
                                
                                if is_streaming:
                                    # 流式播放：发送音频块（携带分段信息）
                                    # 音频块到达时，会触发对应文本段的流式打印
                                    self.play_audio_chunk_signal.emit(audio_data, is_streaming_end)
                                else:
                                    # 完整音频：传统方式
                                    self.play_audio_signal.emit(audio_data)
                            except Exception as e:
                                print(f"Decode error: {e}")
                        elif is_streaming_end:
                            # 处理流式结束标志
                            print("📥 收到流式结束标志")
                            self.play_audio_chunk_signal.emit(b'', True)
                        
                        # 处理end cough：音频播放完成后发送
                        if cough == "end" and is_streaming_end:
                            # end cough会在音频播放完成后由AudioWorker发送
                            pass
                            
                    elif topic_str == "text":
                        # 【重构】处理直接来自thinking的文本
                        text = data.get('text', '')
                        segment_index = data.get('segment_index', 0)
                        cough = data.get('cough')
                        lang = data.get('lang', 'zh')
                        translation = data.get('translation')
                        origin = data.get('origin')
                        
                        # 新回复首句：先停止上一轮播放并清空队列，再收本轮音频，避免缺开头或只播开头
                        if cough == "start":
                            self.stop_audio_signal.emit()
                        
                        # 发送信号添加文本分段
                        self.add_text_segment_signal.emit(text, segment_index, {
                            'cough': cough,
                            'lang': lang,
                            'translation': translation,
                            'origin': origin
                        })
                        
                    elif topic_str == "think":
                        trans = data.get('translation')
                        if trans: 
                            self.append_text_signal.emit(f"\n【翻译】{trans}\n")
                        
                    elif topic_str == "asr":
                        if data.get("sys_cmd") == "stop_speaking":
                            self.stop_audio_signal.emit()
                        
        except Exception as e:
            print(f"ZMQ Connection Error: {e}")
        finally:
            print("⚠️ ZMQ Receiver thread exiting...")
            sub_socket.close()
            sub_socket_audio_play.close()

    def close_all(self):
        """清理 ZMQ 资源"""
        self.running = False
        try:
            self.pub_socket.close()
            self.context.term()
            print("✅ ZMQ Context Destroyed")
        except Exception as e:
            print(f"Error destroying context: {e}")

# ============================
# 后台逻辑：音频播放线程
# ============================
class AudioWorker(QThread):
    """处理音频播放队列，避免阻塞 UI，支持流式播放。流式由独立 feeder 线程持续写 stdin，避免间断导致 ffplay 提前退出。"""
    
    def __init__(self, audio_queue, zmq_manager, streaming_chunk_queue=None):
        super().__init__()
        self.audio_queue = audio_queue
        self.zmq_manager = zmq_manager
        self.streaming_chunk_queue = streaming_chunk_queue if streaming_chunk_queue is not None else queue.Queue()
        self.running = True
        self.current_process = None
        self.process_lock = threading.Lock()
        
        # 流式播放相关（管道方案：ffplay 从 stdin 读取，由 feeder 线程持续写入）
        self.streaming_process = None
        self.streaming_lock = threading.Lock()
        self.first_chunk_received = False
        self.end_signal_sent = False
        self._feeder_thread = None
        if self.streaming_chunk_queue is not None:
            self._feeder_thread = threading.Thread(target=self._streaming_feeder_loop, daemon=True)
            self._feeder_thread.start()
    
    def _reset_streaming_state(self):
        """重置流式播放状态，确保下次能正常播放"""
        with self.process_lock:
            if self.current_process == self.streaming_process:
                self.current_process = None
        self.streaming_process = None
        self.first_chunk_received = False
        self.end_signal_sent = False
        print("🔄 流式播放状态已重置")

    def _streaming_feeder_loop(self):
        """专用线程：从 streaming_chunk_queue 取块并持续写入 ffplay stdin，避免主 worker 在 queue.get 阻塞时管道断流。"""
        while self.running:
            try:
                item = self.streaming_chunk_queue.get()
                if item is None:
                    # 停止信号：终止当前流式播放
                    with self.streaming_lock:
                        proc = self.streaming_process
                        if proc:
                            try:
                                if proc.stdin:
                                    try: proc.stdin.close()
                                    except Exception: pass
                                proc.terminate()
                                proc.wait(timeout=2)
                            except Exception:
                                try: proc.kill()
                                except Exception: pass
                            self.streaming_process = None
                            with self.process_lock:
                                if self.current_process == proc:
                                    self.current_process = None
                            self._reset_streaming_state()
                            if not self.end_signal_sent:
                                self.zmq_manager.send_control("end")
                                self.end_signal_sent = True
                    continue
                chunk, is_end = item
                with self.streaming_lock:
                    try:
                        if is_end and not chunk and self.streaming_process is None:
                            if not self.end_signal_sent:
                                self.zmq_manager.send_control("end")
                                self.end_signal_sent = True
                            continue
                        if self.streaming_process is None and chunk:
                            self.zmq_manager.send_control("start")
                            self.end_signal_sent = False
                            ffplay_path = os.path.join(FFMPEG_BIN, "ffplay.exe")
                            if not os.path.exists(ffplay_path):
                                ffplay_path = "ffplay"
                            startupinfo = subprocess.STARTUPINFO()
                            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                            self.streaming_process = subprocess.Popen(
                                [ffplay_path, "-i", "pipe:0", "-nodisp", "-autoexit", "-hide_banner", "-loglevel", "error"],
                                stdin=subprocess.PIPE, stderr=subprocess.PIPE, stdout=subprocess.DEVNULL, startupinfo=startupinfo
                            )
                            with self.process_lock:
                                self.current_process = self.streaming_process
                            self.first_chunk_received = True
                            print("🎵 流式播放已启动（feeder 线程）")
                        if chunk and self.streaming_process and self.streaming_process.stdin:
                            try:
                                self.streaming_process.stdin.write(chunk)
                                self.streaming_process.stdin.flush()
                            except (BrokenPipeError, OSError) as e:
                                print(f"⚠️ 写入管道失败: {e}")
                        if is_end and self.streaming_process:
                            try:
                                if self.streaming_process.stdin:
                                    self.streaming_process.stdin.close()
                            except Exception: pass
                            try:
                                self.streaming_process.wait()
                            except Exception: pass
                            with self.process_lock:
                                if self.current_process == self.streaming_process:
                                    self.current_process = None
                            self.streaming_process = None
                            self._reset_streaming_state()
                            if not self.end_signal_sent:
                                self.zmq_manager.send_control("end")
                                self.end_signal_sent = True
                                print("🎵 流式播放完成，已发送 end 信号")
                    except Exception as e:
                        print(f"❌ 流式 feeder 异常: {e}")
                        import traceback
                        traceback.print_exc()
                        if self.streaming_process:
                            try:
                                if self.streaming_process.stdin:
                                    self.streaming_process.stdin.close()
                            except Exception: pass
                            try: self.streaming_process.terminate()
                            except Exception: pass
                            self.streaming_process = None
                        with self.process_lock:
                            if self.current_process == self.streaming_process:
                                self.current_process = None
                        self._reset_streaming_state()
                        if not self.end_signal_sent:
                            self.zmq_manager.send_control("end")
                            self.end_signal_sent = True
            except Exception as e:
                if self.running:
                    print(f"Streaming feeder get error: {e}")

    def run(self):
        while self.running:
            try:
                # 获取任务，超时1秒以便检查running状态
                try:
                    item = self.audio_queue.get(timeout=1)
                except queue.Empty:
                    continue

                if callable(item):
                    item() # 执行函数任务
                elif isinstance(item, tuple) and item[0] == 'streaming_chunk':
                    # 流式块由 feeder 线程处理，此处仅处理“仅结束标志无数据”的兜底（无 feeder 时）
                    _, audio_chunk, is_end = item
                    if is_end and not (audio_chunk and len(audio_chunk) > 0):
                        with self.streaming_lock:
                            if self.streaming_process is None and not self.end_signal_sent:
                                self.zmq_manager.send_control("end")
                                self.end_signal_sent = True
                else:
                    # 完整音频
                    self.play_audio(item)
                
                self.audio_queue.task_done()
            except Exception as e:
                print(f"Audio Worker Error: {e}")

    def play_audio(self, audio_data):
        """播放完整音频（传统方式）"""
        if not self.running: return
        
        temp_file_path = None
        try:
            # 1. 发送开始信号 (暂停录音)
            self.zmq_manager.send_control("start")
            # 【修复】重置end信号标志
            self.end_signal_sent = False
            
            # 2. 写入临时文件
            import tempfile
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp_file:
                temp_file.write(audio_data)
                temp_file_path = temp_file.name
            
            # 3. 确定 ffplay 路径
            ffplay_path = os.path.join(FFMPEG_BIN, "ffplay.exe")
            if not os.path.exists(ffplay_path): 
                ffplay_path = "ffplay"
            
            # 4. 启动进程 (隐藏窗口)
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            
            process = subprocess.Popen(
                [ffplay_path, "-i", temp_file_path, "-nodisp", "-autoexit", "-hide_banner", "-loglevel", "error"],
                stderr=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                startupinfo=startupinfo
            )
            
            with self.process_lock:
                self.current_process = process
            
            # 5. 等待播放结束
            process.wait()
            
            with self.process_lock:
                self.current_process = None
            
            # 6. 【修复】发送结束信号 (恢复录音)，防止重复发送
            if not hasattr(self, 'end_signal_sent') or not self.end_signal_sent:
                self.zmq_manager.send_control("end")
                self.end_signal_sent = True
            
        except Exception as e:
            print(f"Play audio failed: {e}")
            # 【修复】错误时也发送end信号，防止重复发送
            if not hasattr(self, 'end_signal_sent') or not self.end_signal_sent:
                self.zmq_manager.send_control("end")
                self.end_signal_sent = True
        finally:
            # 清理临时文件
            if temp_file_path and os.path.exists(temp_file_path):
                try: os.unlink(temp_file_path)
                except: pass
    
    def play_audio_chunk(self, audio_chunk, is_end=False):
        """流式播放：ffplay 从标准输入管道读取 WAV 数据，无临时文件"""
        if not self.running:
            return
        
        with self.streaming_lock:
            try:
                # 收到结束标志但从未启动过流式播放（无任何数据块）
                if is_end and self.streaming_process is None:
                    if not self.end_signal_sent:
                        self.zmq_manager.send_control("end")
                        self.end_signal_sent = True
                    return
                
                # 第一个非空数据块：发送开始信号并启动 ffplay（从 pipe:0 读 stdin）
                if self.streaming_process is None and audio_chunk:
                    self.zmq_manager.send_control("start")
                    self.end_signal_sent = False
                    
                    ffplay_path = os.path.join(FFMPEG_BIN, "ffplay.exe")
                    if not os.path.exists(ffplay_path):
                        ffplay_path = "ffplay"
                    
                    startupinfo = subprocess.STARTUPINFO()
                    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                    
                    try:
                        # pipe:0 表示从标准输入读取；stdin=PIPE 由本进程写入
                        self.streaming_process = subprocess.Popen(
                            [ffplay_path, "-i", "pipe:0", "-nodisp", "-autoexit",
                             "-hide_banner", "-loglevel", "error"],
                            stdin=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            stdout=subprocess.DEVNULL,
                            startupinfo=startupinfo
                        )
                        with self.process_lock:
                            self.current_process = self.streaming_process
                        print("🎵 流式播放已启动（管道）")
                    except Exception as e:
                        print(f"⚠️ 启动 ffplay 失败: {e}")
                        if not self.end_signal_sent:
                            self.zmq_manager.send_control("end")
                            self.end_signal_sent = True
                        return
                
                # 写入音频块到 ffplay 的 stdin
                if audio_chunk and self.streaming_process and self.streaming_process.stdin:
                    if not self.first_chunk_received:
                        self.first_chunk_received = True
                        print(f"📥 收到第一个音频块: {len(audio_chunk)} bytes")
                    try:
                        self.streaming_process.stdin.write(audio_chunk)
                        self.streaming_process.stdin.flush()
                    except (BrokenPipeError, OSError) as e:
                        print(f"⚠️ 写入管道失败（进程可能已退出）: {e}")
                
                # 流结束：关闭 stdin 通知 ffplay EOF，等待播放结束
                if is_end and self.streaming_process:
                    try:
                        if self.streaming_process.stdin:
                            self.streaming_process.stdin.close()
                    except Exception:
                        pass
                    try:
                        self.streaming_process.wait()
                    except Exception as e:
                        print(f"⚠️ 等待播放结束失败: {e}")
                    
                    with self.process_lock:
                        if self.current_process == self.streaming_process:
                            self.current_process = None
                    self.streaming_process = None
                    self._reset_streaming_state()
                    
                    with self.process_lock:
                        if self.current_process:
                            self.current_process = None
                    
                    if not self.end_signal_sent:
                        self.zmq_manager.send_control("end")
                        self.end_signal_sent = True
                        print("🎵 流式播放完成，已发送 end 信号")
                    
            except Exception as e:
                print(f"❌ 流式播放异常: {e}")
                import traceback
                traceback.print_exc()
                if self.streaming_process:
                    try:
                        if self.streaming_process.stdin:
                            self.streaming_process.stdin.close()
                    except Exception:
                        pass
                    try:
                        self.streaming_process.terminate()
                    except Exception:
                        pass
                    self.streaming_process = None
                with self.process_lock:
                    if self.current_process == self.streaming_process:
                        self.current_process = None
                self._reset_streaming_state()
                if not self.end_signal_sent:
                    self.zmq_manager.send_control("end")
                    self.end_signal_sent = True

    def stop_current(self):
        """强制停止当前播放和清空队列（包括流式播放）"""
        # 清空主音频队列
        while not self.audio_queue.empty():
            try: 
                self.audio_queue.get_nowait()
                self.audio_queue.task_done()
            except: 
                break
        
        # 清空流式队列并通知 feeder 终止
        if self.streaming_chunk_queue is not None:
            while not self.streaming_chunk_queue.empty():
                try:
                    self.streaming_chunk_queue.get_nowait()
                except queue.Empty:
                    break
            self.streaming_chunk_queue.put(None)
        
        # 停止流式播放（管道方案：关闭 stdin 再终止进程）
        with self.streaming_lock:
            proc = self.streaming_process
            if proc:
                try:
                    print("🔪 正在强制终止流式播放进程...", flush=True)
                    if proc.stdin:
                        try:
                            proc.stdin.close()
                        except Exception:
                            pass
                    proc.terminate()
                    proc.wait(timeout=2)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                self.streaming_process = None
                with self.process_lock:
                    if self.current_process == proc:
                        self.current_process = None
        
        # 杀掉常规播放进程
        with self.process_lock:
            if self.current_process:
                try:
                    print("🔪 正在强制终止 ffplay 进程...", flush=True)
                    self.current_process.terminate()
                except Exception as e:
                    print(f"⚠️ 终止进程失败: {e}")

# ============================
# GUI：主显示窗口
# ============================
class DesktopPet(QWidget):
    def __init__(self):
        super().__init__()
        
        # --- 窗口属性设置 ---
        # FramelessWindowHint: 无边框
        # WindowStaysOnTopHint: 置顶
        # WA_TranslucentBackground: 关键！实现真正的透明背景
        # Tool: 使得任务栏图标可选显示
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        
        # --- 尺寸与位置 ---
        # 保存逻辑尺寸（不受DPI影响）
        self.logical_width_ratio = 0.3   # 窗口宽度占屏幕的30%
        self.logical_height_ratio = 0.6  # 窗口高度占屏幕的60%
        
        # 初始化窗口大小和位置
        self._update_window_size_and_position()
        
        # --- 状态数据 ---
        self.text_content = "准备就绪...\n等待输入"
        self.is_speaking = False
        self.drag_pos = None # 用于鼠标拖拽
        
        # 加载图片
        self.pixmap_idle = self.load_image(IMAGE_IDLE)
        self.pixmap_speaking = self.load_image(IMAGE_SPEAKING)
        self.current_pixmap = self.pixmap_idle
        
        # 【修复】缓存缩放后的图片，避免每次paintEvent都重新计算
        self.cached_scaled_pixmap = None
        self.cached_window_height = 0
        self.cached_is_speaking = False
        
        # 【修复】文本区域缓存，用于局部重绘优化
        self.text_rect_cache = None

        # --- 初始化后台线程 ---
        self.audio_queue = queue.Queue()
        # 流式专用队列：主线程收到块即放入，feeder 线程持续取出写 ffplay stdin，避免间断导致“播一会儿就没声”
        self.streaming_chunk_queue = queue.Queue()
        
        # 1. 启动 ZMQ
        self.zmq_manager = ZmqManager()
        self.zmq_manager.update_text_signal.connect(self.on_update_text)
        self.zmq_manager.append_text_signal.connect(self.on_append_text)
        self.zmq_manager.play_audio_signal.connect(self.on_receive_audio)
        self.zmq_manager.play_audio_chunk_signal.connect(self.on_receive_audio_chunk)
        self.zmq_manager.stop_audio_signal.connect(self.on_stop_audio)
        self.zmq_manager.send_control_signal.connect(self.zmq_manager.send_control)
        # 【修复】连接新信号，用于检查音频状态
        self.zmq_manager.check_audio_ready_signal.connect(self.on_check_audio_ready)
        # 【修复】连接文本分段信号，用于接收和显示文本
        self.zmq_manager.add_text_segment_signal.connect(self.on_add_text_segment)
        self.zmq_manager.start()
        
        # 2. 启动音频（传入流式队列，feeder 线程会持续写 stdin）
        self.audio_worker = AudioWorker(self.audio_queue, self.zmq_manager, self.streaming_chunk_queue)
        self.audio_worker.start()
        
        # 3. 动画检测定时器 (100ms 检查一次音频状态)
        self.anim_timer = QTimer()
        self.anim_timer.timeout.connect(self.check_speaking_status)
        self.anim_timer.start(100)
        
        # 【重构】文本分段缓存机制初始化
        self.text_segments_cache = []  # 存储文本分段：[{"text": "...", "segment_index": 0, "metadata": {...}}, ...]
        self.current_audio_segment_index = -1  # 当前正在播放的音频对应的分段索引
        self.pending_audio_segments = {}  # 等待音频的分段：{segment_index: {"text": "...", "metadata": {...}}}
        
        print("display已启动", flush=True)
    
    def showEvent(self, event):
        """窗口显示时的事件处理"""
        super().showEvent(event)
        # 在窗口显示后连接屏幕变化信号
        if not hasattr(self, '_screen_changed_connected'):
            self._screen_changed_connected = False
        if not self._screen_changed_connected and self.windowHandle():
            self.windowHandle().screenChanged.connect(self._on_screen_changed)
            self._screen_changed_connected = True
        
        # 设置窗口对截图不可见
        if sys.platform == 'win32':
            try:
                # 获取窗口的 HWND（Windows 句柄）
                hwnd = int(self.winId())
                        
                set_window_exclude_from_capture(hwnd)
            except Exception as e:
                print(f"[GUI Display] 获取窗口句柄失败: {e}", flush=True)
                import traceback
                traceback.print_exc()
    
    def _update_window_size_and_position(self):
        """根据当前屏幕更新窗口大小和位置（处理多显示器DPI问题）"""
        # 获取窗口当前所在的屏幕
        current_screen = self.screen() if self.isVisible() else QApplication.primaryScreen()
        if current_screen is None:
            current_screen = QApplication.primaryScreen()
        
        # 获取屏幕的逻辑尺寸（考虑DPI缩放）
        screen_geometry = current_screen.geometry()
        screen_dpi_ratio = current_screen.devicePixelRatio()
        
        # 计算逻辑像素大小（不受DPI影响）
        logical_width = int(screen_geometry.width() * self.logical_width_ratio)
        logical_height = int(screen_geometry.height() * self.logical_height_ratio)
        
        # 设置窗口大小（使用逻辑像素）
        self.resize(logical_width, logical_height)
        
        # 如果窗口还未显示，移动到屏幕右下角
        if not self.isVisible():
            w, h = logical_width, logical_height
            self.move(screen_geometry.width() - w - 50, screen_geometry.height() - h - 100)
    
    def _on_screen_changed(self, screen):
        """当窗口移动到不同屏幕时调用，重新计算窗口大小"""
        if screen:
            self._update_window_size_and_position()
            self.update()  # 触发重绘

    def load_image(self, path):
        if os.path.exists(path):
            return QPixmap(path)
        print(f"❌ 无法找到图片: {path}")
        # 返回一个透明的空图片防止崩溃
        img = QPixmap(100, 100)
        img.fill(Qt.GlobalColor.transparent)
        return img

    def check_speaking_status(self):
        """根据音频线程状态切换图片"""
        is_busy = False
        # 关键修复：同时检查 current_process 和 streaming_process
        with self.audio_worker.process_lock:
            if self.audio_worker.current_process:
                # 检查进程是否还在运行
                if self.audio_worker.current_process.poll() is None:
                    is_busy = True
        
        # 检查流式播放进程
        with self.audio_worker.streaming_lock:
            if self.audio_worker.streaming_process:
                # 检查进程是否还在运行
                if self.audio_worker.streaming_process.poll() is None:
                    is_busy = True
            
        if is_busy != self.is_speaking:
            self.is_speaking = is_busy
            self.current_pixmap = self.pixmap_speaking if self.is_speaking else self.pixmap_idle
            # 【修复】清除缓存，强制重新计算缩放图片
            self.cached_scaled_pixmap = None
            self.update() # 触发重绘 paintEvent

    # --- 信号槽函数 ---
    def on_update_text(self, text):
        """重置文本（新对话开始）"""
        self.text_content = text
        # 重置打字机效果
        if hasattr(self, 'typewriter_timer') and self.typewriter_timer:
            self.typewriter_timer.stop()
            self.typewriter_buffer = ""
            self.typewriter_index = 0
        # 【修复】重置文本分段缓存
        self.text_segments_cache = []
        self.current_audio_segment_index = -1
        self.update()

    def on_append_text(self, text):
        """追加文本，实现打字机效果（批量显示字符，优化长文本性能）"""
        if not text:
            return
        
        # 初始化打字机效果定时器（如果未初始化）
        if not hasattr(self, 'typewriter_timer') or self.typewriter_timer is None:
            self.typewriter_buffer = ""  # 待显示的文本缓冲区
            self.typewriter_index = 0     # 当前显示位置
            self.typewriter_timer = QTimer()
            self.typewriter_timer.timeout.connect(self._typewriter_step)
            # 【优化】批量显示模式：每次显示多个字符，减少重绘次数
            # 对于中文长文本，批量显示可以显著提升性能
            self.typewriter_timer.setInterval(30)  # 30ms 更新一次（批量显示）
            self.chars_per_step = 3  # 每次显示3个字符（中文）或5个字符（英文）
        
        # 将新文本添加到缓冲区
        self.typewriter_buffer += text
        
        # 如果定时器未运行，启动它
        if not self.typewriter_timer.isActive():
            self.typewriter_timer.start()
    
    def _typewriter_step(self):
        """打字机效果的每一步：批量显示多个字符（优化长文本性能）"""
        if not hasattr(self, 'typewriter_buffer') or not self.typewriter_buffer:
            if hasattr(self, 'typewriter_timer') and self.typewriter_timer:
                self.typewriter_timer.stop()
            return
        
        # 批量显示字符（处理中文字符）
        if self.typewriter_index < len(self.typewriter_buffer):
            # 【优化】批量显示：根据字符类型决定每次显示的数量
            # 中文字符通常更宽，每次显示较少；英文/数字可以显示更多
            remaining = len(self.typewriter_buffer) - self.typewriter_index
            chars_to_show = min(self.chars_per_step, remaining)
            
            # 获取要显示的字符
            chars = self.typewriter_buffer[self.typewriter_index:self.typewriter_index + chars_to_show]
            
            # 动态调整显示速度：如果文本很长，增加每次显示的字符数
            if len(self.typewriter_buffer) > 100:
                # 长文本：每次显示更多字符，加快显示速度
                if remaining > chars_to_show:
                    # 尝试显示更多字符（最多10个）
                    additional_chars = min(7, remaining - chars_to_show)
                    chars = self.typewriter_buffer[self.typewriter_index:self.typewriter_index + chars_to_show + additional_chars]
                    chars_to_show += additional_chars
            
            self.text_content += chars
            self.typewriter_index += chars_to_show
            
            # 【优化】使用局部重绘，只更新文本区域
            # 对于长文本，减少重绘频率可以显著提升性能
            if self.text_rect_cache:
                self.update(self.text_rect_cache)
            else:
                # 如果缓存未初始化，使用全窗口更新
                self.update()
        else:
            # 缓冲区已全部显示，清空并停止定时器
            self.typewriter_buffer = ""
            self.typewriter_index = 0
            if hasattr(self, 'typewriter_timer') and self.typewriter_timer:
                self.typewriter_timer.stop()

    def on_receive_audio(self, audio_data):
        """接收完整音频数据"""
        self.audio_queue.put(audio_data)
    
    def on_receive_audio_chunk(self, audio_chunk, is_end):
        """接收流式音频块：放入流式专用队列，由 feeder 线程持续写 ffplay stdin，实现真正流式播放。"""
        safe_chunk = (audio_chunk if audio_chunk is not None else b'')
        if safe_chunk:
            print(f"📥 收到音频块: {len(safe_chunk)} bytes, is_end={is_end}")
        elif is_end:
            print(f"📥 收到流式结束标志 (无数据)")
        
        if self.text_segments_cache and safe_chunk:
            next_segment_index = self.current_audio_segment_index + 1
            for segment in self.text_segments_cache:
                if segment['segment_index'] == next_segment_index:
                    self.current_audio_segment_index = next_segment_index
                    break
        
        self.streaming_chunk_queue.put((safe_chunk, is_end))
    
    def _stream_print_text_segment(self, text):
        """流式打印文本段（追加到现有文本）"""
        if not text:
            return
        
        # 【修复】追加文本到当前内容（避免重复追加）
        # 检查文本是否已经显示过（通过检查text_content是否以该文本结尾）
        if not self.text_content.endswith(text):
            self.text_content += text
        
        # 使用局部重绘更新显示
        if self.text_rect_cache:
            self.update(self.text_rect_cache)
        else:
            self.update()

    def on_stop_audio(self):
        # 清空流式队列并通知 feeder 终止当前流式播放
        while not self.streaming_chunk_queue.empty():
            try:
                self.streaming_chunk_queue.get_nowait()
            except queue.Empty:
                break
        self.streaming_chunk_queue.put(None)  # 通知 feeder 终止
        self.audio_worker.stop_current()
        self.update()
    
    def on_add_text_segment(self, text, segment_index, metadata):
        """【重构】添加文本分段到缓存，并立即显示"""
        if not text:
            return
        
        segment_data = {
            "text": text,
            "segment_index": segment_index,
            "metadata": metadata
        }
        
        # 添加到缓存
        self.text_segments_cache.append(segment_data)
        
        # 如果这是第一段且带有start cough，清空之前的内容
        if metadata.get('cough') == 'start':
            self.text_content = ""
            self.text_segments_cache = [segment_data]  # 重置缓存，只保留当前段
            self.current_audio_segment_index = -1
            self.pending_audio_segments = {}
            # 【修复】start时立即显示第一段文本
            self._stream_print_text_segment(text)
        else:
            # 【修复】文本到达时立即追加显示，不等待音频
            # 音频到达时会同步播放，但文本应该立即显示
            self._stream_print_text_segment(text)
        
        # 检查是否有对应的音频（音频会稍后到达）
        # 音频到达时会触发流式打印（但文本已经显示了，所以不会重复）
        # 如果没有音频，直接显示所有缓存的文本（作为兜底）
        if metadata.get('cough') == 'end' and not self.pending_audio_segments:
            # 如果没有待播放的音频，确保所有文本都已显示
            self._display_all_cached_text()
    
    def _display_all_cached_text(self):
        """显示所有缓存的文本"""
        full_text = ""
        for segment in sorted(self.text_segments_cache, key=lambda x: x['segment_index']):
            full_text += segment['text']
        self.text_content = full_text
        if self.text_rect_cache:
            self.update(self.text_rect_cache)
        else:
            self.update()
    
    def on_check_audio_ready(self, text):
        """【修复】检查音频是否就绪，如果就绪则更新文本"""
        # 检查音频队列是否为空
        if self.audio_queue.empty():
            # 检查是否有正在播放的音频
            has_playing = False
            has_streaming = False
            
            with self.audio_worker.process_lock:
                if self.audio_worker.current_process:
                    if self.audio_worker.current_process.poll() is None:
                        has_playing = True
            
            with self.audio_worker.streaming_lock:
                if self.audio_worker.streaming_process:
                    if self.audio_worker.streaming_process.poll() is None:
                        has_streaming = True
            
            if not has_playing and not has_streaming:
                # 队列已清空，可以更新文本
                if text:
                    self.on_update_text(text)
                return True
        
        # 如果还没就绪，使用定时器定期检查
        if not hasattr(self, 'start_cough_timer') or self.start_cough_timer is None:
            self.start_cough_timer = QTimer()
            self.start_cough_timer.timeout.connect(lambda: self.on_check_audio_ready(text))
            self.start_cough_timer.setInterval(50)  # 每50ms检查一次
        
        if not self.start_cough_timer.isActive():
            self.start_cough_timer.start()
        
        return False

    # ============================
    # 核心绘制逻辑 (paintEvent)
    # ============================
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)       # 抗锯齿
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform) # 图片平滑
        
        w = self.width()
        h = self.height()
        
        # 布局参数
        text_height_ratio = 0.25  # 文本框占底部 25%
        text_height = int(h * text_height_ratio)
        
        # --- 1. 绘制人物 (裁剪 + 放大) ---
        # 【修复】使用缓存机制，避免每次paintEvent都重新计算
        need_recalculate = (
            self.cached_scaled_pixmap is None or
            self.cached_window_height != h or
            self.cached_is_speaking != self.is_speaking
        )
        
        if need_recalculate and self.current_pixmap and not self.current_pixmap.isNull():
            # A. 获取原始尺寸
            orig_w = self.current_pixmap.width()
            orig_h = self.current_pixmap.height()
            
            # B. 裁剪逻辑：只取上 4/5 (0.8)
            crop_height = int(orig_h * 0.8)
            
            # copy(x, y, w, h) -> 从 (0,0) 开始截取，截掉底部 20%
            cropped_pixmap = self.current_pixmap.copy(0, 0, orig_w, crop_height)
            
            # C. 放大逻辑
            # 让裁剪后的人物占据窗口高度的 90% (之前是 85%，且之前包含腿部)
            # 因为去掉了腿，现在头部和上半身会变得非常大
            target_display_h = int(h * 0.90) 
            
            # D. 生成最终缩放图片（缓存结果）
            self.cached_scaled_pixmap = cropped_pixmap.scaledToHeight(
                target_display_h, 
                Qt.TransformationMode.SmoothTransformation
            )
            self.cached_window_height = h
            self.cached_is_speaking = self.is_speaking
        
        # E. 居中绘制（使用缓存的图片）
        if self.cached_scaled_pixmap and not self.cached_scaled_pixmap.isNull():
            img_x = (w - self.cached_scaled_pixmap.width()) // 2
            
            # F. Y轴位置调整
            # 让人物稍微靠上一点，或者底部贴着窗口底部
            # 这里设为：窗口高度 - 图片高度 (即底部对齐)
            # 也可以减去一点数值让它稍微悬空： h - scaled_pixmap.height() - 50
            img_y = h - self.cached_scaled_pixmap.height() - 10 
            
            painter.drawPixmap(img_x, img_y, self.cached_scaled_pixmap)

        # --- 2. 绘制文本框背景 (半透明) ---
        rect_margin = 10
        # 文本框位置保持在底部
        text_rect = QRectF(rect_margin, h - text_height, w - 2 * rect_margin, text_height - 10)
        
        # 【修复】缓存文本区域，用于局部重绘
        self.text_rect_cache = text_rect.toRect()
        
        # 设定背景色：白色，Alpha=180 (半透明)
        bg_color = QColor(255, 255, 255, 180) 
        painter.setBrush(QBrush(bg_color))
        painter.setPen(Qt.PenStyle.NoPen) # 无边框
        
        # 绘制圆角矩形
        painter.drawRoundedRect(text_rect, 15, 15)
        
        # --- 3. 绘制文字 ---
        painter.setPen(QColor(0, 0, 0)) # 黑色文字
        font = QFont("Microsoft YaHei", 12)
        font.setBold(False)
        painter.setFont(font)
        
        # 文字内边距
        text_inner_rect = text_rect.adjusted(10, 10, -10, -10)
        
        # 【优化】对于长文本，使用更高效的文本绘制方式
        # 如果文本很长，只显示可见部分（优化性能）
        if len(self.text_content) > 500:
            # 长文本优化：只渲染最后500个字符（可见部分）
            # 这样可以避免绘制大量不可见文本导致的性能问题
            display_text = self.text_content[-500:]
            # 添加省略号提示
            if len(self.text_content) > 500:
                display_text = "..." + display_text
        else:
            display_text = self.text_content
        
        # 自动换行绘制
        painter.drawText(
            text_inner_rect, 
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap, 
            display_text
        )

    # ============================
    # 右键菜单逻辑
    # ============================
    def contextMenuEvent(self, event):
        """处理右键点击，弹出菜单"""
        menu = QMenu(self)
        
        # 设置菜单样式（可选，简单的白色背景）
        menu.setStyleSheet("""
            QMenu {
                background-color: white; 
                border: 1px solid #d0d0d0;
            }
            QMenu::item {
                padding: 5px 20px;
            }
            QMenu::item:selected {
                background-color: #e0e0e0;
            }
        """)

        # --- 添加“退出”选项 ---
        quit_action = QAction("❌ 退出程序 (Exit)", self)
        # 连接到 self.close()，它会自动触发 closeEvent 执行资源清理
        quit_action.triggered.connect(self.close)
        menu.addAction(quit_action)

        # # 添加一个“重启”或“隐藏”选项
        # hide_action = QAction("🙈 隐藏 (Hide)", self)
        # hide_action.triggered.connect(self.showMinimized)
        # menu.addAction(hide_action)

        # 在鼠标位置显示菜单
        menu.exec(event.globalPos())

    # ============================
    # 鼠标拖动逻辑
    # ============================
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.MouseButton.LeftButton and self.drag_pos:
            self.move(event.globalPosition().toPoint() - self.drag_pos)
            event.accept()
            
    def mouseReleaseEvent(self, event):
        self.drag_pos = None

    # ============================
    # 资源释放逻辑
    # ============================
    def closeEvent(self, event):
        """窗口关闭时触发"""
        print("🛑 正在关闭窗口，清理资源...", flush=True)
        self.cleanup()
        event.accept()

    def cleanup(self):
        """执行具体的资源释放"""
        # 1. 停止音频
        if hasattr(self, 'audio_worker'):
            self.audio_worker.running = False
            self.audio_worker.stop_current() # 杀进程
            self.audio_worker.quit()
            self.audio_worker.wait()
        
        # 2. 停止 ZMQ
        if hasattr(self, 'zmq_manager'):
            self.zmq_manager.close_all() # 关闭socket和context
            self.zmq_manager.quit()
            self.zmq_manager.wait()
            
        print("✅ 资源清理完成，程序退出。", flush=True)

def main():
    app = QApplication(sys.argv)
    pet = DesktopPet()
    pet.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()