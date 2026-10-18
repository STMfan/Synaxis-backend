"""Start Synaxis from the backend repository root."""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main():
    executable = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if executable.is_file() and Path(sys.prefix).resolve() != (ROOT / ".venv").resolve():
        os.execv(str(executable), [str(executable), str(ROOT / "main.py"), *sys.argv[1:]])
    from runtime.launcher import main as launch

    return launch()


if __name__ == "__main__":
    raise SystemExit(main())
