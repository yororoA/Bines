import re
with open('/Users/bytedance/Bines/thingking/tests/test_persona_and_prompts.py', 'r') as f:
    content = f.read()

content = content.replace(
    'for requirement in ("活泼、自然、满怀爱意的声音", "不写成第三人称工作报告",\n                            "区分真实互动、工具观察和未执行的想法", "不按调用顺序抄流水账",\n                            "同一件事的得意或嘴硬表达一次就够", "随后写事情的结果或真正的新感受",\n                            "不要求每篇集齐全部性格或复读口头禅"):',
    'for requirement in ("活泼、自然、满怀爱意的声音", "不写成第三人称工作报告",\n                            "区分真实互动、工具观察和未执行的想法", "不按调用顺序抄流水账",\n                            "同一件事的得意或撒娇表达一次就够", "随后写事情的结果或真正的新感受",\n                            "不要求每篇集齐全部性格或复读口头禅"):'
)
content = content.replace(
    '"praised": ("骄傲", "固定句子"),',
    '"praised": ("开心", "固定句子"),'
)
content = content.replace(
    '"teased": ("假装不服气", "不升级成争吵", "明确不喜欢时收住"),',
    '"teased": ("顺着玩笑接下去", "不升级成争吵", "明确不喜欢时收住"),'
)

with open('/Users/bytedance/Bines/thingking/tests/test_persona_and_prompts.py', 'w') as f:
    f.write(content)
