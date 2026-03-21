"""
模块管理服务器：提供 Web 界面来管理和监控各个模块
"""
import subprocess
import time
import os
import sys
import threading
import queue
from datetime import datetime
from pathlib import Path
from flask import Flask, render_template, jsonify, request, Response, stream_with_context
from flask_cors import CORS
import json
import zmq

# 确保可以从项目根目录导入 config
ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from config import ZMQ_PORTS, PRESENCE_STATE_PATH
from process_registry import get_processes
from port_cleanup import cleanup_ports
from process_runtime import resolve_process_command, build_python_runtime_env
from zmq_topology import SERVICE_BIND_PORTS
from realtime_screen_config import (
    DEFAULT_REALTIME_SCREEN_CONFIG,
    load_realtime_screen_config,
    normalize_realtime_screen_config,
    save_realtime_screen_config as persist_realtime_screen_config,
)

app = Flask(__name__, template_folder=os.path.join(ROOT_DIR, "server", "templates"),
            static_folder=os.path.join(ROOT_DIR, "server", "static"))
CORS(app)


MODULE_MANAGER_PUB_PORT = 5566

# 缓存游戏模式状态（因为我们无法从Thinking进程查询状态，所以只能在管理器本地缓存一份）
GAME_MODE_STATE = {"enabled": False, "interval": 0.1}

context = zmq.Context()
pub_socket = context.socket(zmq.PUB)
try:
    pub_socket.bind(f"tcp://*:{MODULE_MANAGER_PUB_PORT}")
except zmq.ZMQError:
    # Maybe another instance is running
    pass

@app.route("/api/game_mode", methods=["GET", "POST"])
def game_mode_api():
    if request.method == "GET":
        return jsonify(GAME_MODE_STATE)

    if request.method == "POST":
        data = request.json
        enabled = data.get("enabled", False)
        interval = data.get("interval", 0.1)
        
        # 更新本地缓存
        GAME_MODE_STATE["enabled"] = enabled
        GAME_MODE_STATE["interval"] = interval
        
        cmd = "enable_game_mode" if enabled else "disable_game_mode"
        payload = {
            "system_command": cmd,
            "interval": interval
        }
        
        pub_socket.send_multipart([b"syscmd", json.dumps(payload).encode("utf-8")])
        return jsonify({"success": True, "enabled": enabled, "interval": interval})
    
    return jsonify({"status": "unknown"})

PROCESSES = get_processes(display_script="gui_display.py")

SERVICE_PORTS = SERVICE_BIND_PORTS

# 全局状态：存储所有进程和日志
processes = {}  # {name: {"process": subprocess.Popen, "log_queue": queue.Queue, "logs": list}}
process_lock = threading.Lock()

def log_output(process_name, pipe, log_queue, stream_type="STDOUT"):
    """在后台线程中读取进程输出并添加到日志队列"""
    try:
        # 使用 iter 逐行读取，直到管道关闭
        while True:
            line = pipe.readline()
            if not line:
                break
            # 不忽略空行，因为有些输出可能包含重要的空行
            timestamp = datetime.now().strftime("%H:%M:%S")
            # 清理可能的编码问题字符，确保可以正常显示
            try:
                cleaned_line = line.rstrip('\n\r')
            except UnicodeDecodeError:
                # 如果仍有编码问题，使用 errors='replace' 处理
                cleaned_line = line.encode('utf-8', errors='replace').decode('utf-8', errors='replace').rstrip('\n\r')
            
            # 如果是 stderr，添加标记
            if stream_type == "STDERR":
                log_entry = f"[{timestamp}] [STDERR] {cleaned_line}"
            else:
                log_entry = f"[{timestamp}] {cleaned_line}"
            
            log_queue.put(log_entry)
        pipe.close()
    except Exception as e:
        error_msg = str(e).encode('utf-8', errors='replace').decode('utf-8', errors='replace')
        log_queue.put(f"[{datetime.now().strftime('%H:%M:%S')}] [ERROR] 读取{stream_type}时出错: {error_msg}")
    finally:
        log_queue.put(f"[{datetime.now().strftime('%H:%M:%S')}] {stream_type} 输出流已关闭")

def start_process(config, cleanup_ports_first=False, ports_to_cleanup=None):
    """启动进程（后台运行，不显示窗口）"""
    name = config['name']
    
    with process_lock:
        # 先检查并停止已运行的服务实例（如果存在）
        if name in processes and processes[name].get('process'):
            proc = processes[name]['process']
            if proc.poll() is None:
                # 服务正在运行，先停止它
                print(f"  ⚠️  {name} 正在运行，先停止它...")
                try:
                    proc.terminate()
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                    print(f"  ✓ 已停止旧的 {name} 进程")
                except Exception as e:
                    print(f"  ⚠️  停止旧进程失败: {e}")
                # 清理进程记录
                processes.pop(name, None)
                # 等待一下让端口释放
                time.sleep(1.0)
        
        # 如果需要，先清理端口（清理可能被其他进程占用的端口）
        if cleanup_ports_first and ports_to_cleanup:
            # 只清理指定的端口（该服务使用的端口）
            cleanup_ports(ports_to_cleanup)
        
        try:
            if 'command' not in config and not os.path.exists(config['interpreter']):
                return {"success": False, "message": f"解释器不存在: {config['interpreter']}"}
            cmd = resolve_process_command(config)
            
            # 准备环境变量，确保使用 UTF-8 编码
            env = build_python_runtime_env(os.environ)
            
            # 使用 PIPE 捕获输出，不创建新窗口
            # 注意：在 Windows 上，需要设置 universal_newlines=True 和适当的编码
            # 使用行缓冲模式确保实时输出
            p = subprocess.Popen(
                cmd,
                cwd=config['cwd'],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,  # 分别捕获 stderr，确保所有输出都被捕获
                stdin=subprocess.DEVNULL,  # 不使用 stdin
                universal_newlines=True,  # 自动处理换行符
                bufsize=1,  # 行缓冲，确保实时输出
                encoding='utf-8',
                errors='replace',  # 替换无法解码的字符
                env=env,  # 使用修改后的环境变量
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
            )
            
            # 创建日志队列和列表
            log_queue = queue.Queue()
            logs = []
            
            # 启动两个后台线程分别读取 stdout 和 stderr
            stdout_thread = threading.Thread(target=log_output, args=(name, p.stdout, log_queue, "STDOUT"), daemon=True)
            stderr_thread = threading.Thread(target=log_output, args=(name, p.stderr, log_queue, "STDERR"), daemon=True)
            stdout_thread.start()
            stderr_thread.start()
            
            processes[name] = {
                "process": p,
                "log_queue": log_queue,
                "logs": logs,
                "start_time": datetime.now(),
                "config": config
            }
            
            return {"success": True, "message": f"{name} 已启动 (PID: {p.pid})", "pid": p.pid}
        except Exception as e:
            return {"success": False, "message": f"启动 {name} 失败: {e}"}

def stop_process(name):
    """停止进程"""
    with process_lock:
        if name not in processes:
            return {"success": False, "message": f"{name} 未运行"}
        
        proc_info = processes[name]
        proc = proc_info.get('process')
        
        if not proc or proc.poll() is not None:
            processes.pop(name, None)
            return {"success": False, "message": f"{name} 未运行"}
        
        try:
            # 终止进程
            proc.terminate()
            # 等待最多5秒
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                # 强制终止
                proc.kill()
                proc.wait()
            
            processes.pop(name, None)
            return {"success": True, "message": f"{name} 已停止"}
        except Exception as e:
            return {"success": False, "message": f"停止 {name} 失败: {e}"}

def get_process_logs(name, max_lines=1000):
    """获取进程日志"""
    with process_lock:
        if name not in processes:
            return []
        
        proc_info = processes[name]
        log_queue = proc_info['log_queue']
        logs = proc_info['logs']
        
        # 从队列中获取新日志（非阻塞）
        new_logs_count = 0
        try:
            while True:
                log_entry = log_queue.get_nowait()
                logs.append(log_entry)
                new_logs_count += 1
                # 限制日志数量，但保留更多历史记录
                if len(logs) > max_lines * 2:  # 保留更多日志以支持实时流
                    logs.pop(0)
        except queue.Empty:
            pass
        
        # 返回最后 max_lines 条（用于初始显示）
        return logs[-max_lines:] if len(logs) > max_lines else logs

def get_process_status(name):
    """获取进程状态"""
    with process_lock:
        if name not in processes:
            return {"running": False, "pid": None, "start_time": None}
        
        proc_info = processes[name]
        proc = proc_info.get('process')
        
        if not proc:
            return {"running": False, "pid": None, "start_time": None}
        
        is_running = proc.poll() is None
        start_time = proc_info.get('start_time')
        return {
            "running": is_running,
            "pid": proc.pid if is_running else None,
            "start_time": start_time.isoformat() if start_time else None
        }

# Flask 路由
@app.route('/')
def index():
    """主页面"""
    return render_template('module_manager.html')

@app.route('/api/services', methods=['GET'])
def get_services():
    """获取所有服务列表"""
    services = []
    for config in PROCESSES:
        name = config['name']
        status = get_process_status(name)
        services.append({
            "name": name,
            "running": status.get("running", False),
            "pid": status.get("pid"),
            "start_time": status.get("start_time")
        })
    return jsonify({"services": services})

@app.route('/api/services/<path:name>/start', methods=['POST'])
def start_service(name):
    """启动单个服务（只清理该服务使用的端口）"""
    config = next((p for p in PROCESSES if p['name'] == name), None)
    if not config:
        return jsonify({"success": False, "message": f"未找到服务: {name}"}), 404
    
    # 获取该服务使用的端口列表
    service_ports = SERVICE_PORTS.get(name, [])
    
    # 只清理该服务使用的端口
    result = start_process(config, cleanup_ports_first=bool(service_ports), ports_to_cleanup=service_ports)
    return jsonify(result)

@app.route('/api/services/<path:name>/stop', methods=['POST'])
def stop_service(name):
    """停止服务（使用 path 转换器以支持空格）"""
    result = stop_process(name)
    return jsonify(result)

@app.route('/api/services/<path:name>/logs', methods=['GET'])
def get_service_logs(name):
    """获取服务日志（使用 path 转换器以支持空格）"""
    max_lines = request.args.get('max_lines', 1000, type=int)
    logs = get_process_logs(name, max_lines)
    return jsonify({"logs": logs})

@app.route('/api/services/<path:name>/logs/stream', methods=['GET'])
def stream_service_logs(name):
    """实时流式推送服务日志（Server-Sent Events）"""
    def generate():
        # 【修复】从当前日志数量开始，而不是从 0 开始
        # 这样新连接不会推送所有历史日志，只推送连接建立后的新日志
        initial_logs = get_process_logs(name, max_lines=50000)
        last_log_count = len(initial_logs)  # 从当前日志数量开始
        
        while True:
            try:
                logs = get_process_logs(name, max_lines=50000)  # 获取所有可用日志
                current_count = len(logs)
                
                # 只发送真正的新日志（从 last_log_count 开始）
                if current_count > last_log_count:
                    new_logs = logs[last_log_count:]
                    for log in new_logs:
                        # SSE 格式：data: <内容>\n\n
                        yield f"data: {json.dumps({'log': log}, ensure_ascii=False)}\n\n"
                    last_log_count = current_count
                else:
                    # 发送心跳保持连接
                    yield f"data: {json.dumps({'heartbeat': True}, ensure_ascii=False)}\n\n"
                
                time.sleep(0.05)  # 50ms 检查一次，更快的响应
            except Exception as e:
                yield f"data: {json.dumps({'error': str(e)}, ensure_ascii=False)}\n\n"
                break
    
    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            'Connection': 'keep-alive'
        }
    )

@app.route('/api/services/<path:name>/status', methods=['GET'])
def get_service_status(name):
    """获取服务状态（使用 path 转换器以支持空格）"""
    status = get_process_status(name)
    return jsonify(status)


def _read_presence():
    """读取用户上下线状态，默认在线。"""
    try:
        if PRESENCE_STATE_PATH.exists():
            with open(PRESENCE_STATE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            return bool(data.get("user_online", True))
    except (json.JSONDecodeError, OSError):
        pass
    return True


def _write_presence(online):
    """写入用户上下线状态（原子写入）。"""
    try:
        tmp = PRESENCE_STATE_PATH.with_suffix(PRESENCE_STATE_PATH.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"user_online": bool(online)}, f, ensure_ascii=False)
        os.replace(tmp, PRESENCE_STATE_PATH)
        return True
    except OSError:
        return False


@app.route('/api/presence', methods=['GET', 'POST'])
def api_presence():
    """GET: 获取当前用户上下线状态。POST: 切换状态，body: {"online": true|false}"""
    if request.method == 'GET':
        return jsonify({"user_online": _read_presence()})
    try:
        data = request.get_json(force=True, silent=True) or {}
        online = data.get("online")
        if online is None:
            return jsonify({"success": False, "message": "缺少 online 字段"}), 400
        if _write_presence(online):
            # 发送状态变更通知给 Thinking 核心
            # 注意：虽然文件已更新，但主动通知可以让 Thinking 立即感知（打印日志或调整行为）
            cmd_payload = {
                "system_command": "update_presence",
                "user_online": bool(online)
            }
            pub_socket.send_multipart([b"syscmd", json.dumps(cmd_payload).encode("utf-8")])
            
            return jsonify({"success": True, "user_online": bool(online)})
        return jsonify({"success": False, "message": "写入失败"}), 500
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/api/services/start-all', methods=['POST'])
def start_all_services():
    """启动所有服务（清理所有需要的端口）"""
    results = []
    # 先清理所有端口（启动所有服务时需要清理所有端口）
    cleanup_ports(list(ZMQ_PORTS.values()))
    
    for config in PROCESSES:
        # 启动服务时不清理端口（因为已经在上面统一清理了）
        result = start_process(config, cleanup_ports_first=False)
        results.append({"name": config['name'], **result})
        time.sleep(1)  # 延迟启动
    
    return jsonify({"results": results})

@app.route('/api/services/stop-all', methods=['POST'])
def stop_all_services():
    """停止所有服务"""
    results = []
    with process_lock:
        names = list(processes.keys())
    
    for name in names:
        result = stop_process(name)
        results.append({"name": name, **result})
    
    return jsonify({"results": results})

# ---------- 工具模型可调用工具配置（单一 schema 文件：tools[].name/description/enabled）----------
TOOL_AGENT_SCHEMA_PATH = os.path.join(ROOT_DIR, "server", "tool_agent_schema.json")


@app.route('/api/tool_agent_tools', methods=['GET'])
def get_tool_agent_tools():
    """从 server/tool_agent_schema.json 读取工具列表（每项含 name/description/enabled），不依赖 Thinking 启动。"""
    all_tools = []
    if os.path.exists(TOOL_AGENT_SCHEMA_PATH):
        try:
            with open(TOOL_AGENT_SCHEMA_PATH, 'r', encoding='utf-8') as f:
                data = json.load(f)
            all_tools = data.get("tools") or []
            all_tools = sorted(all_tools, key=lambda t: (t.get("name") or ""))
        except Exception as e:
            return jsonify({"success": False, "message": str(e), "all_tools": []}), 500
    return jsonify({"all_tools": all_tools})


@app.route('/api/tool_agent_tools', methods=['POST'])
def save_tool_agent_tools():
    """根据请求体 enabled 名单更新 schema 中每项工具的 enabled，写回 server/tool_agent_schema.json。"""
    try:
        body = request.get_json() or {}
        enabled = body.get("enabled")
        if not isinstance(enabled, list):
            return jsonify({"success": False, "message": "请提供 enabled 数组"}), 400
        enabled_set = set(str(x) for x in enabled)
        if not os.path.exists(TOOL_AGENT_SCHEMA_PATH):
            return jsonify({"success": False, "message": "schema 文件不存在"}), 400
        with open(TOOL_AGENT_SCHEMA_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        tools = data.get("tools") or []
        for t in tools:
            t["enabled"] = t.get("name") in enabled_set
        data["tools"] = sorted(tools, key=lambda t: (t.get("name") or ""))
        os.makedirs(os.path.dirname(TOOL_AGENT_SCHEMA_PATH), exist_ok=True)
        with open(TOOL_AGENT_SCHEMA_PATH, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return jsonify({"success": True, "message": "已保存，下次工具模型被调用时生效"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


# ---------- 实时屏幕分析（结果作为系统消息注入主模型）----------
# 启动时机：① 其他模块启动完成后，Thinking 收到启动信号时根据保存的 enabled 调用 ensure 接口决定是否启动；
#           ② 运行过程中用户在管理页保存配置时，根据本次保存的 enabled 立即启动或停止。
REALTIME_SCREEN_CONFIG_PATH = os.path.join(ROOT_DIR, "server", "realtime_screen_config.json")
REALTIME_SCREEN_PID_PATH = os.path.join(ROOT_DIR, "server", "realtime_screen_pid.txt")
REALTIME_SCREEN_SCRIPT = os.path.join(ROOT_DIR, "realtime_screen_analysis_standalone.py")
REALTIME_SCREEN_LOG_PATH = os.path.join(ROOT_DIR, "server", "realtime_screen_analysis.log")


def _realtime_screen_is_running():
    """根据 PID 文件判断实时屏幕分析脚本是否在运行。"""
    if not os.path.exists(REALTIME_SCREEN_PID_PATH):
        return False
    try:
        with open(REALTIME_SCREEN_PID_PATH, 'r', encoding='utf-8') as f:
            pid = int(f.read().strip())
    except (ValueError, OSError):
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _realtime_screen_stop():
    """结束实时屏幕分析脚本进程（读 PID 文件后发 SIGTERM）。"""
    if not os.path.exists(REALTIME_SCREEN_PID_PATH):
        return True
    try:
        with open(REALTIME_SCREEN_PID_PATH, 'r', encoding='utf-8') as f:
            pid = int(f.read().strip())
    except (ValueError, OSError):
        try:
            os.remove(REALTIME_SCREEN_PID_PATH)
        except Exception:
            pass
        return True
    try:
        import signal
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass
    try:
        for _ in range(20):
            time.sleep(0.25)
            try:
                os.kill(pid, 0)
            except OSError:
                break
        if os.path.exists(REALTIME_SCREEN_PID_PATH):
            os.remove(REALTIME_SCREEN_PID_PATH)
    except Exception:
        pass
    return True


def _update_tool_enabled_in_schema(tool_name, enabled):
    """助手函数：更新 tool_agent_schema.json 中指定工具的 enabled 状态"""
    try:
        if not os.path.exists(TOOL_AGENT_SCHEMA_PATH):
            return
        with open(TOOL_AGENT_SCHEMA_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        tools = data.get("tools") or []
        changed = False
        for t in tools:
            if t.get("name") == tool_name:
                if t.get("enabled") != enabled:
                    t["enabled"] = enabled
                    changed = True
                break
        if changed:
            with open(TOOL_AGENT_SCHEMA_PATH, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _realtime_screen_start():
    """在项目根目录启动 realtime_screen_analysis_standalone.py。优先使用 thinking_venv 的 Python（含 pyautogui/PIL 等依赖）。"""
    if not os.path.isfile(REALTIME_SCREEN_SCRIPT):
        return False, "未找到脚本 realtime_screen_analysis_standalone.py"
    if _realtime_screen_is_running():
        return True, "脚本已在运行"
    python_exe = sys.executable
    if sys.platform.startswith("win"):
        thinking_python = os.path.join(ROOT_DIR, "thingking", "thinking_venv", "Scripts", "python.exe")
    else:
        thinking_python = os.path.join(ROOT_DIR, "thingking", "thinking_venv", "bin", "python")
    if os.path.isfile(thinking_python):
        python_exe = thinking_python
    try:
        creationflags = 0
        if sys.platform.startswith('win'):
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(
            [python_exe, REALTIME_SCREEN_SCRIPT],
            cwd=ROOT_DIR,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        time.sleep(1.5)
        if _realtime_screen_is_running():
            return True, "已启动实时屏幕分析脚本"
        return False, "脚本未运行，请查看下方「最近日志」排查（如依赖缺失、DASHSCOPE_API_KEY 未设置等）"
    except Exception as e:
        return False, str(e)


def _realtime_screen_ensure_from_config():
    """根据保存的配置决定是否启动实时屏幕分析（仅当 enabled 且未在运行时启动）。供其他模块就绪后调用。"""
    data = load_realtime_screen_config(REALTIME_SCREEN_CONFIG_PATH)

    # 【新增】无论是否启动，都同步工具 schema 状态
    # 实时分析启用 -> 禁用 get_screen_info; 实时分析禁用 -> 启用 get_screen_info
    realtime_enabled = data.get("enabled", False)
    _update_tool_enabled_in_schema("get_screen_info", not realtime_enabled)

    if not realtime_enabled:
        return False, "配置中未启用，不启动"
    if _realtime_screen_is_running():
        return True, "已在运行"
    ok, msg = _realtime_screen_start()
    return ok, msg


@app.route('/api/realtime_screen_ensure_from_config', methods=['GET'])
def api_realtime_screen_ensure_from_config():
    """根据保存的配置决定是否启动实时屏幕分析。在其他模块启动完成后由 Thinking 调用。"""
    try:
        ok, msg = _realtime_screen_ensure_from_config()
        return jsonify({"success": ok, "started": ok, "message": msg})
    except Exception as e:
        return jsonify({"success": False, "started": False, "message": str(e)}), 500


@app.route('/api/realtime_screen_config', methods=['GET'])
def get_realtime_screen_config():
    """获取实时屏幕分析开关、间隔、变化阈值及脚本是否在运行。"""
    data = load_realtime_screen_config(REALTIME_SCREEN_CONFIG_PATH)
    data["script_running"] = _realtime_screen_is_running()
    return jsonify(data)


@app.route('/api/realtime_screen_log', methods=['GET'])
def get_realtime_screen_log():
    """返回实时屏幕分析脚本的最近日志（最后 80 行），便于排查未启动原因。"""
    lines = []
    if os.path.exists(REALTIME_SCREEN_LOG_PATH):
        try:
            with open(REALTIME_SCREEN_LOG_PATH, 'r', encoding='utf-8', errors='replace') as f:
                lines = f.readlines()
        except Exception:
            pass
    tail = lines[-80:] if len(lines) > 80 else lines
    return jsonify({"log_lines": tail, "total_lines": len(lines)})


@app.route('/api/realtime_screen_config', methods=['POST'])
def save_realtime_screen_config():
    """保存配置；运行过程中根据本次保存的 enabled 状态启动或停止实时屏幕分析脚本。"""
    try:
        body = request.get_json() or {}
        merged = dict(DEFAULT_REALTIME_SCREEN_CONFIG)
        merged.update(body)
        config = normalize_realtime_screen_config(merged)
        persist_realtime_screen_config(REALTIME_SCREEN_CONFIG_PATH, config)
        enabled = config.get("enabled", False)

        # 【新增】状态切换时同步工具 schema
        _update_tool_enabled_in_schema("get_screen_info", not bool(enabled))

        if enabled:
            ok, msg = _realtime_screen_start()
            if not ok:
                return jsonify({"success": False, "message": msg}), 500
            return jsonify({"success": True, "message": msg})
        else:
            _realtime_screen_stop()
            return jsonify({"success": True, "message": "已关闭并停止实时屏幕分析脚本"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


if __name__ == '__main__':
    # 设置环境变量
    os.environ["NO_PROXY"] = "localhost,127.0.0.1"
    
    print("=" * 60)
    print("模块管理服务器启动中...")
    print("=" * 60)
    print("访问 http://localhost:5000 查看管理界面")
    print("=" * 60)
    print()
    
    # 使用 use_reloader=False 和 use_debugger=False 来减少输出信息
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True, use_reloader=False)
