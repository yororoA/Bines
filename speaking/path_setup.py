import sys
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
_ROOT_STR = str(_ROOT)
if _ROOT_STR not in sys.path:
    sys.path.insert(0, _ROOT_STR)

from tools.path_setup import ensure_project_root
