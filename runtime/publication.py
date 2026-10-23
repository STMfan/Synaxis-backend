"""Export an audited backend source subset without relying on a Git checkout.

Run from backend: ``python -m runtime.publication --check`` or
``python -m runtime.publication --output dist/synaxis-backend.zip``.
Runtime data, curriculum assets, keys and frontend files are never candidates.
"""

from __future__ import annotations

import argparse
import os
import re
import stat
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

BACKEND_DIRECTORIES = (
    "api",
    "auth",
    "core",
    "domain",
    "llm",
    "runtime",
    "storage",
    "tools",
    "admin",
    "config",
    "docs",
    "tests",
    "deploy",
    "contracts",
)
BACKEND_FILES = (
    "pyproject.toml",
    ".python-version",
    "uv.lock",
    "README.md",
    ".env.example",
    ".gitignore",
    ".gitattributes",
    "LICENSE",
    "LICENSE.md",
    "NOTICE",
    "NOTICE.md",
)
ROOT_FILES = ("main.py", *BACKEND_FILES)
TEXT_SUFFIXES = frozenset(
    {
        ".py",
        ".pyi",
        ".json",
        ".md",
        ".txt",
        ".html",
        ".css",
        ".js",
        ".svg",
        ".service",
        ".yml",
        ".yaml",
        ".toml",
        ".lock",
        ".example",
    }
)
PRIVATE_DIRECTORIES = frozenset(
    {
        ".git",
        ".venv",
        ".uv-cache",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        ".tools",
        ".python",
        ".secrets",
        "data",
        "archive",
        "test-results",
        "deploy-backups",
        "rootfs",
        "vendor",
        "node_modules",
        "htmlcov",
        "dist",
    }
)
PRIVATE_SUFFIXES = (
    ".key",
    ".pem",
    ".p12",
    ".pfx",
    ".jks",
    ".keystore",
    ".sqlite",
    ".sqlite3",
    ".db",
    ".sqlite-wal",
    ".sqlite-shm",
    ".sqlite3-wal",
    ".sqlite3-shm",
    ".db-wal",
    ".db-shm",
    ".pyc",
    ".pyo",
    ".onnx",
    ".pdf",
    ".zip",
    ".tar",
    ".gz",
    ".log",
    ".bak",
)
MAX_SOURCE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024


class PublicationError(ValueError):
    """A safe public subset could not be established; no archive was published."""


@dataclass(frozen=True)
class SourceFile:
    name: str
    content: bytes


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    kind: str


# Only high confidence token formats. Generic passwords and arbitrary sk-* text
# would classify deliberately invalid URL fixtures and ordinary examples as secrets.
_TOKEN_PATTERNS = (
    ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,255}\b|\bgithub_pat_[A-Za-z0-9_]{60,255}\b")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("provider-api-key", re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{32,255}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{24,255}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{20,}\b")),
)
_SECRET_SETTING = re.compile(r"api[-_]?key|apikey|secret|password|token|image[-_]key", re.IGNORECASE)


def _is_link(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _private(name: str) -> bool:
    lower = name.casefold()
    return (
        lower in PRIVATE_DIRECTORIES
        or lower == ".coverage"
        or lower.startswith(".coverage.")
        or lower.startswith("id_rsa")
        or lower.startswith("id_ed25519")
        or (".env" in lower and lower != ".env.example")
        or lower.endswith(PRIVATE_SUFFIXES)
    )


def _source_name(path: Path, root: Path) -> str:
    relative = path.relative_to(root)
    if any(
        part in {".", ".."}
        or any(character in "\\:" or ord(character) < 32 or ord(character) == 127 for character in part)
        for part in relative.parts
    ):
        raise PublicationError("Source name is not portable or safe")
    return relative.as_posix()


def _read_source(path: Path, root: Path) -> SourceFile:
    relative = _source_name(path, root)
    if _is_link(path) or not stat.S_ISREG(path.lstat().st_mode):
        raise PublicationError(f"Non-regular source: {relative}")
    if path.suffix.casefold() not in TEXT_SUFFIXES and path.name not in (*ROOT_FILES, *BACKEND_FILES):
        raise PublicationError(f"Unsupported source type: {relative}")
    if path.stat().st_size > MAX_SOURCE_BYTES:
        raise PublicationError(f"Oversized source: {relative}")
    # Check again after open; a substituted symlink must not become a source.
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise PublicationError(f"Non-regular source: {relative}")
            content = stream.read(MAX_SOURCE_BYTES + 1)
    except OSError:
        raise PublicationError(f"Cannot read source: {relative}") from None
    if len(content) > MAX_SOURCE_BYTES:
        raise PublicationError(f"Oversized source: {relative}")
    try:
        decoded = content.decode("utf-8-sig")
    except UnicodeError:
        raise PublicationError(f"Non-UTF-8 source: {relative}") from None
    if "\0" in decoded:
        raise PublicationError(f"Binary source: {relative}")
    return SourceFile(relative, content)


def _walk(directory: Path, root: Path):
    if _is_link(directory) or not directory.is_dir():
        raise PublicationError(f"Non-directory source: {directory.relative_to(root).as_posix()}")
    for path in sorted(directory.iterdir(), key=lambda item: item.name):
        relative = _source_name(path, root)
        # Links and special files are rejected even if their names would be excluded.
        # Excluded ordinary directories are not traversed.
        if _is_link(path):
            raise PublicationError(f"Linked source: {relative}")
        info = path.lstat()
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise PublicationError(f"Non-regular source: {relative}")
        if _private(path.name):
            continue
        if path.is_dir():
            yield from _walk(path, root)
        else:
            yield _read_source(path, root)


def collect_sources(root: Path | None = None) -> tuple[SourceFile, ...]:
    """Read only the explicit backend source allowlist, including root CI/licenses."""
    root = Path(root) if root is not None else Path(__file__).resolve().parents[1]
    if _is_link(root) or not root.is_dir():
        raise PublicationError("Source root must be a real directory")
    root = root.resolve()
    collected: list[SourceFile] = []
    for path in (root / name for name in ROOT_FILES):
        if path.exists() or path.is_symlink():
            source = _read_source(path, root)
            collected.append(source)
    for name in BACKEND_DIRECTORIES:
        path = root / name
        if path.exists() or path.is_symlink():
            collected.extend(_walk(path, root))
    github = root / ".github"
    if github.exists() or github.is_symlink():
        if _is_link(github) or not github.is_dir():
            raise PublicationError("Non-directory source: .github")
        workflows = github / "workflows"
        if workflows.exists() or workflows.is_symlink():
            if _is_link(workflows) or not workflows.is_dir():
                raise PublicationError("Non-directory source: .github/workflows")
            workflow = workflows / "backend.yml"
            if workflow.exists() or workflow.is_symlink():
                collected.append(_read_source(workflow, root))
    collected.sort(key=lambda item: item.name)
    if len({item.name.casefold() for item in collected}) != len(collected):
        raise PublicationError("Source names collide on case-insensitive filesystems")
    if not collected or sum(len(item.content) for item in collected) > MAX_TOTAL_BYTES:
        raise PublicationError("Empty or oversized public source set")
    return tuple(collected)


def _private_text(path: Path, limit: int) -> str | None:
    if not path.exists() and not path.is_symlink():
        return None
    if _is_link(path) or not stat.S_ISREG(path.lstat().st_mode) or path.stat().st_size > limit:
        raise PublicationError("Local secret configuration cannot be safely checked")
    try:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise PublicationError("Local secret configuration cannot be safely checked")
            content = stream.read(limit + 1)
        if len(content) > limit:
            raise PublicationError("Local secret configuration cannot be safely checked")
        return content.decode("utf-8-sig")
    except (OSError, UnicodeError):
        raise PublicationError("Local secret configuration cannot be safely checked") from None


def known_secrets(root: Path) -> tuple[str, ...]:
    """Match local dotenv and default-store secrets only; never report their values.

    Custom DATA_DIR/config paths are intentionally not followed. Generic patterns
    and this default-store check cannot establish that unknown or historical
    secrets are absent.
    """
    dotenv = _private_text(root / ".env", 1024 * 1024)
    values: set[str] = set()
    for line in (dotenv or "").splitlines():
        key, separator, value = line.strip().removeprefix("export ").partition("=")
        if not separator or key.startswith("#") or not _SECRET_SETTING.search(key):
            continue
        value = value.strip()
        if value.startswith(("'", '"')) and value[-1:] == value[:1]:
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].rstrip()
        if len(value) >= 12 and not value.startswith("${"):
            values.add(value)
    storage = root / "data"
    if storage.exists() or storage.is_symlink():
        if _is_link(storage) or not storage.is_dir():
            raise PublicationError("Local secret storage cannot be safely checked")
        for filename in ("jwt.key", "admin-password.hash"):
            value = _private_text(storage / filename, 16 * 1024)
            if value is not None and len(value.strip()) >= 12:
                values.add(value.strip())
    return tuple(sorted(values))


def scan_sources(sources: tuple[SourceFile, ...], secrets: tuple[str, ...] = ()) -> tuple[Finding, ...]:
    """Return location/type only. Neither source snippets nor secret hashes escape."""
    found: set[Finding] = set()
    for source in sources:
        text = source.content.decode("utf-8-sig")
        for kind, pattern in _TOKEN_PATTERNS:
            for match in pattern.finditer(text):
                found.add(Finding(source.name, text.count("\n", 0, match.start()) + 1, kind))
        for secret in secrets:
            start = 0
            while (index := text.find(secret, start)) >= 0:
                found.add(Finding(source.name, text.count("\n", 0, index) + 1, "known-local-secret"))
                start = index + len(secret)
    return tuple(sorted(found, key=lambda item: (item.path, item.line, item.kind)))


def _validate_output(output: Path, root: Path) -> Path:
    if ".." in output.parts:
        raise PublicationError("Output must not contain parent traversal")
    if output.suffix.casefold() != ".zip":
        raise PublicationError("Output must be a ZIP file")
    # Resolve existing parents separately to detect symlinks/junctions before use.
    absolute = output.absolute()
    for path in (absolute, *absolute.parents):
        if (path.exists() or path.is_symlink()) and _is_link(path):
            raise PublicationError("Output cannot pass through a link")
    destination = absolute.resolve()
    if destination.exists():
        raise PublicationError("Output already exists; choose a new archive path")
    relative = destination.relative_to(root) if destination.is_relative_to(root) else None
    if relative is not None and not (len(relative.parts) >= 2 and relative.parts[0] == "dist"):
        raise PublicationError("Output inside source must be in dist/")
    return destination


def publish(output: Path | None = None, *, root: Path | None = None) -> tuple[SourceFile, ...]:
    """Scan the complete source snapshot, then atomically create a new ZIP if requested."""
    root = Path(root) if root is not None else Path(__file__).resolve().parents[1]
    sources = collect_sources(root)
    root = root.resolve()
    findings = scan_sources(sources, known_secrets(root))
    if findings:
        raise PublicationError(
            "Secret scan failed:\n" + "\n".join(f"{item.path}:{item.line}: {item.kind}" for item in findings)
        )
    if output is None:
        return sources
    destination = _validate_output(Path(output), root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        descriptor, name = tempfile.mkstemp(
            prefix=".synaxis-publication-", suffix=".tmp", dir=destination.parent
        )
        temporary = Path(name)
        with os.fdopen(descriptor, "w+b") as stream:
            # ZIP_STORED avoids platform/zlib-version-dependent compression output.
            with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as archive:
                for source in sources:
                    info = zipfile.ZipInfo(source.name, date_time=(1980, 1, 1, 0, 0, 0))
                    info.create_system = 3
                    info.external_attr = (stat.S_IFREG | 0o644) << 16
                    archive.writestr(info, source.content)
            stream.flush()
            os.fsync(stream.fileno())
        # Hard-link creation is atomic and fails if another process claimed the name.
        # No replace/overwrite operation can destroy an existing file.
        os.link(temporary, destination)
    except OSError:
        raise PublicationError("Cannot create publication archive") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return sources


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="Scan the public source allowlist without writing")
    mode.add_argument("--output", type=Path, help="Create a new reproducible backend ZIP")
    args = parser.parse_args(argv)
    try:
        sources = publish(args.output)
    except (PublicationError, OSError) as error:
        # PublicationError messages contain safe locations/types only; unrelated OS
        # exceptions can contain private path details and are intentionally hidden.
        print(str(error) if isinstance(error, PublicationError) else "Publication failed", file=sys.stderr)
        return 1
    print(f"Public backend sources verified: {len(sources)} files")
    if args.output:
        print("Backend source ZIP created")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
