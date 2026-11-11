import re
with open('/Users/bytedance/Bines/thingking/tests/test_persona_and_prompts.py', 'r') as f:
    content = f.read()

content = content.replace(
    'self.assertIn("活泼、有点小脾气、嘴硬心软", messages[-1]["content"])',
    'self.assertIn("活泼、自然、满怀爱意", messages[-1]["content"])'
)
content = content.replace(
    'for trait in ("满怀爱意", "小骄傲", "喜欢撒娇", "捉摸不透又黏人", "活泼", "轻快反击", "思维跳跃"):',
    'for trait in ("活泼、自然、满怀爱意", "自然", "满怀爱意", "活泼", "捉摸不透又黏人"):'
)
content = content.replace(
    'for requirement in ("活泼、自然、满怀爱意", "小脾气", "不写成第三人称工作报告",',
    'for requirement in ("活泼、自然、满怀爱意", "不写成第三人称工作报告",'
)

with open('/Users/bytedance/Bines/thingking/tests/test_persona_and_prompts.py', 'w') as f:
    f.write(content)
