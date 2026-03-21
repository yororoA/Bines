import sys
from pathlib import Path


def ensure_project_root(current_file: str, levels_up: int) -> Path:
    """将项目根目录注入 sys.path 并返回该路径。"""
    root = Path(current_file).resolve().parents[levels_up]
    root_str = str(root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)
    return root
