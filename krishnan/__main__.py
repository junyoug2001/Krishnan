"""Launch the measurement workstation."""

from .bootstrap import prepare_paths

prepare_paths()

if __name__ == "__main__":
    try:
        from .gui import main
        main()
    except BaseException:
        import traceback
        from pathlib import Path
        from datetime import datetime
        path = Path(__file__).resolve().parents[1] / "logs" / "gui"
        path.mkdir(parents=True, exist_ok=True)
        error = traceback.format_exc()
        (path / f"startup-{datetime.now():%Y%m%d-%H%M%S}.log").write_text(error, encoding="utf-8")
        import os
        if os.name == "nt":
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, f"Krishnan could not start. See {path}\n\n{error[-1000:]}",
                                            "Krishnan startup error", 0x10)
        raise
