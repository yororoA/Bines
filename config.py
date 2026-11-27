"""Load root .env settings and safe defaults; credentials never live in Python."""
from pathlib import Path
import runpy

globals().update({
    key: value for key, value in runpy.run_path(
        str(Path(__file__).with_name("config.example.py"))
    ).items() if not key.startswith("_")
})
