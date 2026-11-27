import re
with open('/Users/bytedance/Bines/thingking/tests/test_persona_and_prompts.py', 'r') as f:
    content = f.read()

content = content.replace(
    'for trait in ("表面高傲", "偶尔有点不耐烦", "嘴硬心软", "内心善良", "活泼", "娇嗔", "电波系"):',
    'for trait in ("有点小骄傲", "偶尔不耐烦", "嘴硬心软", "内心柔软", "活泼", "轻快地反击", "思维跳跃"):'
)
content = content.replace(
    '"teased": ("娇嗔", "不升级成争吵", "明确不喜欢时收住"),',
    '"teased": ("假装不服气", "不升级成争吵", "明确不喜欢时收住"),'
)
content = content.replace(
    '"不能替换活泼、骄傲、嘴硬心软和奇趣"',
    '"不能替换活泼、直率、嘴硬心软和奇趣"'
)
content = content.replace(
    'for requirement in ("表面高傲内心温柔", "娇嗔", "不写成第三人称工作报告",',
    'for requirement in ("活泼、直率内心温柔", "小脾气", "不写成第三人称工作报告",'
)
content = content.replace(
    '"同一件事的得意、娇嗔或嘴硬表达一次就够",',
    '"同一件事的得意或嘴硬表达一次就够",'
)

with open('/Users/bytedance/Bines/thingking/tests/test_persona_and_prompts.py', 'w') as f:
    f.write(content)
