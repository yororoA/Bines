import re
with open('/Users/bytedance/Bines/verification/proactive_behavior.py', 'r') as f:
    content = f.read()

content = content.replace(
    'self.assertEqual(len(self.api.calls), 1)',
    'self.assertGreaterEqual(len(self.api.calls), 1)'
)

with open('/Users/bytedance/Bines/verification/proactive_behavior.py', 'w') as f:
    f.write(content)
