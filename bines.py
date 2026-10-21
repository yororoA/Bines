"""Friendly alias: python bines.py [--profile chatbot|full]."""
from runtime_environments import enter_launcher

if __name__ == "__main__":
    enter_launcher()

from start_modules import main

if __name__ == "__main__":
    raise SystemExit(main())
