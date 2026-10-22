"""只读扫描：统计 C 盘各目录占用，找出可回收空间。

不删除、不移动任何文件，只输出报告。
"""
from __future__ import annotations

import os
import sys

ROOT = "C:\\"
SKIP_NAMES = {"$Recycle.Bin", "System Volume Information", "$WinREAgent", "Config.Msi"}


def dir_size(path: str, cap: int = 0) -> int:
    total = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_symlink() or entry.is_junction():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name in SKIP_NAMES:
                                continue
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def fmt(size: int) -> str:
    gb = size / 2 ** 30
    if gb >= 1:
        return f"{gb:7.2f} GB"
    return f"{size / 2 ** 20:7.1f} MB"


def main() -> None:
    targets = []
    for entry in os.scandir(ROOT):
        try:
            if entry.is_dir(follow_symlinks=False) and entry.name not in SKIP_NAMES:
                targets.append(entry.path)
        except OSError:
            continue
    # 单独列出几个已知大户（散落在用户目录深处的）
    deep = [
        r"C:\Users\ice\AppData\Local\Docker",
        r"C:\Users\ice\AppData\Local\Temp",
        r"C:\Users\ice\AppData\Local\Packages",
        r"C:\Users\ice\AppData\Local\pip",
        r"C:\Users\ice\AppData\Local\Programs",
        r"C:\Users\ice\AppData\Roaming",
        r"C:\Users\ice\.cache",
        r"C:\Windows\SoftwareDistribution",
        r"C:\Windows\Temp",
    ]

    rows = []
    for path in targets + [d for d in deep if os.path.isdir(d)]:
        sys.stderr.write(f"scanning {path}\n")
        sys.stderr.flush()
        rows.append((dir_size(path), path))

    rows.sort(reverse=True)
    print()
    print(f"{'大小':>11}  路径")
    print("-" * 74)
    for size, path in rows[:28]:
        print(f"{fmt(size):>11}  {path}")


if __name__ == "__main__":
    main()
