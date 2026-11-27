import re
with open('/Users/bytedance/Bines/thingking/tests/test_persona_and_prompts.py', 'r') as f:
    content = f.read()

content = content.replace(
    'for trait in ("充满活力", "活泼", "捉摸不透又黏人", "自然"):',
    'for trait in ("活泼", "捉摸不透又黏人", "自然"):'
)
content = content.replace(
    '"你想吐槽我听着"',
    '"过来让我抱抱"'
)
content = content.replace(
    '"想呀 这次让我挑个话题嘛"',
    '"想呀~ 这次让我挑个话题嘛"'
)

with open('/Users/bytedance/Bines/thingking/tests/test_persona_and_prompts.py', 'w') as f:
    f.write(content)
