import asyncio
import contextlib
import json
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from typing import Annotated, Literal

import httpx
import jwt
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm

from api.admin_routes import register_admin_routes
from api.client_routes import (
    IdempotencyKey,
    accepted,
    expert_catalog,
    register_client_routes,
    require_capacity,
)
from api.docs import localized_schema, swagger_html
from api.middleware import BodyLimit
from auth.security import DUMMY_HASH, decode_token, load_jwt_secret, token_for, verify_password
from core.harness import Harness
from core.settings import Settings
from core.version import VERSION
from domain.client_models import AuthTokens, ExpertsResponse, Preferences
from domain.errors import HarnessError
from domain.schemas import AnswerRequest, APIError, SessionView, TutorRequest, UsagePage
from domain.views import session_view
from llm.router import RoutedClient
from storage.artifacts import ArtifactStore
from storage.client_store import StoreError
from storage.database import Database, dumps
from storage.retention import expire_artifacts
from tools.executor import SkillExecutor
from tools.pythonbox import sandbox_available
from tools.syllabus import SyllabusService

oauth = OAuth2PasswordBearer(tokenUrl="/v1/auth/token")


def create_app(settings: Settings | None = None, llm_factory=None, http_transport=None):
    settings = settings or Settings()
    db = Database(settings.db_path)
    syllabus = SyllabusService(settings)

    @asynccontextmanager
    async def lifespan(app):
        db.initialize()
        db.recover()
        await asyncio.to_thread(syllabus.initialize)
        app.state.secret = load_jwt_secret(settings)
        async with httpx.AsyncClient(
            timeout=settings.request_timeout, follow_redirects=False, transport=http_transport
        ) as http:
            llm = llm_factory(settings, db, http) if llm_factory else RoutedClient(settings, db, http)
            artifacts = ArtifactStore(settings, db)
            executor = SkillExecutor(settings, db, llm, http, artifacts, syllabus)
            app.state.harness = Harness(settings, db, llm, executor, artifacts)
            app.state.executor = executor
            app.state.artifacts = artifacts

            async def maintenance():
                while True:
                    await asyncio.to_thread(
                        db.compact_ledger, settings.retention_days, settings.retention_bytes
                    )
                    await asyncio.to_thread(db.cleanup_refresh_tokens)
                    await asyncio.to_thread(expire_artifacts, settings, db)
                    await asyncio.sleep(settings.maintenance_seconds)

            cleanup_task = asyncio.create_task(maintenance())
            try:
                yield
            finally:
                cleanup_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await cleanup_task
                await app.state.harness.shutdown()

    app = FastAPI(
        title="Synaxis Teaching API / 教学 API",
        version=VERSION,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        responses={
            401: {"model": APIError, "description": "Authentication required / 需要登录"},
            422: {"model": APIError, "description": "Invalid request / 请求参数无效"},
            402: {"model": APIError, "description": "Budget blocked / 额度或计费配置阻止调用"},
            404: {"model": APIError, "description": "Not found / 记录不存在"},
            409: {"model": APIError, "description": "State or idempotency conflict / 状态或幂等冲突"},
            410: {"model": APIError, "description": "Deleted session / 会话已删除"},
            429: {"model": APIError, "description": "Rate limited / 请求限流"},
            503: {"model": APIError, "description": "Temporarily unavailable / 服务暂不可用"},
        },
    )
    app.add_middleware(BodyLimit)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "Last-Event-ID"],
        expose_headers=["Idempotency-Replayed"],
    )
    app.state.db, app.state.settings = db, settings
    app.state.syllabus = syllabus
    register_admin_routes(app, settings, db)
    attempts = defaultdict(deque)

    def auth_limit(request):
        client = request.client.host if request.client else "unknown"
        timestamp = time.monotonic()
        if len(attempts) > 2048:
            for key in list(attempts):
                if not attempts[key] or attempts[key][-1] < timestamp - 60:
                    del attempts[key]
        window = attempts[client]
        while window and window[0] < timestamp - 60:
            window.popleft()
        if len(window) >= 10:
            raise HTTPException(429, "Try again after one minute / 请一分钟后再试")
        window.append(timestamp)

    @app.exception_handler(StoreError)
    async def store_error(request, exc):
        return JSONResponse({"code": exc.code, "detail": exc.code}, status_code=exc.status)

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return JSONResponse(
            {
                "code": {401: "UNAUTHORIZED", 404: "NOT_FOUND", 409: "CONFLICT", 429: "RATE_LIMITED"}.get(
                    exc.status_code, "REQUEST_FAILED"
                ),
                "detail": str(exc.detail),
            },
            status_code=exc.status_code,
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Validation output must not echo passwords or large image data.
        return JSONResponse({"code": "VALIDATION_ERROR", "detail": "Invalid request / 请求参数无效"}, 422)

    @app.exception_handler(HarnessError)
    async def harness_error(request, exc):
        return JSONResponse(
            {"code": exc.code, "detail": exc.detail},
            503 if exc.code == "SERVER_BUSY" or (exc.code.startswith("SYLLABUS_") and exc.code != "SYLLABUS_QUERY_TOO_LONG") else 402 if exc.code.startswith("BUDGET_") else 400,
        )

    async def current_user(token: Annotated[str, Depends(oauth)]):
        try:
            claims = decode_token(token, app.state.secret)
            user = db.user(user_id=int(claims["sub"]))
            if not user or not user["active"] or claims["ver"] != user["token_version"]:
                raise ValueError("Inactive token")
            user["jwt_expires_at"] = claims["exp"]
            return user
        except (jwt.PyJWTError, ValueError, TypeError, KeyError) as exc:
            raise HTTPException(
                401, "Invalid or expired token / 登录失效", headers={"WWW-Authenticate": "Bearer"}
            ) from exc

    User = Annotated[dict, Depends(current_user)]

    def owned(session_id, user):
        row = db.session(session_id, user["id"])
        if row is None:
            raise HTTPException(404, "Session not found / 会话不存在")
        return row

    @app.get("/health", tags=["System / 系统"], operation_id="health")
    async def health():
        return {
            "status": "ok",
            "version": VERSION,
            "syllabus": syllabus.info()["availability"],
            "sandbox": "configured" if sandbox_available(settings) else "unconfigured",
        }

    @app.post(
        "/v1/auth/token",
        response_model=AuthTokens,
        tags=["Auth / 鉴权"],
        operation_id="token",
        responses={429: {"model": APIError}},
    )
    async def login(request: Request, form: Annotated[OAuth2PasswordRequestForm, Depends()]):
        auth_limit(request)
        if len(form.password) > 1024 or len(form.username) > 100:
            raise HTTPException(401, "Invalid credentials / 用户名或密码错误")
        user = db.user(username=form.username)
        valid = await asyncio.to_thread(
            verify_password, user["password_hash"] if user else DUMMY_HASH, form.password
        )
        if not user or not valid or not user["active"]:
            raise HTTPException(401, "Invalid credentials / 用户名或密码错误")
        fresh_user = db.user(user_id=user["id"])
        if not fresh_user or not fresh_user["active"] or fresh_user["password_hash"] != user["password_hash"]:
            raise HTTPException(401, "Credentials changed / 登录信息已变更")
        user = fresh_user
        with db.connection() as c:
            refresh = db.issue_refresh(c, user, settings.refresh_days)
        return {
            "access_token": token_for(user, app.state.secret, settings.jwt_minutes),
            "token_type": "bearer",
            "expires_in": settings.jwt_minutes * 60,
            "refresh_token": refresh,
            "refresh_expires_in": settings.refresh_days * 86400,
        }

    @app.get("/v1/experts", response_model=ExpertsResponse, tags=["System / 系统"], operation_id="experts")
    async def experts(user: User):
        return expert_catalog(settings)

    @app.post(
        "/v1/sessions",
        response_model=SessionView,
        status_code=202,
        tags=["Tutor / 教学"],
        operation_id="create_session",
        responses={503: {"model": APIError}},
    )
    async def create_session(
        body: TutorRequest, user: User, response: Response, idempotency_key: IdempotencyKey = None
    ):
        def mutate(c):
            preferences = Preferences.model_validate(json.loads(user["preferences"])).model_dump()
            request = {
                **body.model_dump(),
                **{k: v for k, v in preferences.items() if k not in body.model_fields_set},
            }
            if request["subject"] == "chinese" and body.images:
                raise StoreError("EXPERT_IMAGES_UNSUPPORTED", 400)
            require_capacity(app)
            return db.insert_session(c, user["id"], request)

        result, replay = db.submit(
            user["id"], "create", idempotency_key, body.model_dump(exclude_unset=True), mutate
        )
        return accepted(app, result, replay, response)

    @app.get(
        "/v1/sessions/{session_id}",
        response_model=SessionView,
        tags=["Tutor / 教学"],
        operation_id="get_session",
        responses={404: {"model": APIError}},
    )
    async def get_session(session_id: str, user: User):
        return session_view(owned(session_id, user))

    @app.get(
        "/v1/sessions/{session_id}/events",
        tags=["Tutor / 教学"],
        operation_id="events",
        response_class=StreamingResponse,
        responses={
            200: {
                "description": "SSE teaching events / 教学事件流",
                "content": {
                    "text/event-stream": {
                        "schema": {"type": "string"},
                        "example": 'id: 2\nevent: presentation\ndata: {"version":"1.0","blocks":[{"type":"markdown","text":"解题提示"}]}\n\n',
                    }
                },
            }
        },
    )
    async def events(
        session_id: str,
        user: User,
        request: Request,
        after: int = Query(default=0, ge=0),
        last_event_id: Annotated[int | None, Header(ge=0)] = None,
    ):
        owned(session_id, user)
        cursor = max(after, last_event_id or 0)

        async def stream():
            nonlocal cursor
            heartbeat = time.monotonic()
            while True:
                if await request.is_disconnected():
                    return
                current = db.user(user_id=user["id"])
                if (
                    time.time() >= user["jwt_expires_at"]
                    or not current
                    or not current["active"]
                    or current["token_version"] != user["token_version"]
                ):
                    yield 'event: failed\ndata: {"code":"UNAUTHORIZED"}\n\n'
                    return
                batch = db.events(session_id, cursor)
                for event in batch:
                    cursor = event["id"]
                    yield f"id: {cursor}\nevent: {event['kind']}\ndata: {dumps(event['data'])}\n\n"
                row = db.session(session_id)
                if row is None:
                    yield 'event: failed\ndata: {"code":"NOT_FOUND"}\n\n'
                    return
                if len(batch) < 100 and row["status"] in ("waiting", "completed", "failed", "cancelled"):
                    # Recheck after reading state to catch events committed between the two reads.
                    if not db.events(session_id, cursor):
                        return
                if time.monotonic() - heartbeat >= 15:
                    yield ": keepalive\n\n"
                    heartbeat = time.monotonic()
                await asyncio.sleep(0.1)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post(
        "/v1/sessions/{session_id}/answers",
        response_model=SessionView,
        status_code=202,
        tags=["Tutor / 教学"],
        operation_id="answer",
        responses={409: {"model": APIError}},
    )
    async def answer(
        session_id: str,
        body: AnswerRequest,
        user: User,
        response: Response,
        idempotency_key: IdempotencyKey = None,
    ):
        def mutate(c):
            db.raw_session(c, session_id, user["id"])
            require_capacity(app, session_id)
            return db.append_input(c, session_id, user["id"], body.model_dump(), answer=True)

        result, replay = db.submit(
            user["id"], "answer:" + session_id, idempotency_key, body.model_dump(), mutate
        )
        return accepted(app, result, replay, response)

    @app.post(
        "/v1/sessions/{session_id}/cancel",
        response_model=SessionView,
        tags=["Tutor / 教学"],
        operation_id="cancel",
    )
    async def cancel(session_id: str, user: User):
        owned(session_id, user)
        app.state.harness.cancel(session_id)
        # Await cancellation so the response reflects its persisted terminal state.
        task = app.state.harness.tasks.get(session_id)
        if task:
            await asyncio.gather(task, return_exceptions=True)
        return session_view(db.session(session_id))

    @app.get("/v1/usage", response_model=UsagePage, tags=["Usage / 用量"], operation_id="usage")
    async def usage(
        user: User,
        before: int | None = Query(default=None, ge=1),
        limit: int = Query(default=50, ge=1, le=100),
        session_id: str | None = None,
    ):
        return db.usage(user["id"], before, limit, session_id)

    @app.get(
        "/v1/artifacts/{artifact_id}",
        response_class=FileResponse,
        tags=["Files / 文件"],
        operation_id="artifact",
        responses={
            200: {
                "description": "Authorized file / 授权文件",
                "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}},
            }
        },
    )
    async def artifact(artifact_id: str, user: User):
        row = db.artifact(artifact_id, user["id"])
        if row and (row.get("expired_at") or not (settings.data_dir / "artifacts" / row["id"]).is_file()):
            raise HTTPException(410, "ARTIFACT_EXPIRED")
        if row and not db.session(row["session_id"], user["id"]):
            row = None
        path = settings.data_dir / "artifacts" / row["id"] if row else None
        if not row or not path.is_file():
            raise HTTPException(404, "Artifact not found / 文件不存在")
        return FileResponse(
            path,
            media_type=row["mime"],
            filename=row["name"],
            headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"},
        )

    @app.get("/openapi.json", include_in_schema=False)
    async def openapi(lang: Literal["zh", "en"] = "zh"):
        return localized_schema(app.openapi(), lang)

    @app.get("/docs", include_in_schema=False, response_class=HTMLResponse)
    async def docs(lang: Literal["zh", "en"] = "zh"):
        return swagger_html(lang)

    register_client_routes(app, settings, db, current_user, auth_limit)
    return app


app = create_app()
