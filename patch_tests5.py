import re
with open('/Users/bytedance/Bines/thingking/tests/test_persona_and_prompts.py', 'r') as f:
    content = f.read()

content = content.replace(
    'for trait in ("满怀爱意", "活泼", "捉摸不透又黏人", "自然"):',
    'for trait in ("充满活力", "活泼", "捉摸不透又黏人", "自然"):'
)
content = content.replace(
    'for requirement in ("活泼、自然、满怀爱意", "不写成第三人称工作报告",',
    'for requirement in ("活泼、自然、满怀爱意的声音", "不写成第三人称工作报告",'
)

with open('/Users/bytedance/Bines/thingking/tests/test_persona_and_prompts.py', 'w') as f:
    f.write(content)
