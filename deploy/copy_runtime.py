"""Copy only shared runtime libraries into the dedicated nsjail root."""

import re
import shutil
import subprocess
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
if sys.platform != "linux" or root == Path("/") or not (root / "opt/python/bin/python3.12").is_file():
    raise SystemExit("Use a dedicated Linux rootfs containing opt/python/bin/python3.12")
python = root / "opt/python"
files = [python / "bin/python3.12", *python.rglob("*.so"), *python.rglob("*.so.*")]
copied = set()
for file in files:
    result = subprocess.run(["ldd", str(file)], capture_output=True, text=True, check=False)
    for match in re.finditer(r"(?:=>\s+)?(/[^\s]+)\s+\(", result.stdout):
        source = Path(match[1])
        if python in source.parents or source in copied:
            continue
        target = root / source.relative_to("/")
        if not target.resolve().is_relative_to(root):
            raise SystemExit("Runtime copy target escapes rootfs")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target, follow_symlinks=True)
        copied.add(source)
print(f"Copied {len(copied)} runtime libraries")
