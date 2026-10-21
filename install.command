#!/bin/bash
cd "$(dirname "$0")" || exit 1
installer_python=""
for candidate in python3 python3.12 python3.11 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        installer_python="$candidate"
        break
    fi
done
if [ -z "$installer_python" ] && [ -x ".venv/bin/python" ]; then
    installer_python=".venv/bin/python"
fi
if [ -n "$installer_python" ]; then
    "$installer_python" install.py "$@"
    result=$?
else
    printf '未找到 Python，请先安装 Python 3.12 后重试。\n'
    result=1
fi
if [ -t 0 ]; then
    printf '\n按回车关闭…'
    read -r _
fi
exit "$result"
