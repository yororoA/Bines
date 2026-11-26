"""Launch Bines, including the isolated --profile chatbot --mode clone."""
from runtime_environments import enter_launcher

if __name__ == "__main__":
    enter_launcher()

from start_modules import main

if __name__ == "__main__":
    raise SystemExit(main())
