import re
with open('/Users/bytedance/Bines/thingking/tests/test_persona_and_prompts.py', 'r') as f:
    content = f.read()

content = content.replace(
    'self.assertIn("自己决定分段", guard)',
    'self.assertIn("严禁包含任何换行符", guard)'
)
content = content.replace(
    'self.assertIn("不按字数拆分", guard)',
    'self.assertIn("独立的聊天气泡", guard)'
)

with open('/Users/bytedance/Bines/thingking/tests/test_persona_and_prompts.py', 'w') as f:
    f.write(content)
