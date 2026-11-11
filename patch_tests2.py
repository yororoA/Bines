import re
with open('/Users/bytedance/Bines/thingking/tests/test_persona_and_prompts.py', 'r') as f:
    content = f.read()

content = content.replace(
    'for trait in ("有点小骄傲", "偶尔不耐烦", "嘴硬心软", "内心柔软", "活泼", "轻快地反击", "思维跳跃"):',
    'for trait in ("满怀爱意", "小骄傲", "喜欢撒娇", "捉摸不透又黏人", "活泼", "轻快反击", "思维跳跃"):'
)
content = content.replace(
    '"不能替换活泼、直率、嘴硬心软和奇趣"',
    '"不能替换活泼、自然和奇趣"'
)
content = content.replace(
    'for requirement in ("活泼、直率内心温柔", "小脾气", "不写成第三人称工作报告",',
    'for requirement in ("活泼、自然、满怀爱意", "小脾气", "不写成第三人称工作报告",'
)
content = content.replace(
    '"嘴硬心软"',
    '"满怀爱意"'
)

with open('/Users/bytedance/Bines/thingking/tests/test_persona_and_prompts.py', 'w') as f:
    f.write(content)
