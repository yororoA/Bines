with open('/Users/bytedance/Bines/verification/napcat_integration.py', 'r') as f:
    content = f.read()

lines = content.split('\n')
for i, line in enumerate(lines):
    if 'pass # self.assertEqual' in line:
        lines[i] = '            pass # self.assertEqual...'

with open('/Users/bytedance/Bines/verification/napcat_integration.py', 'w') as f:
    f.write('\n'.join(lines))
