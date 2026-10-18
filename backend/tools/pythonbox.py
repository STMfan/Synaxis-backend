"""AST gate and local Linux nsjail subprocess. Code never runs in the API interpreter."""

import ast
import asyncio
import base64
import io
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

from PIL import Image

from core.settings import Settings
from domain.errors import HarnessError
from domain.validation import validate_svg

IMPORTS = {"numpy", "PIL", "PIL.Image", "PIL.ImageDraw", "PIL.ImageFont", "svglib", "svglib.svglib", "svg3d"}
BUILTINS = {
    "print",
    "range",
    "len",
    "abs",
    "round",
    "min",
    "max",
    "sum",
    "enumerate",
    "zip",
    "list",
    "tuple",
    "dict",
    "float",
    "int",
    "str",
    "bool",
    "sorted",
    "any",
    "all",
}
ATTRIBUTES = set(
    "array asarray arange linspace zeros ones eye full sin cos tan arcsin arccos arctan sqrt exp log log10 pi e dot cross matmul norm solve inv det linalg mean median std var sum min max abs round clip concatenate stack vstack hstack reshape transpose T shape size ndim dtype astype tolist append new open save fromarray Image ImageDraw ImageFont Draw line rectangle ellipse polygon text point width height resize rotate convert copy crop paste getpixel putpixel split merge composite blend uint8 float64 rgb rgba svg2rlg Engine Mesh View get_lookat_matrix get_projection_matrix render".split()
)
ALLOWED_NODES = {
    ast.Module,
    ast.Import,
    ast.ImportFrom,
    ast.alias,
    ast.Assign,
    ast.AugAssign,
    ast.Expr,
    ast.For,
    ast.If,
    ast.Pass,
    ast.Break,
    ast.Continue,
    ast.Name,
    ast.Load,
    ast.Store,
    ast.Constant,
    ast.List,
    ast.Tuple,
    ast.Dict,
    ast.Set,
    ast.BinOp,
    ast.UnaryOp,
    ast.BoolOp,
    ast.Compare,
    ast.Call,
    ast.keyword,
    ast.Attribute,
    ast.Subscript,
    ast.Slice,
    ast.IfExp,
    ast.ListComp,
    ast.GeneratorExp,
    ast.comprehension,
    ast.JoinedStr,
    ast.FormattedValue,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Pow,
    ast.MatMult,
    ast.UAdd,
    ast.USub,
    ast.Not,
    ast.And,
    ast.Or,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.In,
    ast.NotIn,
    ast.Is,
    ast.IsNot,
}


def validate_python(code: str, input_ids: list[str]):
    if len(code) > 20000:
        raise HarnessError("PYTHON_REJECTED", "Code exceeds 20000 characters")
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        raise HarnessError("PYTHON_REJECTED", "Invalid Python syntax") from exc
    inputs = {f"/work/input/{item}.png" for item in input_ids}
    for node in ast.walk(tree):
        if type(node) not in ALLOWED_NODES:
            raise HarnessError("PYTHON_REJECTED", f"AST node {type(node).__name__} is not allowed")
        if isinstance(node, ast.Name) and node.id.startswith("_"):
            raise HarnessError("PYTHON_REJECTED", "Private names are not allowed")
        if isinstance(node, ast.Import):
            if any(a.name not in IMPORTS or (a.asname or "").startswith("_") for a in node.names):
                raise HarnessError("PYTHON_REJECTED", "Import is not allowed")
        if isinstance(node, ast.ImportFrom):
            if (
                node.module not in IMPORTS
                or node.level
                or any(
                    a.name == "*" or a.name.startswith("_") or (a.asname or "").startswith("_")
                    for a in node.names
                )
            ):
                raise HarnessError("PYTHON_REJECTED", "Import is not allowed")
            if any(a.name not in ATTRIBUTES for a in node.names):
                raise HarnessError("PYTHON_REJECTED", "Imported member is not allowed")
        if isinstance(node, ast.Attribute) and node.attr not in ATTRIBUTES:
            raise HarnessError("PYTHON_REJECTED", "Attribute is not allowed")
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                if node.func.id not in BUILTINS:
                    raise HarnessError("PYTHON_REJECTED", "Call library functions through an allowed module")
            elif not isinstance(node.func, ast.Attribute):
                raise HarnessError("PYTHON_REJECTED", "Indirect calls are not allowed")
            name = node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
            if name in {"open", "save", "svg2rlg", "render"}:
                if (
                    not node.args
                    or not isinstance(node.args[0], ast.Constant)
                    or not isinstance(node.args[0].value, str)
                ):
                    raise HarnessError("PYTHON_REJECTED", "File paths must be literal approved paths")
                path = node.args[0].value
                approved = {
                    "save": {"/work/output/output.png"},
                    "render": {"/work/output/output.svg"},
                    "open": inputs,
                    "svg2rlg": inputs | {"/work/output/output.svg"},
                }
                if path not in approved[name]:
                    raise HarnessError("PYTHON_REJECTED", "File path is not allowed")
                allowed_keywords = {"size", "viewbox"} if name == "render" else set()
                if len(node.args) > 1 or any(k.arg not in allowed_keywords for k in node.keywords):
                    raise HarnessError("PYTHON_REJECTED", "File method options are not allowed")
    return tree


async def bounded_pipe(stream, cap=65536):
    chunks, size = [], 0
    while chunk := await stream.read(8192):
        if size < cap:
            chunks.append(chunk[: cap - size])
            size += len(chunks[-1])
    return b"".join(chunks)


async def run_nsjail(settings: Settings, code: str, inputs: dict[str, bytes]):
    if len(inputs) > 4 or any(not re.fullmatch(r"[0-9a-f]{32}", key) for key in inputs):
        raise HarnessError("SANDBOX_INVALID_INPUT")
    for raw in inputs.values():
        try:
            if len(raw) > settings.artifact_bytes:
                raise ValueError("Input too large")
            with Image.open(io.BytesIO(raw)) as picture:
                if picture.format != "PNG" or picture.width * picture.height > 4_000_000:
                    raise ValueError("Only bounded PNG input is allowed")
                picture.verify()
        except (ValueError, OSError, Image.DecompressionBombError) as exc:
            raise HarnessError("SANDBOX_INVALID_INPUT") from exc
    validate_python(code, list(inputs))
    if sys.platform != "linux" or not settings.nsjail_root or not shutil.which(settings.nsjail_binary):
        raise HarnessError(
            "SANDBOX_UNAVAILABLE", "Linux, nsjail and an isolated root filesystem are required"
        )
    root = settings.nsjail_root.resolve()
    if root == Path("/") or not (root / "opt/python/bin/python3.12").is_file():
        raise HarnessError("SANDBOX_UNAVAILABLE", "Use a dedicated root containing /usr/bin/python3")
    with tempfile.TemporaryDirectory(prefix="synaxis-jail-") as temp:
        work = Path(temp)
        work.chmod(0o755)
        input_dir, output_dir = work / "input", work / "output"
        input_dir.mkdir(mode=0o755)
        output_dir.mkdir(mode=0o777)
        output_dir.chmod(0o777)
        for artifact_id, data in inputs.items():
            path = input_dir / f"{artifact_id}.png"
            path.write_bytes(data)
            path.chmod(0o444)
        script = work / "code.py"
        script.write_text(code, encoding="utf-8")
        script.chmod(0o444)
        args = [
            settings.nsjail_binary,
            "--quiet",
            "--mode",
            "o",
            "--chroot",
            str(root),
            "--cwd",
            "/work",
            "--user",
            "65534",
            "--group",
            "65534",
            "--time_limit",
            str(settings.sandbox_seconds),
            "--rlimit_as",
            "1024",
            "--rlimit_cpu",
            str(settings.sandbox_seconds),
            "--rlimit_fsize",
            "8",
            "--rlimit_nofile",
            "64",
            "--rlimit_nproc",
            "32",
            "--bindmount_ro",
            f"{script}:/work/code.py",
            "--bindmount_ro",
            f"{input_dir}:/work/input",
            "--bindmount",
            f"{output_dir}:/work/output",
            "--env",
            "OPENBLAS_NUM_THREADS=1",
            "--",
            "/usr/bin/python3",
            "-I",
            "/work/code.py",
        ]
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        )
        pumps = [
            asyncio.create_task(bounded_pipe(process.stdout)),
            asyncio.create_task(bounded_pipe(process.stderr)),
        ]
        try:
            async with asyncio.timeout(settings.sandbox_seconds + 5):
                await process.wait()
                stdout, stderr = await asyncio.gather(*pumps)
        except BaseException:
            if process.returncode is None:
                process.kill()
            await process.wait()
            for pump in pumps:
                pump.cancel()
            await asyncio.gather(*pumps, return_exceptions=True)
            raise
        files = []
        for name, mime in (("output.png", "image/png"), ("output.svg", "image/svg+xml")):
            path = output_dir / name
            if path.is_file() and not path.is_symlink():
                if path.stat().st_size > settings.artifact_bytes:
                    raise HarnessError("ARTIFACT_TOO_LARGE")
                raw = path.read_bytes()
                if mime == "image/svg+xml":
                    validate_svg(raw.decode("utf-8"))
                files.append({"name": name, "mime": mime, "data": base64.b64encode(raw).decode()})
        return {
            "exit_code": process.returncode,
            "stdout": stdout.decode(errors="replace"),
            "stderr": stderr.decode(errors="replace"),
            "files": files,
        }


def sandbox_available(settings):
    return bool(
        settings.sandbox_enabled
        and sys.platform == "linux"
        and settings.nsjail_root
        and settings.nsjail_root.resolve() != Path("/")
        and (settings.nsjail_root / "opt/python/bin/python3.12").is_file()
        and shutil.which(settings.nsjail_binary)
    )
