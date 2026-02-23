# 使用GUI显示模式，不再在终端输出文本
# 导入GUI显示模块
import sys
import os

# 添加当前目录到路径
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, CURRENT_DIR)

# 导入GUI显示模块
from gui_display import main as gui_main

if __name__ == "__main__":
    # 启动GUI窗口
    gui_main()
