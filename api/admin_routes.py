"""Small same-origin admin surface: no student token grants management access."""

import asyncio
import hashlib
import ipaddress
import secrets
import time
from collections import defaultdict, deque
from pathlib import Path

from fastapi import Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import Field, SecretStr
from starlette.datastructures import MutableHeaders

from auth.admin_security import password_hash
from auth.security import DUMMY_HASH, hash_password, verify_password
from core.runtime_config import NetworkConfig, save_network
from domain.schemas import StrictModel
from storage.admin_store import create_user, list_users, update_user

STATIC = Path(__file__).resolve().parents[1] / "admin"
COOKIE = "synaxis_admin"


class AdminLogin(StrictModel):
    password: SecretStr = Field(min_length=1, max_length=1024)


class UserCreate(StrictModel):
    username: str = Field(min_length=1, max_length=64, pattern=r"^[\w.-]+$")
    password: SecretStr = Field(min_length=12, max_length=1024)


class UserPatch(StrictModel):
    username: str | None = Field(default=None, min_length=1, max_length=64, pattern=r"^[\w.-]+$")
    password: SecretStr | None = Field(default=None, min_length=12, max_length=1024)
    active: bool | None = None


def loopback(host):
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class AdminBoundary:
    """Pure ASGI: ordinary API streams and disconnect messages pass through unchanged."""

    headers = {
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
        "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    }

    def __init__(self, app, settings):
        self.app, self.settings = app, settings

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] != "http" or not (path == "/admin" or path.startswith("/admin/")):
            return await self.app(scope, receive, send)
        request = Request(scope)
        host = request.client.host if request.client else ""
        local = loopback(host) and loopback(request.url.hostname or "")
        code, status = None, 403
        if not self.settings.admin_enabled or (not self.settings.admin_allow_lan and not local):
            code, status = "ADMIN_DISABLED", 404
        elif request.url.scheme != "https" and not local:
            code = "ADMIN_HTTPS_REQUIRED"
        elif request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get("origin") != str(
            request.base_url
        ).rstrip("/"):
            code = "ADMIN_ORIGIN_REJECTED"
        if code:
            return await JSONResponse({"code": code}, status, headers=self.headers)(scope, receive, send)

        async def secure_send(message):
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message).update(self.headers)
            await send(message)

        await self.app(scope, receive, secure_send)


def register_admin_routes(app, settings, db):
    sessions, attempts = {}, defaultdict(deque)
    hash_gate = asyncio.Semaphore(2)
    app.state.restart_callback = None
    app.add_middleware(AdminBoundary, settings=settings)

    async def password_work(function, *args):
        async with hash_gate:
            return await asyncio.to_thread(function, *args)

    def authenticated(request: Request):
        token = request.cookies.get(COOKIE, "")
        session = sessions.get(hashlib.sha256(token.encode()).hexdigest())
        if (
            not session
            or session["expires"] <= time.monotonic()
            or session["hash"] != password_hash(settings)
        ):
            raise HTTPException(401, "ADMIN_UNAUTHORIZED")
        if request.method not in ("GET", "HEAD") and not secrets.compare_digest(
            request.headers.get("X-CSRF-Token", "").encode(), session["csrf"].encode()
        ):
            raise HTTPException(403, "ADMIN_CSRF_REJECTED")
        return session

    @app.get("/admin", include_in_schema=False)
    @app.get("/admin/", include_in_schema=False)
    async def panel():
        return FileResponse(STATIC / "index.html")

    @app.get("/admin/assets/{name}", include_in_schema=False)
    async def asset(name: str):
        if name not in ("admin.js", "admin.css"):
            raise HTTPException(404)
        return FileResponse(STATIC / name)

    @app.post("/admin/api/login", include_in_schema=False)
    async def login(value: AdminLogin, request: Request, response: Response):
        clock = time.monotonic()
        host = request.client.host if request.client else "unknown"
        # Cap keys and prune expired entries; attacker-chosen addresses cannot
        # grow a permanent rate-limit map.
        for key in list(attempts):
            while attempts[key] and attempts[key][0] < clock - 60:
                attempts[key].popleft()
            if not attempts[key]:
                del attempts[key]
        if len(attempts) >= 2048 or len(attempts[host]) >= 5:
            raise HTTPException(429, "ADMIN_RATE_LIMITED")
        attempts[host].append(clock)
        encoded = password_hash(settings)
        valid = await password_work(verify_password, encoded or DUMMY_HASH, value.password.get_secret_value())
        if not encoded or not valid:
            raise HTTPException(401, "ADMIN_UNAUTHORIZED")
        for key in list(sessions):
            if sessions[key]["expires"] <= clock:
                del sessions[key]
        if len(sessions) >= 64:
            sessions.pop(next(iter(sessions)))
        token, csrf = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
        sessions[hashlib.sha256(token.encode()).hexdigest()] = {
            "expires": clock + settings.admin_session_minutes * 60,
            "csrf": csrf,
            "hash": encoded,
        }
        response.set_cookie(
            COOKIE,
            token,
            max_age=settings.admin_session_minutes * 60,
            httponly=True,
            secure=request.url.scheme == "https",
            samesite="strict",
            path="/admin",
        )
        return {"csrf_token": csrf}

    @app.get("/admin/api/session", include_in_schema=False)
    async def session(value=Depends(authenticated)):
        return {"csrf_token": value["csrf"]}

    @app.post("/admin/api/logout", include_in_schema=False)
    async def logout(request: Request, response: Response, value=Depends(authenticated)):
        sessions.pop(hashlib.sha256(request.cookies[COOKIE].encode()).hexdigest(), None)
        response.delete_cookie(COOKIE, path="/admin")
        return {"ok": True}

    @app.get("/admin/api/users", include_in_schema=False)
    async def users(
        q: str = Query(default="", max_length=64),
        after: int = Query(default=0, ge=0),
        limit: int = Query(default=50, ge=1, le=100),
        value=Depends(authenticated),
    ):
        return await asyncio.to_thread(list_users, db, q, after, limit)

    @app.post("/admin/api/users", status_code=201, include_in_schema=False)
    async def add_user(body: UserCreate, value=Depends(authenticated)):
        encoded = await password_work(hash_password, body.password.get_secret_value())
        return await asyncio.to_thread(create_user, db, body.username, encoded)

    @app.patch("/admin/api/users/{user_id}", include_in_schema=False)
    async def patch_user(user_id: int, body: UserPatch, value=Depends(authenticated)):
        fields = body.model_dump(exclude_none=True, exclude={"password"})
        if body.password is not None:
            fields["password_hash"] = await password_work(hash_password, body.password.get_secret_value())
        return await asyncio.to_thread(update_user, db, user_id, fields)

    @app.delete("/admin/api/users/{user_id}", status_code=204, include_in_schema=False)
    async def delete_user(user_id: int, value=Depends(authenticated)):
        await asyncio.to_thread(update_user, db, user_id, {}, delete=True)
        return Response(status_code=204)

    @app.get("/admin/api/network", include_in_schema=False)
    async def network(value=Depends(authenticated)):
        path = settings.config_dir / "runtime/network.json"
        saved = NetworkConfig.model_validate_json(path.read_text(encoding="utf-8"))
        effective = {key: getattr(settings, key) for key in NetworkConfig.model_fields}
        return {
            "saved": saved.model_dump(),
            "effective": effective,
            "restart_required": saved.model_dump() != effective,
        }

    @app.put("/admin/api/network", include_in_schema=False)
    async def put_network(body: NetworkConfig, value=Depends(authenticated)):
        try:
            saved = await asyncio.to_thread(save_network, settings.config_dir, body)
        except (OSError, ValueError) as exc:
            raise HTTPException(422, "NETWORK_CONFIGURATION_INVALID") from exc
        return {"saved": saved.model_dump(), "restart_required": True}

    @app.post("/admin/api/restart", status_code=202, include_in_schema=False)
    async def restart(value=Depends(authenticated)):
        if app.state.harness.tasks:
            raise HTTPException(409, "ACTIVE_TEACHING_REQUESTS")
        if app.state.restart_callback is None:
            raise HTTPException(503, "MANAGED_LAUNCHER_REQUIRED")
        app.state.harness.accepting = False
        app.state.restart_callback()
        return {"restarting": True, "login_again": True}
