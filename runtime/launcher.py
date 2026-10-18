"""One launcher, one application worker, per-call local nsjail subprocesses."""

import argparse
import asyncio
import os
import signal
import subprocess
import sys
from pathlib import Path

from runtime.maintenance import InstanceLock, reset_databases

PROJECT = Path(__file__).resolve().parents[1]
RESTART = 75


async def serve(settings):
    import uvicorn

    from api.app import create_app

    app = create_app(settings)
    restart = asyncio.Event()
    app.state.restart_callback = restart.set
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=settings.host,
            port=settings.port,
            ssl_certfile=settings.tls_certfile,
            ssl_keyfile=settings.tls_keyfile,
            proxy_headers=False,
            workers=1,
            timeout_graceful_shutdown=30,
        )
    )

    async def watcher():
        await restart.wait()
        await asyncio.sleep(0.25)  # Flush the authenticated restart response.
        server.config.timeout_graceful_shutdown = 5  # Remaining SSE subscribers can reconnect.
        server.should_exit = True

    watcher_task = asyncio.create_task(watcher())
    try:
        await server.serve()
    finally:
        watcher_task.cancel()
        await asyncio.gather(watcher_task, return_exceptions=True)
    return RESTART if restart.is_set() else 0


def supervise():
    # A fresh interpreter reloads prompt/model JSON as well as network settings.
    # Initialization flags are deliberately not forwarded to restarted workers.
    environment = os.environ.copy()
    environment["SYNAXIS_MANAGED_WORKER"] = "1"
    child = None

    def stop(signum, frame):
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, stop)
    try:
        while True:
            child = subprocess.Popen(
                [sys.executable, str(PROJECT / "main.py"), "--serve-worker"],
                cwd=PROJECT,
                env=environment,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
            )
            code = child.wait()
            if code != RESTART:
                return code
    except KeyboardInterrupt:
        if child and child.poll() is None:
            # SIGINT asks uvicorn to close the lifespan and settle uncertain calls.
            try:
                child.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT)
            except (OSError, ValueError):
                child.terminate()
            try:
                child.wait(timeout=35)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        return 130
    finally:
        signal.signal(signal.SIGTERM, previous)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Synaxis backend / 后端启动与维护")
    parser.add_argument("--config-dir", type=Path, help="Non-secret JSON directory / JSON 配置目录")
    parser.add_argument("--env-file", type=Path, help="Private dotenv path / 私有环境文件路径")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--initialization",
        action="store_true",
        help="Confirm twice and back up before resetting both databases",
    )
    mode.add_argument(
        "--set-admin-password",
        action="store_true",
        help="Set the independent administrator password interactively",
    )
    mode.add_argument(
        "--check", action="store_true", help="Validate configuration without starting or changing databases"
    )
    mode.add_argument("--serve-worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.config_dir:
        os.environ["SYNAXIS_CONFIG_DIR"] = str(args.config_dir.resolve())
    if args.env_file:
        os.environ["SYNAXIS_ENV_FILE"] = str(args.env_file.resolve())
    try:
        from auth.admin_security import set_password
        from core.settings import Settings

        env_file = os.environ.get("SYNAXIS_ENV_FILE")
        settings = Settings(_env_file=env_file) if env_file else Settings()
        if bool(settings.tls_certfile) != bool(settings.tls_keyfile):
            raise ValueError("TLS certificate and key must be configured together")
        if args.check:
            print(f"Configuration valid / 配置有效：{settings.host}:{settings.port}")
            return 0
        if args.serve_worker:
            if os.environ.get("SYNAXIS_MANAGED_WORKER") != "1":
                raise ValueError("Internal worker must be started by main.py")
            return asyncio.run(serve(settings))
        with InstanceLock(settings.data_dir):
            if args.set_admin_password:
                set_password(settings)
                print("Administrator password saved / 管理员密码已保存。")
                return 0
            if args.initialization:
                backup = reset_databases(settings)
                if backup is None:
                    print("Initialization cancelled / 已取消初始化。")
                    return 0
                print(f"Backup / 备份：{backup}")
            return supervise()
    except (ValueError, RuntimeError, OSError) as exc:
        from pydantic import ValidationError

        if isinstance(exc, ValidationError):
            # Pydantic's default repr can include raw dotenv values.
            fields = [".".join(map(str, e["loc"])) for e in exc.errors(include_input=False)]
            print("Invalid configuration fields: " + ", ".join(fields), file=sys.stderr)
        else:
            print(str(exc), file=sys.stderr)
        return 2
