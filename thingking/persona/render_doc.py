"""Regenerate PERSONA.md from persona.yaml. Run: python thingking/persona/render_doc.py [--check]"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from persona_profile import render_markdown  # noqa: E402

DOC_PATH = HERE / "PERSONA.md"


def main() -> int:
    rendered = render_markdown()
    if "--check" in sys.argv:
        current = DOC_PATH.read_text(encoding="utf-8") if DOC_PATH.exists() else ""
        if current != rendered:
            print("PERSONA.md is out of date; run python thingking/persona/render_doc.py")
            return 1
        print("PERSONA.md is up to date")
        return 0
    DOC_PATH.write_text(rendered, encoding="utf-8", newline="\n")
    print(f"wrote {DOC_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
