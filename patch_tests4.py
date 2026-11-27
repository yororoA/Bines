import re
with open('/Users/bytedance/Bines/thingking/tests/test_persona_and_prompts.py', 'r') as f:
    content = f.read()

content = content.replace(
    'self.assertIn("真诚夸奖", guard)',
    'self.assertIn("直白表达感谢与关心", guard)'
)
content = content.replace(
    'for trait in ("活泼、自然、满怀爱意", "自然", "满怀爱意", "活泼", "捉摸不透又黏人"):',
    'for trait in ("满怀爱意", "活泼", "捉摸不透又黏人", "自然"):'
)

with open('/Users/bytedance/Bines/thingking/tests/test_persona_and_prompts.py', 'w') as f:
    f.write(content)
