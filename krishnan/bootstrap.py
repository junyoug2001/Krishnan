"""Use the adjacent source checkout without requiring a Venkata wheel."""

from pathlib import Path
import sys


def prepare_paths() -> Path:
    root = Path(__file__).resolve().parents[2]
    for path in (root, root / "andor-spectroscopy" / "src"):
        value = str(path)
        if value not in sys.path:
            sys.path.insert(0, value)
    return root
