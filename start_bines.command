#!/bin/bash
cd "$(dirname "$0")" || exit 1
if [ -x ".venv/bin/python" ]; then
    exec .venv/bin/python bines.py --web "$@"
elif [ -x "thingking/thinking_venv/bin/python" ]; then
    exec thingking/thinking_venv/bin/python bines.py --web "$@"
else
    exec python3 bines.py --web "$@"
fi
