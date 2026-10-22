---
name: desktop-control
description: 打开本机应用，并在非浏览器应用中执行键鼠操作。
actions: open_application, automate_action, automate_sequence, find_element_and_click, find_element_and_type, analyze_and_operate, smart_click, smart_type, left_click, left_double_click, right_click, left_drag, right_drag, type_text, hotkey
---
仅处理桌面应用。所有浏览器、网页和搜索任务都交给 `web-research`。用户请求或可信 [System Event: Will] 可驱动操作；自主轮次只在键鼠空闲且目标清楚时行动，先观察真实画面，不猜坐标，不删除文件、支付或更改安全设置。目标或参数不明确时，自主轮次暂缓，普通请求向用户询问。
