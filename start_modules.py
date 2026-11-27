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

    clone = getattr(options, "mode", "normal") == "clone"
    target = "clone" if clone else options.profile
    os.environ["BINES_DOCKER_MODE"] = "1"
    # The compose network owns NapCat; host services must not start another container.
    os.environ["BINES_NAPCAT_DEPLOY"] = "external"
    os.environ["_BINES_NAPCAT_DEPLOY"] = "external"
    already_running = None
    try:
        print("正在检查 Docker 引擎…", flush=True)
        docker_launcher.ensure_engine()
        already_running = docker_launcher.running_services("clone" if clone else "full")
        mode_arg = " --mode clone" if clone else ""
        print(f"QQ 登录/扫码状态：另开终端运行 python napcat.py logs{mode_arg}。", flush=True)
        print(f"python napcat.py webui{mode_arg} 打开 NapCat 管理页；管理页不显示 QQ 登录二维码。", flush=True)
        print("正在启动 Docker 链路…", flush=True)
        docker_launcher.up(target)
        docker_launcher.wait_ready(target)
        print("Docker 服务已就绪：" + ", ".join(docker_launcher.REQUIRED_SERVICES[target]), flush=True)
        if clone:
            print("分身记忆管理：http://127.0.0.1:15001", flush=True)
        print("QQ 连接/扫码情况请查看 NapCat 日志，服务就绪不代表 QQ 已登录。", flush=True)
        if options.profile == "chatbot":
            print("接下来跟随服务日志；NapCat 输出的登录二维码会显示在这里。", flush=True)
            print("Ctrl+C 删除本次启动的容器及其容器日志，保留记忆、当前镜像和已保存的启动诊断。", flush=True)
            if already_running:
                print("启动前已运行的服务将保留：" + ", ".join(sorted(already_running)), flush=True)
            try:
                docker_launcher.follow(target)
            except KeyboardInterrupt:
                pass
            return 0
        return _run_host(options)
    except KeyboardInterrupt:
        print("启动已取消。", flush=True)
        return 130
    except Exception as exc:
        print(docker_launcher.startup_failure_report(target, exc,
                  inspect_services=already_running is not None), file=sys.stderr, flush=True)
        return 1
    finally:
        if already_running is not None:
            print("正在停止并清理本次 Docker 链路…", flush=True)
            try:
                if clone:
                    docker_launcher.down(previously_running=already_running, target=target)
                else:
                    docker_launcher.down(previously_running=already_running)
            except Exception as exc:
                print(docker_launcher.format_failure(exc, "Docker 清理失败"), file=sys.stderr, flush=True)
                return 1


def main():
    import signal
    def stop_on_signal(_signum, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop_on_signal)
    parser = argparse.ArgumentParser(description="启动 Bines，自动处理依赖、就绪与退出")
    profiles = parser.add_mutually_exclusive_group()
    profiles.add_argument("--profile", choices=["full", "chatbot"],
                          default=os.environ.get("BINES_PROFILE", "full"))
    profiles.add_argument("--chatbot", dest="profile", action="store_const", const="chatbot",
                          help="等同于 --profile chatbot")
    parser.add_argument("--mode", choices=["normal", "clone"], default="normal",
                        help="clone 启动独立的 ChatBot 分身（需要 Docker）")
    parser.add_argument("--web", action="store_true", help="启动服务并打开 Web 管理器")
    parser.add_argument("--console", action="store_true", help="终端模式，退出时停止本次启动的服务")
    parser.add_argument("--manager-only", action="store_true", help="只打开管理器，按需启动服务")
    parser.add_argument("--external-napcat", action="store_true", help="连接现有 NapCat，不管理 Docker")
    parser.add_argument("--host", action="store_true", help="强制使用本机 venv，不启动 Docker 链路")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--docker-down", action="store_true",
                         help="停止并删除 Bines 容器，保留记忆、当前镜像和构建缓存")
    actions.add_argument("--reset-memory", action="store_true",
                         help="清空已停止的 ChatBot 实例的记忆和状态，保留人格（需要 Docker）")
    options = parser.parse_args()
    if options.mode == "clone":
        if options.profile != "chatbot":
            parser.error("--mode clone 仅支持 --profile chatbot（或 --chatbot）")
        if options.host or options.external_napcat or options.web or options.manager_only:
            parser.error("--mode clone 使用单独的 Docker 链路和 NapCat，不能与 --host、--external-napcat、--web 或 --manager-only 一起使用")
    if options.reset_memory and (options.profile != "chatbot" or options.host or options.external_napcat
                                 or options.web or options.manager_only):
        parser.error("--reset-memory 仅支持 Docker ChatBot，请使用 --chatbot，分身另加 --mode clone")
    os.environ["BINES_PROFILE"] = options.profile
    os.environ["_BINES_LAUNCH_PROFILE"] = options.profile
    if options.external_napcat:
        os.environ["BINES_NAPCAT_DEPLOY"] = "external"
        os.environ["_BINES_NAPCAT_DEPLOY"] = "external"
    import docker_launcher
    target = "clone" if options.mode == "clone" else options.profile
    if (options.mode == "clone" or options.reset_memory) and not docker_launcher.available():
        print("该操作需要 Docker Compose，请安装 Docker 后重试。", file=sys.stderr, flush=True)
        return 1
    if options.reset_memory:
        return _reset_memory(target, docker_launcher)
    if options.docker_down:
        try:
            docker_launcher.ensure_engine()
            if options.mode == "clone":
                docker_launcher.down(target="clone")
                print("ChatBot 分身容器已清理，分身记忆与登录数据已保留。", flush=True)
            else:
                docker_launcher.down()
                print("Bines 容器与容器日志已清理，构建器已停止；记忆、当前镜像和构建缓存已保留。", flush=True)
            return 0
        except Exception as exc:
            print(f"Docker 清理失败：{exc}", file=sys.stderr, flush=True)
            return 1
    try:
        with docker_launcher.instance_lock(target):
            docker_launcher.check_reset_complete(target)
            return _launch(options, docker_launcher)
    except (docker_launcher.DockerError, OSError) as exc:
        print(f"启动失败：{exc}", file=sys.stderr, flush=True)
        return 1


def _reset_memory(target, docker_launcher):
    project = docker_launcher.instance_project(target)
    label = "分身" if target == "clone" else "主实例"
    try:
        typed = input(f"将永久清空 {label} 的对话、记忆和状态。确认请输入 {project}：")
    except (EOFError, KeyboardInterrupt):
        typed = ""
    if typed.strip() != project:
        print("已取消重置。", flush=True)
        return 1
    try:
        with docker_launcher.instance_lock(target):
            docker_launcher.reset_memory(target)
    except KeyboardInterrupt:
        print("重置已中断。如启动时提示未完成，请重新运行同一重置命令。", file=sys.stderr, flush=True)
        return 130
    except Exception as exc:
        print(f"重置未完成：{exc}", file=sys.stderr, flush=True)
        return 1
    print(f"{label} 的记忆和状态已重置。下次启动从现有人格配置和初始状态开始。", flush=True)
    return 0


def _launch(options, docker_launcher):
    if options.mode == "clone":
        print("Bines · Docker · ChatBot 分身", flush=True)
        return _run_docker(options)
    from website_runtime import WebsiteBridgeSupervisor
    website_bridge = WebsiteBridgeSupervisor()
    try:
        try:
            if website_bridge.start():
                print("网站浏览器 · 已启用自动连接恢复", flush=True)
        except Exception as exc:
            print(f"网站浏览器不可用：{exc}", file=sys.stderr, flush=True)
        if not options.host and not options.external_napcat and docker_launcher.available():
            print(f"Bines · Docker · {options.profile}", flush=True)
            return _run_docker(options)
        if not os.getenv("BINES_AUTONOMY_BACKEND"):
            backend = "redis" if os.getenv("BINES_AUTONOMY_REDIS_URL") else "sqlite"
            os.environ["BINES_AUTONOMY_BACKEND"] = backend
        print(f"Bines · 宿主机 · {options.profile}", flush=True)
        return _run_host(options)
    finally:
        website_bridge.close()


if __name__ == "__main__":
    raise SystemExit(main())
