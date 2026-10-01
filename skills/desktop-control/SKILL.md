---
name: desktop-control
description: 打开本机应用，并在非浏览器应用中执行键鼠操作。
actions: open_application, automate_action, automate_sequence, find_element_and_click, find_element_and_type, analyze_and_operate, smart_click, smart_type, left_click, left_double_click, right_click, left_drag, right_drag, type_text, hotkey
---
仅处理桌面应用。所有浏览器、网页和搜索任务都交给 `web-research`。按动作参数描述执行；如果目标或参数不明确，先向用户询问，不要猜坐标或输入内容。
