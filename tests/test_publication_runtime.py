"""Only synthetic fixtures enter publication tests; no private output is exposed."""

import os
import re
import zipfile

import pytest

from runtime import publication


@pytest.fixture
def source_root(tmp_path):
    root = tmp_path / "source"
    (root / "api").mkdir(parents=True)
    (root / "config" / "models").mkdir(parents=True)
    (root / "contracts").mkdir()
    (root / "docs").mkdir()
    (root / ".github" / "workflows").mkdir(parents=True)
    files = {
        "main.py": "print('entry')\n",
        ".gitignore": "data/\n",
        "LICENSE": "Synthetic backend license\n",
        "NOTICE": "Synthetic third-party resource boundaries\n",
        "docs/deployment.md": "Synthetic deployment instructions\n",
        "README.md": "# Backend\n\n[Deployment](docs/deployment.md) [MIT](LICENSE) [NOTICE](NOTICE)\n",
        ".env.example": "TEXT_MAIN_API_KEY=\n",
        "api/main.py": "VERSION = 'test'\n",
        "config/models/providers.json": '{"roles": {}}\n',
        "contracts/openapi.json": '{"openapi": "3.1.0"}\n',
        ".github/workflows/backend.yml": "name: backend\n",
    }
    for name, content in files.items():
        (root / name).write_text(content, encoding="utf-8")
    return root


def test_export_allowlist_excludes_runtime_frontend_and_private_files(source_root, tmp_path):
    private = (
        ".env",
        "data/syllabus/model.onnx",
        "models/model.onnx",
        ".venv/bin/python",
        "archive/old.md",
        "test-results/log.json",
        "rootfs/main.py",
        "vendor/main.py",
        "frontend/src/main.ts",
        "config/secret.key",
        "config/copy.pdf",
        "config/local.env",
        "config/cache.sqlite3-wal",
        "config/__pycache__/a.pyc",
    )
    for name in private:
        path = source_root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("PRIVATE\n", encoding="utf-8")
    archive = tmp_path / "backend.zip"
    sources = publication.publish(archive, root=source_root)
    with zipfile.ZipFile(archive) as exported:
        assert exported.namelist() == [source.name for source in sources]
        assert "contracts/openapi.json" in exported.namelist()
        assert ".github/workflows/backend.yml" in exported.namelist()
        assert ".env.example" in exported.namelist()
        assert "config/models/providers.json" in exported.namelist()
        assert "LICENSE" in exported.namelist()
        assert "README.md" in exported.namelist()
        assert not set(private).intersection(exported.namelist())
        assert all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in exported.infolist())


def test_zip_is_reproducible_and_check_does_not_write(source_root, tmp_path):
    before = {item.relative_to(source_root) for item in source_root.rglob("*")}
    assert publication.publish(root=source_root)
    assert before == {item.relative_to(source_root) for item in source_root.rglob("*")}
    first, second = tmp_path / "one.zip", tmp_path / "two.zip"
    publication.publish(first, root=source_root)
    for path in source_root.rglob("*"):
        os.utime(path, (1_700_000_000, 1_700_000_000))
    publication.publish(second, root=source_root)
    assert first.read_bytes() == second.read_bytes()


def test_standalone_archive_has_only_backend_workflow_and_existing_readme_links(source_root, tmp_path):
    for name in ("frontend.yml", "unrelated.yaml"):
        (source_root / ".github" / "workflows" / name).write_text(
            "Synthetic workflow that requires omitted sources\n", encoding="utf-8"
        )
    output = tmp_path / "standalone.zip"
    publication.publish(output, root=source_root)
    with zipfile.ZipFile(output) as archive:
        workflows = [name for name in archive.namelist() if name.startswith(".github/workflows/")]
        assert workflows == [".github/workflows/backend.yml"]
        readme = archive.read("README.md").decode()
        assert "frontend/" not in readme
        assert "[Deployment](docs/deployment.md)" in readme
        assert "[MIT](LICENSE)" in readme
        assert "[NOTICE](NOTICE)" in readme
        targets = re.findall(r"\[[^]]+\]\(([^)]+)\)", readme)
        assert targets and all(target in archive.namelist() for target in targets)
    assert not (source_root / "backend").exists()
    assert readme == (source_root / "README.md").read_bytes().decode("utf-8")


def test_standalone_archive_reexports_identically_without_frontend(source_root, tmp_path):
    first, second = tmp_path / "first.zip", tmp_path / "second.zip"
    publication.publish(first, root=source_root)
    extracted = tmp_path / "extracted"
    with zipfile.ZipFile(first) as archive:
        archive.extractall(extracted)
    assert not (extracted / "frontend").exists()
    publication.publish(second, root=extracted)
    assert first.read_bytes() == second.read_bytes()


def test_standalone_archive_does_not_read_omitted_workflows(source_root, monkeypatch):
    omitted = source_root / ".github" / "workflows" / "frontend.yml"
    omitted.write_text("not part of the backend publication\n", encoding="utf-8")
    original = publication._read_source

    def read_source(path, root):
        assert path != omitted, "Unrelated workflow must not be inspected or exported"
        return original(path, root)

    monkeypatch.setattr(publication, "_read_source", read_source)
    assert publication.publish(root=source_root)


@pytest.mark.parametrize("relative", ["api/result.zip", "../escape.zip", "other.zip"])
def test_output_must_not_overlap_sources_or_traverse(source_root, relative):
    with pytest.raises(publication.PublicationError, match="Output"):
        publication.publish(source_root / relative, root=source_root)


def test_export_never_overwrites_existing_output(source_root, tmp_path):
    output = tmp_path / "existing.zip"
    output.write_bytes(b"keep")
    with pytest.raises(publication.PublicationError, match="already exists"):
        publication.publish(output, root=source_root)
    assert output.read_bytes() == b"keep"
    publication.publish(source_root / "dist" / "new.zip", root=source_root)


def test_linked_source_is_rejected_before_name_exclusion(source_root, monkeypatch):
    target = source_root / "config" / "private.key"
    target.write_text("never follow", encoding="utf-8")
    original = publication._is_link
    monkeypatch.setattr(publication, "_is_link", lambda path: path == target or original(path))
    with pytest.raises(publication.PublicationError, match="Linked source"):
        publication.publish(root=source_root)


def test_real_symlink_and_linked_output_directory_are_rejected(source_root, tmp_path):
    link = source_root / "api" / "linked.py"
    try:
        link.symlink_to(source_root / "main.py")
    except OSError:
        pytest.skip("Native symlinks unavailable on this host")
    with pytest.raises(publication.PublicationError, match="Linked source"):
        publication.publish(root=source_root)
    link.unlink()
    directory = tmp_path / "linked-output"
    directory.symlink_to(source_root, target_is_directory=True)
    with pytest.raises(publication.PublicationError, match="link"):
        publication.publish(directory / "publication.zip", root=source_root)


def test_binary_and_unknown_source_types_are_rejected(source_root):
    path = source_root / "api" / "hidden.py"
    path.write_bytes(b"prefix\0binary")
    with pytest.raises(publication.PublicationError, match="Binary source"):
        publication.publish(root=source_root)
    path.unlink()
    path.with_suffix(".exe").write_bytes(b"executable")
    with pytest.raises(publication.PublicationError, match="Unsupported source type"):
        publication.publish(root=source_root)


def test_non_utf8_and_oversized_sources_fail_closed(source_root, monkeypatch):
    path = source_root / "api" / "bad.py"
    path.write_bytes(b"\xff\xfe")
    with pytest.raises(publication.PublicationError, match="Non-UTF-8"):
        publication.publish(root=source_root)
    path.write_bytes(b"a" * 1025)
    monkeypatch.setattr(publication, "MAX_SOURCE_BYTES", 1024)
    with pytest.raises(publication.PublicationError, match="Oversized"):
        publication.publish(root=source_root)


@pytest.mark.skipif(os.name == "nt", reason="Windows prevents nonportable filenames itself")
def test_windows_traversal_name_and_case_collisions_are_rejected(source_root):
    path = source_root / "api" / "..\\evil.py"
    path.write_text("unsafe", encoding="utf-8")
    with pytest.raises(publication.PublicationError, match="not portable"):
        publication.publish(root=source_root)
    path.unlink()
    (path.parent / "MAIN.py").write_text("collision", encoding="utf-8")
    with pytest.raises(publication.PublicationError, match="collide"):
        publication.publish(root=source_root)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="Native FIFO requires POSIX")
def test_special_source_file_is_rejected(source_root):
    os.mkfifo(source_root / "api" / "pipe.py")
    with pytest.raises(publication.PublicationError, match="Non-regular source"):
        publication.publish(root=source_root)


def test_secret_reports_only_location_and_kind_and_never_creates_zip(source_root, tmp_path):
    secret = "sk-" + "Q7bP2xMv" * 6
    path = source_root / "api" / "unsafe.py"
    path.write_text("# comment\nKEY = '" + secret + "'\n", encoding="utf-8")
    output = tmp_path / "blocked.zip"
    with pytest.raises(publication.PublicationError) as caught:
        publication.publish(output, root=source_root)
    assert "api/unsafe.py:2: provider-api-key" in str(caught.value)
    assert secret not in str(caught.value)
    assert not output.exists()


def test_known_dotenv_secret_exact_match_is_redacted(source_root):
    secret = "local-test-value-" + "1234567890"
    (source_root / ".env").write_text(
        'TEXT_MAIN_API_KEY="' + secret + '"\nTEXT_MAIN_MODEL=not-secret-model-name\n', encoding="utf-8"
    )
    source = source_root / "api" / "bad.py"
    source.write_text("copied_value = '" + secret + "'\n", encoding="utf-8")
    with pytest.raises(publication.PublicationError) as caught:
        publication.publish(root=source_root)
    assert "known-local-secret" in str(caught.value)
    assert secret not in str(caught.value)
    assert "not-secret-model-name" not in publication.known_secrets(source_root)


@pytest.mark.parametrize("filename", ["jwt.key", "admin-password.hash"])
def test_default_storage_secret_exact_matches_are_redacted(source_root, filename):
    storage = source_root / "data"
    storage.mkdir()
    secret = "synthetic-" + filename + "-" + "0123456789" * 4
    (storage / filename).write_text(secret + "\n", encoding="utf-8")
    source = source_root / "api" / "bad.py"
    source.write_text("accidental_copy = '" + secret + "'\n", encoding="utf-8")
    with pytest.raises(publication.PublicationError) as caught:
        publication.publish(root=source_root)
    assert "api/bad.py:1: known-local-secret" in str(caught.value)
    assert secret not in str(caught.value)
    assert all(not item.name.startswith("data/") for item in publication.collect_sources(source_root))


@pytest.mark.parametrize("filename", ["jwt.key", "admin-password.hash"])
def test_default_secret_storage_rejects_links_and_oversized_files(source_root, filename, monkeypatch):
    storage = source_root / "data"
    storage.mkdir()
    path = storage / filename
    path.write_bytes(b"x" * (16 * 1024 + 1))
    with pytest.raises(publication.PublicationError, match="cannot be safely checked"):
        publication.known_secrets(source_root)
    path.write_text("synthetic-secret-123456789", encoding="utf-8")
    original = publication._is_link
    monkeypatch.setattr(publication, "_is_link", lambda candidate: candidate == path or original(candidate))
    with pytest.raises(publication.PublicationError, match="cannot be safely checked"):
        publication.known_secrets(source_root)


def test_default_secret_storage_directory_is_not_followed(source_root, monkeypatch):
    storage = source_root / "data"
    storage.mkdir()
    original = publication._is_link
    monkeypatch.setattr(publication, "_is_link", lambda candidate: candidate == storage or original(candidate))
    with pytest.raises(publication.PublicationError, match="storage cannot be safely checked"):
        publication.known_secrets(source_root)


def test_patterns_cover_private_key_and_ignore_short_invalid_url_fixture():
    private_header = "-----BEGIN " + "PRIVATE KEY-----"
    token = "ghp_" + "Q7bP2xMv" * 5
    source = publication.SourceFile("example.py", (private_header + "\n" + token).encode())
    findings = publication.scan_sources((source,))
    assert [(item.line, item.kind) for item in findings] == [(1, "private-key"), (2, "github-token")]
    harmless = publication.SourceFile("test.py", b"URL='https://sk-invalid-url@example.test'\n")
    assert not publication.scan_sources((harmless,))


def test_cli_success_and_failure_are_safe(source_root, monkeypatch, capsys):
    actual_publish = publication.publish
    monkeypatch.setattr(publication, "publish", lambda output: actual_publish(output, root=source_root))
    assert publication.main(["--check"]) == 0
    assert "files" in capsys.readouterr().out
    (source_root / "api" / "bad.py").write_text(
        "-----BEGIN " + "OPENSSH PRIVATE KEY-----", encoding="utf-8"
    )
    assert publication.main(["--check"]) == 1
    failure = capsys.readouterr()
    assert "private-key" in failure.err
    assert "BEGIN" not in failure.err
    assert not failure.out


def test_archive_creation_failure_cleans_temporary_files(source_root, tmp_path, monkeypatch):
    output = tmp_path / "failed.zip"
    monkeypatch.setattr(publication.os, "link", lambda *_: (_ for _ in ()).throw(OSError("private path")))
    with pytest.raises(publication.PublicationError, match="Cannot create"):
        publication.publish(output, root=source_root)
    assert not output.exists()
    assert not list(tmp_path.glob(".synaxis-publication-*"))
