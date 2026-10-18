import asyncio
import subprocess
import sys

import pytest

from runtime import launcher
from runtime.maintenance import InstanceLock


def test_launcher_check_internal_guard_and_tls_validation(settings, monkeypatch, capsys):
    monkeypatch.setattr("core.settings.Settings", lambda: settings)
    assert launcher.main(["--check"]) == 0
    assert not settings.db_path.exists()
    assert "5667" in capsys.readouterr().out
    monkeypatch.delenv("SYNAXIS_MANAGED_WORKER", raising=False)
    assert launcher.main(["--serve-worker"]) == 2
    settings.tls_certfile = "missing.crt"
    assert launcher.main(["--check"]) == 2


def test_launcher_cancel_initialization_and_set_password(settings, monkeypatch):
    monkeypatch.setattr("core.settings.Settings", lambda: settings)
    monkeypatch.setattr("builtins.input", lambda _: "cancel")
    monkeypatch.setattr(launcher, "supervise", lambda: pytest.fail("Cancellation must not start service"))
    assert launcher.main(["--initialization"]) == 0
    calls = []
    monkeypatch.setattr("auth.admin_security.set_password", lambda value: calls.append(value.data_dir))
    assert launcher.main(["--set-admin-password"]) == 0
    assert calls == [settings.data_dir]


def test_instance_lock_prevents_second_launcher(settings):
    with InstanceLock(settings.data_dir), pytest.raises(RuntimeError):
        with InstanceLock(settings.data_dir):
            pytest.fail("Second instance must be blocked")


def test_supervisor_restarts_fresh_worker_without_reset_flags(monkeypatch):
    launches, codes = [], iter([launcher.RESTART, 0])

    class Child:
        def wait(self):
            return next(codes)

    def spawn(args, **kwargs):
        launches.append((args, kwargs))
        return Child()

    monkeypatch.setattr(subprocess, "Popen", spawn)
    assert launcher.supervise() == 0
    assert len(launches) == 2
    for args, kwargs in launches:
        assert args == [sys.executable, str(launcher.PROJECT / "main.py"), "--serve-worker"]
        assert kwargs["env"]["SYNAXIS_MANAGED_WORKER"] == "1"
        assert "--initialization" not in args


def test_supervisor_propagates_startup_failure(monkeypatch):
    class Child:
        def wait(self):
            return 2

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: Child())
    assert launcher.supervise() == 2


async def test_managed_server_disables_forwarded_headers_and_restarts(settings, monkeypatch):
    import uvicorn

    configurations = []

    class Server:
        def __init__(self, config):
            configurations.append(config)
            self.config, self.should_exit = config, False

        async def serve(self):
            self.config.app.state.restart_callback()
            async with asyncio.timeout(2):
                while not self.should_exit:
                    await asyncio.sleep(0.01)

    monkeypatch.setattr(uvicorn, "Server", Server)
    assert await launcher.serve(settings) == launcher.RESTART
    assert configurations[0].proxy_headers is False and configurations[0].workers == 1
