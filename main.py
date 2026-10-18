"""Start Synaxis from the project root; all implementation stays in backend."""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BACKEND = ROOT / "backend"


def main():
    executable = BACKEND / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if executable.is_file() and Path(sys.prefix).resolve() != (BACKEND / ".venv").resolve():
        os.execv(str(executable), [str(executable), str(ROOT / "main.py"), *sys.argv[1:]])
    sys.path.insert(0, str(BACKEND))
    from runtime.launcher import main as launch
    return launch()


if __name__ == "__main__":
    raise SystemExit(main())
