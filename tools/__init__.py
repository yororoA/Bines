"""Lazy capability registry: QQ mode imports no desktop/audio/browser drivers."""
from importlib import import_module
import inspect


class LazyTool:
    def __init__(self, module, name):
        self.module, self.name = module, name
        self.__doc__ = f"调用 {name}。"

    def resolve(self):
        return getattr(import_module(f"tools.{self.module}"), self.name)

    def __call__(self, **kwargs):
        return self.resolve()(**kwargs)


_MODULES = {
    "time_tool": {"get_time": "get_current_time"},
    "screen_tool": {"get_screen_info": "get_screen_info_wrapper"},
    "app_tool": {"open_application": "open_application"},
    "browser_tool": {"browser_search": "browser_search", "web_fetch": "web_fetch"},
    "automation_tool": {name: name for name in ("automate_action", "automate_sequence")},
    "music_tool": {name: name for name in (
        "open_netease_music", "search_music_in_netease", "play_music_in_netease",
        "control_netease_music", "play_song_from_playlist", "play_all_songs_in_playlist",
        "search_and_open_playlist", "collect_playlist", "uncollect_playlist",
        "get_playlist_name_in_netease", "switch_to_playlist")},
    "smart_automation_tool": {name: name for name in (
        "find_element_and_click", "find_element_and_type", "analyze_and_operate", "smart_click", "smart_type")},
    "pointer_tool": {name: name for name in (
        "left_click", "left_double_click", "right_click", "left_drag", "right_drag", "type_text", "hotkey")},
    "game_mode_tool": {name: name for name in ("enable_game_mode", "disable_game_mode")},
    "memory_tool": {"update_status": "update_status"},
    "visual_tool": {"get_visual_info": "get_visual_info"},
    "sing_tool": {name: name for name in ("sing", "list_songs")},
    "qq_tool": {name: name for name in (
        "send_qq_private_msg", "send_qq_group_msg", "get_qq_group_list", "get_qq_friend_list",
        "broadcast_to_all_friends", "broadcast_to_all_groups", "at_each_group_member")},
    "napcat_tool": {name: name for name in (
        "napcat_call", "napcat_discover", "napcat_catalog", "napcat_operation", "napcat_events", "napcat_send", "napcat_forward")},
    "moments_tool": {
        "get_moments": "get_moments_simple", **{name: name for name in (
            "add_moment", "comment_moment", "get_comments", "like_moment", "like_comment", "analyze_moment_images")}},
    "ark_tool": {name: name for name in (
        "get_ark_profile", "get_ark_conversations", "get_ark_chat_history", "send_ark_chat", "get_ark_operation")},
    "fast_screen_tool": {
        "fast_screen_analysis": "fast_screen_analysis", "find_color_region": "find_color_region_in_screen",
        "template_match": "template_match_in_screen"},
    "mcp_tool": {name: name for name in ("mcp_discover", "mcp_call")},
}
TOOLS_REGISTRY = {action: LazyTool(module, name) for module, rows in _MODULES.items() for action, name in rows.items()}


def call_tool(tool_name, **kwargs):
    try:
        return TOOLS_REGISTRY[tool_name](**kwargs)
    except Exception as exc:
        return f"Error executing '{tool_name}': {exc}"


def __getattr__(name):
    for module, names in _MODULES.items():
        if name in names.values():
            return getattr(import_module(f"tools.{module}"), name)
    raise AttributeError(name)
