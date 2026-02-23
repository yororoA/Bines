"""
浏览器工具：Selenium 打开搜索页后，使用屏幕分析或 DOM 获取结果页内容。
使用独立 Chrome 配置目录，与您日常使用的 Chrome 分离，避免「开了其他浏览器/Chrome」导致只打开窗口、后续操作失败。
"""
import time
import os
from pathlib import Path
from urllib.parse import quote

# 屏幕分析用于搜索结果的提示：快速提取搜索结果页主要内容
_SEARCH_FOCUS = "简要描述搜索结果页面上的主要内容，包括搜索标题、搜索结果条目，不要描述页面框架、搜索链接等无关内容"

# 项目根目录，用于默认配置路径
_TOOLS_ROOT = Path(__file__).resolve().parents[1]
_PROJECT_ROOT = _TOOLS_ROOT.parent


def _get_chrome_user_data_dir():
    """浏览器自动化专用配置目录（与日常 Chrome 分离，不杀进程也可运行）。"""
    try:
        from config import BROWSER_AUTOMATION_USER_DATA_DIR
        return os.path.abspath(BROWSER_AUTOMATION_USER_DATA_DIR)
    except Exception:
        return str(_PROJECT_ROOT / "static" / "Bines Data Automation")


def _kill_chrome_processes():
    """仅在启动失败时可选调用：强制关闭所有 Chrome 进程（会关闭您正在使用的 Chrome）。"""
    try:
        if os.name == "nt":
            os.system("taskkill /f /im chrome.exe >nul 2>&1")
        time.sleep(1)
    except Exception:
        pass


def _get_search_result_via_screen():
    """通过屏幕分析（截图 + 视觉 API）快速获取当前屏幕上的搜索结果内容。"""
    try:
        from .screen_tool import get_screen_info
        result = get_screen_info(
            simple_recognition=True,
            fast_mode=True,
            focus_description=_SEARCH_FOCUS,
        )
        if result and "Error" not in result:
            return result.strip()[:1200]
        return ""
    except Exception as e:
        return f"（屏幕分析失败: {e}）"


def _get_search_result_content_via_selenium(driver):
    """
    通过 Selenium 从当前页面提取搜索结果文本（DOM 文本），作为屏幕分析不可用时的回退。
    """
    try:
        from selenium.webdriver.common.by import By
    except ImportError:
        return "（无法导入 Selenium By，跳过页面内容提取）"
    lines = []
    try:
        result_divs = driver.find_elements(By.CSS_SELECTOR, "div.g")
        if result_divs:
            for i, div in enumerate(result_divs[:10], 1):
                try:
                    text = div.text.strip()
                    if text:
                        lines.append(f"[结果{i}] {text[:500]}")
                except Exception:
                    continue
        if not lines:
            for sel in ("main", "article", "#search", "body"):
                try:
                    el = driver.find_element(By.CSS_SELECTOR, sel)
                    if el and el.text and len(el.text.strip()) > 50:
                        lines.append(el.text.strip()[:2000])
                        break
                except Exception:
                    continue
    except Exception as e:
        lines.append(f"（提取页面文本时出错: {e}）")
    return "\n".join(lines) if lines else ""


def browser_search(query: str) -> str:
    """
    使用 Selenium (undetected-chromedriver) 打开搜索页，然后通过屏幕分析快速获取结果页内容。
    当用户要求「用浏览器搜索」「在网上搜一下」等时，应使用此工具。

    Args:
        query: 搜索关键词

    Returns:
        操作结果与搜索结果摘要文本
    """
    if not (query or str(query).strip()):
        return "Error: 搜索内容不能为空"
    query = str(query).strip()
    
    try:
        import undetected_chromedriver as uc
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.common.by import By
    except ImportError as e:
        return f"Error: 依赖缺失: {e}. 请确保安装了 undetected-chromedriver 和 selenium"

    user_data_dir = _get_chrome_user_data_dir()
    os.makedirs(user_data_dir, exist_ok=True)

    driver = None
    try:
        options = uc.ChromeOptions()
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1280,800")
        options.add_argument("--user-data-dir=" + user_data_dir)
        options.add_argument("--profile-directory=Default")

        driver = uc.Chrome(options=options, use_subprocess=True)

        search_url = "https://www.google.com/search?q=" + quote(query)
        driver.get(search_url)

        wait = WebDriverWait(driver, 20)
        try:
            wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, "div.g")))
        except Exception:
            pass
        time.sleep(2.5)

        content = _get_search_result_via_screen()
        if not content or "Error" in content or "失败" in content:
            content = _get_search_result_content_via_selenium(driver)
        if not content:
            content = "（未获取到结果区内容，可能遇到验证码/地区限制或网络问题，请稍后重试）"

        return f"已在 Chrome 中搜索: {query}\n【搜索结果摘要】\n{content}"

    except Exception as e:
        err_msg = str(e).lower()
        if "not reachable" in err_msg or "user data directory" in err_msg or "already in use" in err_msg:
            return (
                f"浏览器搜索失败: {e}\n"
                "【可能原因】本工具使用独立 Chrome 配置运行，若仍失败可尝试：1) 关闭所有 Chrome 窗口后重试；"
                "2) 或检查是否被安全软件拦截。与您日常使用的 Chrome 已分离，一般不会因「开了其他浏览器」而冲突。"
            )
        return f"浏览器搜索失败: {e}\n(若您同时开着多个 Chrome，可先关闭再重试；本工具使用独立配置目录，通常可与日常 Chrome 并存。)"

    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass