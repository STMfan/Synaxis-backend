import asyncio
import base64
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response

from api.errors import error_catalog
from auth.security import hash_password, token_for, verify_password
from core.configuration import MODELS, STAGES
from core.prompts import EXPERTS
from core.version import VERSION
from domain.client_models import (
    AuthTokens,
    BudgetView,
    Capabilities,
    ErrorCatalog,
    EventPage,
    MessagePage,
    MessageRequest,
    PasswordChange,
    Preferences,
    Profile,
    RefreshRequest,
    SessionPage,
    SessionPatch,
    SessionStatus,
    UsageSummary,
)
from domain.schemas import SessionView, Subject
from domain.syllabus import SyllabusQuery, SyllabusResult, SyllabusSection
from domain.views import session_view
from llm.providers import TextProvider, key_for, provider_for, supports_max
from storage.client_store import StoreError
from storage.pricing import pricing_info
from tools.executor import DESCRIPTIONS
from tools.pythonbox import sandbox_available

IdempotencyKey = Annotated[
    str | None, Header(alias="Idempotency-Key", min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_.:-]+$")
]


def expert_catalog(settings=None):
    return {
        "experts": [
            {
                "name": e.name,
                "translation": e.translation,
                "subjects": e.subjects,
                "skills": e.exskills,
                "effort": e.effort,
                "allow_max_thinking": e.allow_max_thinking
                and (settings is None or supports_max(settings, e.model_role)),
                "images": e.images
                and (settings is None or provider_for(settings, e.model_role)[1].supports_images),
            }
            for e in EXPERTS.values()
        ],
        "skills": DESCRIPTIONS,
    }


def require_capacity(app, sid=None):
    harness = app.state.harness
    harness.ensure_capacity()
    if sid and sid in harness.tasks:
        raise StoreError("SESSION_BUSY")


def accepted(app, result, replay, response):
    if not replay:
        app.state.harness.start(result["id"])
    response.headers["Idempotency-Replayed"] = "true" if replay else "false"
    return result


def register_client_routes(app: FastAPI, settings, db, current_user, auth_limit):
    User = Annotated[dict, Depends(current_user)]

    @app.get(
        "/v1/error-codes", response_model=ErrorCatalog, tags=["System / 系统"], operation_id="error_codes"
    )
    async def error_codes(user: User):
        return error_catalog()

    def owned(sid, user):
        row = db.session(sid, user["id"])
        if not row:
            raise StoreError("NOT_FOUND", 404)
        return row

    def tokens(user, refresh):
        return {
            "access_token": token_for(user, app.state.secret, settings.jwt_minutes),
            "token_type": "bearer",
            "expires_in": settings.jwt_minutes * 60,
            "refresh_token": refresh,
            "refresh_expires_in": settings.refresh_days * 86400,
        }

    @app.post("/v1/auth/refresh", response_model=AuthTokens, tags=["Auth / 鉴权"], operation_id="refresh")
    async def refresh(body: RefreshRequest, request: Request):
        auth_limit(request)
        user, refresh_token = db.rotate_refresh(body.refresh_token, settings.refresh_days)
        return tokens(user, refresh_token)

    @app.post("/v1/auth/logout", status_code=204, tags=["Auth / 鉴权"], operation_id="logout")
    async def logout(user: User):
        db.revoke_auth(user["id"])
        return Response(status_code=204)

    @app.get("/v1/me", response_model=Profile, tags=["Account / 账号"], operation_id="me")
    async def me(user: User):
        return {k: user[k] for k in ("id", "username", "created_at")} | {
            "preferences": json.loads(user["preferences"])
        }

    @app.get(
        "/v1/me/preferences",
        response_model=Preferences,
        tags=["Account / 账号"],
        operation_id="get_preferences",
    )
    async def get_preferences(user: User):
        return json.loads(user["preferences"])

    @app.put(
        "/v1/me/preferences",
        response_model=Preferences,
        tags=["Account / 账号"],
        operation_id="set_preferences",
    )
    async def set_preferences(body: Preferences, user: User):
        db.set_preferences(user["id"], body.model_dump())
        return body

    @app.post("/v1/me/password", status_code=204, tags=["Account / 账号"], operation_id="change_password")
    async def change_password(body: PasswordChange, user: User, request: Request):
        auth_limit(request)
        if not await asyncio.to_thread(verify_password, user["password_hash"], body.current_password):
            raise StoreError("INVALID_PASSWORD", 401)
        encoded = await asyncio.to_thread(hash_password, body.new_password)
        db.revoke_auth(user["id"], user["password_hash"], encoded)
        return Response(status_code=204)

    @app.get(
        "/v1/sessions", response_model=SessionPage, tags=["History / 历史"], operation_id="list_sessions"
    )
    async def list_sessions(
        user: User,
        limit: int = Query(default=20, ge=1, le=100),
        cursor: str | None = Query(default=None, max_length=1000),
        subject: Subject | None = None,
        status: SessionStatus | None = None,
        q: str | None = Query(default=None, max_length=100),
        archived: Literal["exclude", "only", "all"] = "exclude",
        pinned: bool | None = None,
    ):
        return db.list_sessions(user["id"], limit, cursor, subject, status, q, archived, pinned)

    @app.patch(
        "/v1/sessions/{session_id}",
        response_model=SessionView,
        tags=["History / 历史"],
        operation_id="patch_session",
    )
    async def patch_session(session_id: str, body: SessionPatch, user: User):
        return session_view(db.patch_session(session_id, user["id"], body.model_dump(exclude_unset=True)))

    @app.delete(
        "/v1/sessions/{session_id}", status_code=204, tags=["History / 历史"], operation_id="delete_session"
    )
    async def delete_session(session_id: str, user: User):
        with db.connection() as c:
            row = db.raw_session(c, session_id, user["id"], include_deleted=True)
        if row["deleted_at"]:
            return Response(status_code=204)
        app.state.harness.cancel(session_id)
        task = app.state.harness.tasks.get(session_id)
        if task:
            await asyncio.gather(task, return_exceptions=True)
        db.delete_session(session_id, user["id"])
        return Response(status_code=204)

    @app.get(
        "/v1/sessions/{session_id}/messages",
        response_model=MessagePage,
        tags=["History / 历史"],
        operation_id="messages",
    )
    async def messages(
        session_id: str,
        user: User,
        after: int = Query(default=0, ge=0),
        limit: int = Query(default=20, ge=1, le=100),
    ):
        owned(session_id, user)
        return db.messages_page(session_id, after, limit)

    @app.post(
        "/v1/sessions/{session_id}/messages",
        response_model=SessionView,
        status_code=202,
        tags=["Tutor / 教学"],
        operation_id="send_message",
    )
    async def send_message(
        session_id: str,
        body: MessageRequest,
        user: User,
        response: Response,
        idempotency_key: IdempotencyKey = None,
    ):
        def mutate(c):
            row = db.raw_session(c, session_id, user["id"])
            plan = row["state"]["plan"]
            if body.images and ((plan and not EXPERTS[plan["expert"]].images) or row["subject"] == "chinese"):
                raise StoreError("EXPERT_IMAGES_UNSUPPORTED", 400)
            require_capacity(app, session_id)
            return db.append_input(c, session_id, user["id"], body.model_dump())

        result, replay = db.submit(
            user["id"], "message:" + session_id, idempotency_key, body.model_dump(), mutate
        )
        return accepted(app, result, replay, response)

    @app.get(
        "/v1/sessions/{session_id}/messages/{message_id}/images/{image_index}",
        tags=["Files / 文件"],
        operation_id="message_image",
        response_class=Response,
        responses={
            200: {
                "content": {
                    "image/png": {"schema": {"type": "string", "format": "binary"}},
                    "image/jpeg": {"schema": {"type": "string", "format": "binary"}},
                }
            }
        },
    )
    async def message_image(session_id: str, message_id: int, image_index: int, user: User):
        owned(session_id, user)
        with db.connection() as c:
            row = c.execute(
                "SELECT images FROM public_messages WHERE id=? AND session_id=?", (message_id, session_id)
            ).fetchone()
        images = json.loads(row["images"]) if row else []
        if image_index < 0 or image_index >= len(images):
            raise StoreError("NOT_FOUND", 404)
        image = images[image_index]
        return Response(
            base64.b64decode(image["data"]),
            media_type=image["media_type"],
            headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"},
        )

    @app.get(
        "/v1/sessions/{session_id}/events/history",
        response_model=EventPage,
        tags=["History / 历史"],
        operation_id="event_history",
    )
    async def event_history(
        session_id: str,
        user: User,
        after: int = Query(default=0, ge=0),
        limit: int = Query(default=50, ge=1, le=100),
    ):
        owned(session_id, user)
        return db.events_page(session_id, after, limit)

    @app.get(
        "/v1/sessions/{session_id}/budget",
        response_model=BudgetView,
        tags=["Usage / 用量"],
        operation_id="budget",
    )
    async def budget(session_id: str, user: User):
        owned(session_id, user)
        return db.budget_snapshot(session_id, user["id"], settings)

    @app.get(
        "/v1/usage/summary", response_model=UsageSummary, tags=["Usage / 用量"], operation_id="usage_summary"
    )
    async def usage_summary(
        user: User,
        group_by: Literal["day", "session", "total"] = "day",
        date_from: date | None = None,
        date_to: date | None = None,
        session_id: str | None = None,
        limit: int = Query(default=20, ge=1, le=100),
        cursor: str | None = Query(default=None, max_length=1000),
    ):
        end = date_to or datetime.now(UTC).date()
        start = date_from or end - timedelta(days=29)
        if end < start or (end - start).days > 365:
            raise StoreError("INVALID_DATE_RANGE", 422)
        return await asyncio.to_thread(
            db.usage_summary,
            user["id"],
            group_by,
            start.isoformat(),
            end.isoformat(),
            session_id,
            limit,
            cursor,
        )

    @app.get(
        "/v1/capabilities", response_model=Capabilities, tags=["System / 系统"], operation_id="capabilities"
    )
    async def capabilities(user: User):
        configured = bool(
            settings.deepseek_key.get_secret_value()
            or (key_for(settings, "main") if settings.main_provider == "deepseek" else "")
        )
        stage_models = {}
        for name, stage in STAGES.items():
            role = "process" if name == "Formatter" else stage.model_role
            provider_name, provider = provider_for(settings, role)
            model = getattr(settings, role + "_model")
            if name == "Search":
                provider_name, provider, model = (
                    "deepseek",
                    TextProvider(protocol="deepseek_anthropic", base_url=settings.deepseek_base),
                    settings.search_model,
                )
            native = provider.protocol == "deepseek_anthropic"
            output_limits = MODELS.model_output_limits if native else provider.output_limits
            contexts = MODELS.model_context_limits if native else provider.context_limits
            key = configured if name == "Search" else key_for(settings, role)
            stage_models[name] = {
                "provider": provider_name,
                "model": model,
                "context_limit": contexts.get(model),
                "max_output_tokens": min(
                    settings.max_tokens,
                    stage.max_tokens or settings.max_tokens,
                    output_limits.get(model, settings.max_tokens),
                ),
                "effort": stage.effort,
                "consumption_excluded": provider.excluded_from_budget,
                "availability": "configured"
                if key and model in contexts and model in output_limits
                else "unavailable",
            }
        sandbox = "configured" if sandbox_available(settings) else "unavailable"
        render = (
            "disabled"
            if not settings.render_enabled
            else "ready"
            if settings.browser_path and Path(settings.browser_path).is_file()
            else "configured"
        )
        image_ready = bool(settings.image_base and settings.image_key.get_secret_value())
        return {
            "version": VERSION,
            "experts": expert_catalog(settings)["experts"],
            "skills": [
                {"name": "Search", "availability": "configured" if configured else "unavailable"},
                {"name": "AssistantOnWeb", "availability": "configured" if configured else "unavailable"},
                {"name": "Syllabus", **{key: app.state.syllabus.info()[key] for key in ("availability", "reason")}},
                {"name": "GPTImage", "availability": "configured" if image_ready else "unavailable"},
                {"name": "PythonBox", "availability": sandbox},
                {"name": "Render", "availability": render},
            ],
            "image_price": {
                "model": settings.image_model,
                "credits": settings.image_credit,
                "cost": str(Decimal(settings.image_credit) / 100),
                "currency": "CNY",
                "requires_explicit_consent": settings.image_credit > 0,
            },
            "max_steps": settings.max_steps,
            "max_tool_calls": settings.max_tool_calls,
            "max_output_tokens": settings.max_tokens,
            "stage_max_output_tokens": {
                name: info["max_output_tokens"] for name, info in stage_models.items()
            },
            "stage_efforts": {name: stage.effort for name, stage in STAGES.items()},
            "stage_models": stage_models,
            "expert_max_output_tokens": settings.expert_max_tokens,
            "planner_max_experts": STAGES["Planner"].max_experts,
            "session_budget_cny": str(settings.session_budget_cny),
            "pricing": pricing_info(settings),
            "syllabus": app.state.syllabus.info(),
        }

    @app.post(
        "/v1/syllabus/search", response_model=SyllabusResult,
        tags=["Syllabus / 课标"], operation_id="search_syllabus",
    )
    async def search_syllabus(body: SyllabusQuery, user: User):
        result = await app.state.syllabus.search(body)
        if result["status"] != "ok":
            raise HTTPException(503, result["code"])
        return result

    @app.get(
        "/v1/syllabus/sections/{section_id}", response_model=SyllabusSection,
        tags=["Syllabus / 课标"], operation_id="syllabus_section",
    )
    async def syllabus_section(section_id: str, user: User):
        if app.state.syllabus.index is None:
            raise HTTPException(503, app.state.syllabus.reason)
        result = app.state.syllabus.section(section_id)
        if result is None:
            raise HTTPException(404, "Syllabus section not found / 课标章节不存在")
        return result
