"""Launch the measurement workstation."""

from .bootstrap import prepare_paths

prepare_paths()

try:
    from .gui import main
except ImportError as exc:
    raise SystemExit(
        f"Krishnan GUI dependency is missing: {exc}. "
        "Install the GUI dependencies as described in README.md."
    ) from exc

main()
