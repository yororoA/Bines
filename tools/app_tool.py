import subprocess
import os
import sys

def open_application(app_name_or_path, arguments=None):
    """
    打开应用程序或文件（不含浏览器；浏览器相关请使用 browser_search）。
    
    Args:
        app_name_or_path (str): 应用程序名称（如 "notepad", "calc", "vscode"）或完整路径
        arguments (str, optional): 传递给应用程序的参数（例如文件路径）
    
    Returns:
        str: 操作结果描述。若传入 chrome/browser/edge 等会提示使用 browser_search。
    """
    try:
        # Windows 系统
        if sys.platform.startswith('win'):
            # 如果提供了参数，需要一起传递
            if arguments:
                # 尝试作为完整命令执行
                if os.path.exists(app_name_or_path):
                    # 如果是文件路径，使用 startfile 或 subprocess
                    if app_name_or_path.endswith(('.exe', '.bat', '.cmd')):
                        # 可执行文件，使用 subprocess
                        cmd = [app_name_or_path]
                        if arguments:
                            cmd.extend(arguments.split() if isinstance(arguments, str) else arguments)
                        subprocess.Popen(cmd, shell=False)
                        return f"已启动应用程序: {app_name_or_path}"
                    else:
                        # 其他文件，尝试用关联程序打开
                        os.startfile(app_name_or_path)
                        return f"已打开文件: {app_name_or_path}"
                else:
                    # 可能是应用名称，尝试用 start 命令
                    if arguments:
                        cmd = f'start "" "{app_name_or_path}" {arguments}'
                    else:
                        cmd = f'start "" "{app_name_or_path}"'
                    os.system(cmd)
                    return f"已尝试启动: {app_name_or_path}"
            else:
                # 没有参数的情况
                if os.path.exists(app_name_or_path):
                    # 文件存在，直接打开
                    if app_name_or_path.endswith(('.exe', '.bat', '.cmd', '.msi')):
                        # 可执行文件
                        subprocess.Popen([app_name_or_path], shell=False)
                        return f"已启动应用程序: {app_name_or_path}"
                    else:
                        # 其他文件，用关联程序打开
                        os.startfile(app_name_or_path)
                        return f"已打开文件: {app_name_or_path}"
                else:
                    # 可能是应用名称，尝试多种方式
                    # 1. 尝试直接执行（系统 PATH 中的程序）
                    try:
                        subprocess.Popen([app_name_or_path], shell=False)
                        return f"已启动应用程序: {app_name_or_path}"
                    except FileNotFoundError:
                        pass
                    
                    # 2. 尝试使用 start 命令
                    try:
                        os.system(f'start "" "{app_name_or_path}"')
                        return f"已尝试启动: {app_name_or_path}"
                    except Exception:
                        pass
                    
                    # 3. 尝试查找常见应用路径（不包含浏览器：浏览器仅通过 browser_search 启动并搜索）
                    username = os.getenv('USERNAME', '')
                    common_apps = {
                        'notepad': 'notepad.exe',
                        'calc': 'calc.exe',
                        'mspaint': 'mspaint.exe',
                        'cmd': 'cmd.exe',
                        'powershell': 'powershell.exe',
                        'explorer': 'explorer.exe',
                    }
                    
                    # VS Code 路径（尝试多个可能的位置）
                    if username:
                        vscode_paths = [
                            rf'C:\Users\{username}\AppData\Local\Programs\Microsoft VS Code\Code.exe',
                            r'C:\Program Files\Microsoft VS Code\Code.exe',
                            r'C:\Program Files (x86)\Microsoft VS Code\Code.exe',
                        ]
                        for vscode_path in vscode_paths:
                            if os.path.exists(vscode_path):
                                common_apps['vscode'] = vscode_path
                                common_apps['code'] = vscode_path
                                break
                    
                    app_lower = app_name_or_path.lower().strip()
                    # 浏览器相关一律引导使用 browser_search，不在此处启动
                    if app_lower in ('chrome', 'browser', 'edge', 'msedge', 'firefox', '浏览器'):
                        return "与浏览器相关的操作请使用 browser_search 工具：该工具会启动浏览器并执行搜索，或直接使用 browser_search 进行网页搜索。请勿使用 open_application 打开浏览器。"
                    if app_lower in common_apps:
                        app_path = common_apps[app_lower]
                        if app_lower in ['notepad', 'calc', 'mspaint', 'cmd', 'powershell', 'explorer']:
                            # 系统应用，直接执行（这些应用名可以直接作为命令）
                            subprocess.Popen([app_path], shell=False)
                            return f"已启动: {app_name_or_path}"
                        elif os.path.exists(app_path):
                            # 第三方应用，检查路径是否存在
                            subprocess.Popen([app_path], shell=False)
                            return f"已启动: {app_name_or_path}"
                        else:
                            return f"未找到应用程序: {app_name_or_path}（常见路径不存在，请提供完整路径）"
                    
                    # 4. 尝试使用 where 命令查找
                    try:
                        result = subprocess.run(['where', app_name_or_path], 
                                               capture_output=True, text=True, timeout=2)
                        if result.returncode == 0 and result.stdout.strip():
                            app_path = result.stdout.strip().split('\n')[0]
                            subprocess.Popen([app_path], shell=False)
                            return f"已启动: {app_name_or_path} (路径: {app_path})"
                    except (subprocess.TimeoutExpired, FileNotFoundError, Exception):
                        pass
                    
                    return f"无法找到或启动应用程序: {app_name_or_path}。请提供完整路径或确保应用程序在系统 PATH 中。"
        
        # Linux/Mac 系统（备用实现）
        else:
            if arguments:
                subprocess.Popen([app_name_or_path] + (arguments.split() if isinstance(arguments, str) else arguments))
            else:
                subprocess.Popen([app_name_or_path])
            return f"已启动应用程序: {app_name_or_path}"
    
    except Exception as e:
        return f"打开应用程序时出错: {str(e)}"


if __name__ == "__main__":
    # 测试
    print(open_application("notepad"))
    print(open_application("calc"))
    # print(open_application("chrome"))
