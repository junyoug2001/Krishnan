"""Compatibility entry point; backend controls now live in the GUI Console tab."""
from pathlib import Path
import sys


def main():
    if len(sys.argv) > 2 and sys.argv[2] == "motors":
        from .motor_service import run
    else:
        from .device_backend import run
    run(Path(sys.argv[1]).expanduser().resolve())


if __name__ == "__main__":
    main()
