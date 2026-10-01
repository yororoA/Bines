---
name: screen-observation
description: 获取当前屏幕、周边视觉信息或快速识别屏幕元素。
actions: get_screen_info, get_visual_info, fast_screen_analysis, find_color_region, template_match
---
只根据用户当前请求观察实时画面。不要用屏幕观察读取历史对话或记忆。若上下文已明确包含本轮最新屏幕结果，不要重复获取。
