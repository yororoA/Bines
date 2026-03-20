import subprocess
import time
import os
import sys
import ctypes
from ctypes import wintypes
from config import ZMQ_PORTS
from process_registry import get_processes
from port_cleanup import cleanup_ports

# Define modules and their venv pythons
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))

# 控制台窗口大小配置（字符数）
CONSOLE_WIDTH = 60   # 窗口宽度（字符数），可以调整：60-120
CONSOLE_HEIGHT = 25  # 窗口高度（行数），可以调整：20-50

# 启动顺序：Classification 最先启动（先监听 MODULE_READY_REP）；
# 然后 Display/Speaking/Visual/RAG Server/Hearing 启动并就绪后向 Classification 上报；
# Classification 收齐后通知 Thinking，Thinking 最后启动（上线通知、摘要/日记等）。
# 控制台模式使用 display.py。
PROCESSES = get_processes(display_script="display.py")

processes = []

def set_console_size(pid, width=80, height=30):
    """
    设置控制台窗口大小（Windows）
    通过查找进程的控制台窗口并调整其大小
    
    Args:
        pid: 进程ID
        width: 窗口宽度（字符数），默认80
        height: 窗口高度（行数），默认30
    """
    try:
        kernel32 = ctypes.windll.kernel32
        user32 = ctypes.windll.user32
        
        # 用于存储找到的窗口句柄
        found_hwnd = [None]
        
        # 枚举窗口回调函数
        @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        def enum_windows_callback(hwnd, lParam):
            window_pid = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(window_pid))
            
            if window_pid.value == pid:
                # 检查是否是控制台窗口
                class_name = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(hwnd, class_name, 256)
                if "ConsoleWindowClass" in class_name.value:
                    found_hwnd[0] = hwnd
                    return False  # 停止枚举
            return True  # 继续枚举
        
        # 枚举所有窗口
        user32.EnumWindows(enum_windows_callback, 0)
        
        if found_hwnd[0]:
            hwnd = found_hwnd[0]
            # 获取当前窗口位置
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            
            # 估算字符大小（控制台默认字体通常是 8x16 或 9x16）
            # 使用更保守的估算
            char_width = 9
            char_height = 16
            
            # 计算新的窗口大小（像素）
            new_width = width * char_width + 16  # 加上边框
            new_height = height * char_height + 39  # 加上标题栏和边框
            
            # 设置窗口大小（保持位置不变）
            SWP_NOMOVE = 0x0002
            SWP_NOZORDER = 0x0004
            user32.SetWindowPos(
                hwnd, 0,
                rect.left, rect.top,
                new_width, new_height,
                SWP_NOMOVE | SWP_NOZORDER
            )
            return True
        return False
    except Exception as e:
        # 静默失败，不影响启动流程
        return False

def start_process(config):
    print(f"Starting {config['name']}...")
    try:
        if 'command' in config:
            cmd = config['command']
        else:
            if not os.path.exists(config['interpreter']):
                 print(f"Warning: Interpreter not found for {config['name']}: {config['interpreter']}")
            cmd = [config['interpreter'], config['script']]

        # CREATE_NEW_CONSOLE = 16
        p = subprocess.Popen(
            cmd,
            cwd=config['cwd'],
            creationflags=subprocess.CREATE_NEW_CONSOLE
        )
        processes.append(p)
        print(f"{config['name']} started (PID: {p.pid})")
        
        # 等待窗口创建，然后设置窗口大小
        time.sleep(0.3)  # 给窗口一点时间创建
        # 设置窗口大小（使用配置的值）
        set_console_size(p.pid, width=CONSOLE_WIDTH, height=CONSOLE_HEIGHT)
    except Exception as e:
        print(f"Failed to start {config['name']}: {e}")

if __name__ == "__main__":
    import argparse
    
    # 解析命令行参数
    parser = argparse.ArgumentParser(description='启动 ATRI 系统模块')
    parser.add_argument('--web', action='store_true', help='启动 Web 管理界面（不显示终端窗口）')
    parser.add_argument('--console', action='store_true', help='使用传统控制台模式（显示终端窗口）')
    args = parser.parse_args()
    
    # 默认使用 Web 模式
    use_web_mode = args.web or (not args.console)
    
    if use_web_mode:
        # Web 模式：启动模块管理服务器，等就绪后再提示/打开浏览器，避免“进不去网页”
        os.environ["NO_PROXY"] = "localhost,127.0.0.1"
        print("=" * 60)
        print("启动 Web 管理界面模式...")
        print("=" * 60)
        
        try:
            import threading
            import urllib.request
            import webbrowser
            from server.module_manager import app
            
            MANAGER_URL = "http://127.0.0.1:5000"
            ready = threading.Event()
            
            def run_flask():
                app.run(host='0.0.0.0', port=5000, debug=False, threaded=True, use_reloader=False)
            
            server_thread = threading.Thread(target=run_flask, daemon=False)
            server_thread.start()
            
            # 轮询直到页面可访问，再提示并打开浏览器，避免“经常进不去”
            print("等待 Web 服务就绪...")
            for _ in range(40):  # 约 20 秒
                time.sleep(0.5)
                try:
                    req = urllib.request.urlopen(MANAGER_URL + "/", timeout=2)
                    if req.getcode() == 200:
                        ready.set()
                        break
                except Exception:
                    pass
            
            if ready.is_set():
                print("✅ 模块管理服务器已就绪")
                print("📱 访问 http://localhost:5000 查看管理界面")
                try:
                    webbrowser.open(MANAGER_URL)
                except Exception:
                    pass
            else:
                print("⚠️ 服务启动较慢，请稍后手动访问 http://localhost:5000")
            print("=" * 60)
            
            server_thread.join()
        except ImportError as e:
            print(f"❌ 无法导入模块管理服务器: {e}")
            print("💡 请确保已安装 Flask: pip install flask flask-cors")
            sys.exit(1)
    else:
        # 传统控制台模式
        # --- 关键配置：设置不通过代理访问本地地址 ---
        os.environ["NO_PROXY"] = "localhost,127.0.0.1"
        
        print("Starting ATRI System Modules via ZMQ...")
        
        # 【优化】清理所有ZMQ使用的端口，防止 Address in use 错误
        # 端口列表来自全局配置 ZMQ_PORTS
        cleanup_ports(list(ZMQ_PORTS.values()))
        
        for p_conf in PROCESSES:
            start_process(p_conf)
            # Low latency startup
            time.sleep(1)

        print("All modules launched.")
        print("Each module runs in a separate window.")
        print("Press Ctrl+C to exit this launcher script request loop (modules survive).")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("Launcher exited.")
