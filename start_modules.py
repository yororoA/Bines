"""Single Bines entry point; CLI and Web use the same readiness supervisor."""
import argparse
import os
import sys
import time

if __name__ == "__main__":
    from runtime_environments import enter_launcher
    enter_launcher()

from config import HTTP_HOST, HTTP_PORTS
from process_registry import get_processes
from service_supervisor import ServiceSupervisor


def _run_host(options):
    use_web = options.web or options.manager_only or (options.profile == "full" and not options.console)
    if use_web:
        from process_runtime import find_port_listeners
        port = HTTP_PORTS["MODULE_MANAGER"]
        owner = find_port_listeners([port]).get(port)
        if owner:
            print(f"管理器已在端口 {port} 运行，请访问 http://{HTTP_HOST}:{port}", flush=True)
            return 1
        from server.module_manager import app, supervisor
        from werkzeug.serving import make_server
        import threading
        import webbrowser
        server = make_server(HTTP_HOST, port, app, threaded=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        print(f"管理页：http://{HTTP_HOST}:{port}", flush=True)
        if not options.manager_only:
            supervisor.start_background()
        webbrowser.open(f"http://{HTTP_HOST}:{port}")
        try:
            while thread.is_alive():
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            supervisor.cancel.set()
            supervisor.close()
            server.shutdown()
            server.server_close()
        return 0

    def progress(name, state, message):
        print(f"{name} · {message}", flush=True)
    supervisor = ServiceSupervisor(get_processes(display_script="display.py", profile=options.profile),
                                   emit=progress)
    result_code = 0
    try:
        supervisor.start_background()
        while supervisor.startup_status()["active"]:
            time.sleep(0.2)
        results = supervisor.startup_status()["results"]
        if any(not result["success"] and not supervisor.definitions.get(result["name"], {}).get("optional") for result in results):
            return 1
        print("Bines 已就绪。Ctrl+C 停止本次启动的服务。", flush=True)
        if "ChatBot" in supervisor.definitions:
            print(supervisor.status("ChatBot")["message"], flush=True)
        while True:
            failed = [name for name, row in supervisor.definitions.items()
                      if row.get("autostart", True) and not row.get("optional")
                      and supervisor.status(name)["state"] == "failed"]
            if failed:
                print("服务异常退出：" + ", ".join(failed), flush=True)
                result_code = 1
                break
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        supervisor.cancel.set()
        supervisor.close()
    return result_code


def _run_docker(options):
    """Start the QQ/memory chain through compose, then host desktop services in full mode."""
    import docker_launcher

    os.environ["BINES_DOCKER_MODE"] = "1"
    # The compose network owns NapCat; host services must not start another container.
    os.environ["BINES_NAPCAT_DEPLOY"] = "external"
    os.environ["_BINES_NAPCAT_DEPLOY"] = "external"
    already_running = None
    try:
        print("正在检查 Docker 引擎…", flush=True)
        docker_launcher.ensure_engine()
        already_running = docker_launcher.running_services("full")
        print("正在启动 Docker 链路…", flush=True)
        docker_launcher.up(options.profile)
        docker_launcher.wait_ready(options.profile)
        print("Docker 链路已就绪：" + ", ".join(docker_launcher.REQUIRED_SERVICES[options.profile]), flush=True)
        if options.profile == "chatbot":
            print("NapCat 二维码：python napcat.py logs", flush=True)
            print("Ctrl+C 删除本次启动的容器和日志，保留记忆与当前镜像。", flush=True)
            if already_running:
                print("启动前已运行的服务将保留：" + ", ".join(sorted(already_running)), flush=True)
            try:
                docker_launcher.follow(options.profile)
            except KeyboardInterrupt:
                pass
            return 0
        return _run_host(options)
    except KeyboardInterrupt:
        print("启动已取消。", flush=True)
        return 130
    except Exception as exc:
        print(f"Docker 启动失败：{exc}", file=sys.stderr, flush=True)
        return 1
    finally:
        if already_running is not None:
            print("正在停止并清理本次 Docker 链路…", flush=True)
            try:
                docker_launcher.down(previously_running=already_running)
            except Exception as exc:
                print(f"Docker 清理失败：{exc}", file=sys.stderr, flush=True)
                return 1


def main():
    import signal
    def stop_on_signal(_signum, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop_on_signal)
    parser = argparse.ArgumentParser(description="启动 Bines，自动处理依赖、就绪与退出")
    parser.add_argument("--profile", choices=["full", "chatbot"],
                        default=os.environ.get("BINES_PROFILE", "full"))
    parser.add_argument("--web", action="store_true", help="启动服务并打开 Web 管理器")
    parser.add_argument("--console", action="store_true", help="终端模式，退出时停止本次启动的服务")
    parser.add_argument("--manager-only", action="store_true", help="只打开管理器，按需启动服务")
    parser.add_argument("--external-napcat", action="store_true", help="连接现有 NapCat，不管理 Docker")
    parser.add_argument("--host", action="store_true", help="强制使用本机 venv，不启动 Docker 链路")
    parser.add_argument("--docker-down", action="store_true",
                        help="停止并删除 Bines 容器，清理旧镜像与专用构建缓存，保留记忆和当前镜像")
    options = parser.parse_args()
    os.environ["BINES_PROFILE"] = options.profile
    os.environ["_BINES_LAUNCH_PROFILE"] = options.profile
    if options.external_napcat:
        os.environ["BINES_NAPCAT_DEPLOY"] = "external"
        os.environ["_BINES_NAPCAT_DEPLOY"] = "external"
    import docker_launcher
    if options.docker_down:
        try:
            docker_launcher.ensure_engine()
            docker_launcher.down()
            print("Bines 容器、日志和可回收缓存已清理，记忆与当前镜像已保留。", flush=True)
            return 0
        except Exception as exc:
            print(f"Docker 清理失败：{exc}", file=sys.stderr, flush=True)
            return 1
    from website_runtime import WebsiteBridgeProcess
    website_bridge = WebsiteBridgeProcess()
    try:
        try:
            if website_bridge.start():
                print("网站浏览器 · 已就绪", flush=True)
        except Exception as exc:
            print(f"网站浏览器不可用：{exc}", file=sys.stderr, flush=True)
        if not options.host and not options.external_napcat and docker_launcher.available():
            print(f"Bines · Docker · {options.profile}", flush=True)
            return _run_docker(options)
        print(f"Bines · 宿主机 · {options.profile}", flush=True)
        return _run_host(options)
    finally:
        website_bridge.close()


if __name__ == "__main__":
    raise SystemExit(main())
