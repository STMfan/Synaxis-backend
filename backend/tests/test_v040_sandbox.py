import asyncio
import base64
import io
from pathlib import Path

import pytest
from PIL import Image

from domain.errors import HarnessError
from tools import pythonbox


def png():
    stream = io.BytesIO()
    Image.new("RGB", (8, 8), "blue").save(stream, format="PNG")
    return stream.getvalue()


@pytest.mark.parametrize("inputs", [{"../escape": b"x"}, {"a" * 32: b"invalid image"}])
async def test_local_sandbox_rejects_invalid_input_before_process(settings, inputs, monkeypatch):
    async def spawn(*args, **kwargs):
        pytest.fail("Invalid input must never start a process")

    monkeypatch.setattr(pythonbox.asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(HarnessError, match="SANDBOX_INVALID_INPUT"):
        await pythonbox.run_nsjail(settings, "print(1)", inputs)


async def test_local_nsjail_arguments_and_bounded_outputs(settings, tmp_path, monkeypatch):
    settings.nsjail_root = tmp_path / "rootfs"
    interpreter = settings.nsjail_root / "opt/python/bin/python3.12"
    interpreter.parent.mkdir(parents=True)
    interpreter.touch()
    monkeypatch.setattr(pythonbox.sys, "platform", "linux")
    monkeypatch.setattr(pythonbox.shutil, "which", lambda _: "nsjail")
    requests = []

    class Process:
        returncode = 0

        def __init__(self):
            self.stdout, self.stderr = asyncio.StreamReader(), asyncio.StreamReader()
            self.stdout.feed_data(b"x" * 80000)
            self.stdout.feed_eof()
            self.stderr.feed_eof()

        async def wait(self):
            return self.returncode

    async def spawn(*args, **kwargs):
        requests.append((args, kwargs))
        target = next(args[i + 1] for i, a in enumerate(args) if a == "--bindmount").rsplit(":", 1)[0]
        Path(target, "output.png").write_bytes(png())
        return Process()

    monkeypatch.setattr(pythonbox.asyncio, "create_subprocess_exec", spawn)
    result = await pythonbox.run_nsjail(settings, "print(5)", {"a" * 32: png()})
    assert result["exit_code"] == 0 and len(result["stdout"]) == 65536
    assert base64.b64decode(result["files"][0]["data"]) == png()
    args, kwargs = requests[0]
    assert "--bindmount_ro" in args and "--chroot" in args and "--rlimit_as" in args
    assert "--disable_clone_newnet" not in args and "--disable_clone_newuser" not in args
    assert args[args.index("--user") + 1] == "65534"
    assert list(kwargs["env"]) == ["PATH"]
    assert pythonbox.sandbox_available(settings)
    settings.sandbox_enabled = False
    assert not pythonbox.sandbox_available(settings)
