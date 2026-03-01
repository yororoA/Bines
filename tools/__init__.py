from .time_tool import get_current_time
from .screen_tool import get_screen_info, get_screen_info_wrapper
from .app_tool import open_application
from .automation_tool import automate_action, automate_sequence
from .browser_tool import browser_search
from .music_tool import (
    open_netease_music, 
    search_music_in_netease, 
    play_music_in_netease, 
    control_netease_music, 
    play_song_from_playlist,
    play_all_songs_in_playlist,
    search_and_open_playlist,
    collect_playlist,
    uncollect_playlist,
    get_playlist_name_in_netease,
    switch_to_playlist
)
from .smart_automation_tool import find_element_and_click, find_element_and_type, analyze_and_operate, smart_click, smart_type
from .pointer_tool import (
    left_click,
    left_double_click,
    right_click,
    left_drag,
    right_drag,
    type_text,
    hotkey,
)
# 新增工具（从 handle_zmq.py 迁移）
from .game_mode_tool import enable_game_mode, disable_game_mode
from .memory_tool import update_status
from .visual_tool import get_visual_info
from .thinking_tool import call_thinking_model
from .sing_tool import sing
# QQ 工具
from .qq_tool import send_qq_private_msg, send_qq_group_msg, get_qq_group_list, get_qq_friend_list, broadcast_to_all_friends, broadcast_to_all_groups, at_each_group_member

# 动态（Moments）工具：get_moments（简化版）, add_moment, comment_moment, get_comments, like_moment, like_comment, analyze_moment_images（uid/token 从环境变量读取）
try:
    from .moments_tool import (
        get_moments_simple,
        add_moment,
        comment_moment,
        get_comments,
        like_moment,
        like_comment,
        analyze_moment_images,
    )
except ImportError:
    get_moments_simple = add_moment = comment_moment = None
    get_comments = like_moment = like_comment = analyze_moment_images = None

# 尝试导入快速屏幕工具（可选）
try:
    from .fast_screen_tool import fast_screen_analysis, find_color_region_in_screen, template_match_in_screen
    FAST_SCREEN_AVAILABLE = True
except ImportError:
    FAST_SCREEN_AVAILABLE = False


def task_complete(result: str = "") -> str:
    """工具模型显式结束任务时调用，提交最终结果。由 ThinkingModelHelper 识别后用于强制收敛。"""
    return "任务已确认完成。" + (f" 结果：{result[:200]}" if result else "")


# 工具注册表
# 格式: "tool_name": function
TOOLS_REGISTRY = {
    "get_time": get_current_time,
    "get_screen_info": get_screen_info_wrapper,  # 使用 wrapper 版本
    "open_application": open_application,
    "browser_search": browser_search,  # 浏览器搜索：仅 Selenium + DOM 内容，不用屏幕分析
    "automate_action": automate_action,
    "automate_sequence": automate_sequence,
    "open_netease_music": open_netease_music,
    "search_music_in_netease": search_music_in_netease,
    "play_music_in_netease": play_music_in_netease,
    "control_netease_music": control_netease_music,
    "play_song_from_playlist": play_song_from_playlist,
    "play_all_songs_in_playlist": play_all_songs_in_playlist,
    "search_and_open_playlist": search_and_open_playlist,
    "collect_playlist": collect_playlist,
    "uncollect_playlist": uncollect_playlist,
    "get_playlist_name_in_netease": get_playlist_name_in_netease,
    "switch_to_playlist": switch_to_playlist,
    "find_element_and_click": find_element_and_click,
    "find_element_and_type": find_element_and_type,
    "analyze_and_operate": analyze_and_operate,
    "smart_click": smart_click,
    "smart_type": smart_type,
    "left_click": left_click,
    "left_double_click": left_double_click,
    "right_click": right_click,
    "left_drag": left_drag,
    "right_drag": right_drag,
    "type_text": type_text,
    "hotkey": hotkey,
    # 新增工具（从 handle_zmq.py 迁移）
    "enable_game_mode": enable_game_mode,
    "disable_game_mode": disable_game_mode,
    "update_status": update_status,
    "get_visual_info": get_visual_info,
    "call_thinking_model": call_thinking_model,
    "task_complete": task_complete,
    "sing": sing,
    "send_qq_private_msg": send_qq_private_msg,
    "send_qq_group_msg": send_qq_group_msg,
    "get_qq_group_list": get_qq_group_list,
    "get_qq_friend_list": get_qq_friend_list,
    "broadcast_to_all_friends": broadcast_to_all_friends,
    "broadcast_to_all_groups": broadcast_to_all_groups,
    "at_each_group_member": at_each_group_member,
}
if get_moments_simple is not None:
    TOOLS_REGISTRY["get_moments"] = get_moments_simple
    TOOLS_REGISTRY["add_moment"] = add_moment
    TOOLS_REGISTRY["comment_moment"] = comment_moment
    if get_comments is not None:
        TOOLS_REGISTRY["get_comments"] = get_comments
    if like_moment is not None:
        TOOLS_REGISTRY["like_moment"] = like_moment
    if like_comment is not None:
        TOOLS_REGISTRY["like_comment"] = like_comment
    if analyze_moment_images is not None:
        TOOLS_REGISTRY["analyze_moment_images"] = analyze_moment_images

# 如果快速屏幕工具可用，注册它
if FAST_SCREEN_AVAILABLE:
    TOOLS_REGISTRY["fast_screen_analysis"] = fast_screen_analysis
    TOOLS_REGISTRY["find_color_region"] = find_color_region_in_screen
    TOOLS_REGISTRY["template_match"] = template_match_in_screen


def call_tool(tool_name, **kwargs):
    """统一调用接口"""
    func = TOOLS_REGISTRY.get(tool_name)
    if not func:
        return f"Error: Tool '{tool_name}' not found."
    try:
        return func(**kwargs)
    except Exception as e:
        return f"Error executing '{tool_name}': {e}"
