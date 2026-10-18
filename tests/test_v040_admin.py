import asyncio
import json
import shutil

import pytest
from pydantic import SecretStr

from auth.security import hash_password, verify_password
from core.runtime_config import CONFIG_ROOT
from domain.schemas import TutorRequest
from storage.admin_store import create_user, list_users, update_user
from storage.client_store import StoreError
from storage.database import Database

PASSWORD = "synthetic-admin-password"


async def admin_login(client):
    client.headers["Origin"] = "https://test"
    response = await client.post("/admin/api/login", json={"password": PASSWORD})
    assert response.status_code == 200
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    return response


@pytest.fixture
def admin_settings(settings, tmp_path):
    directory = tmp_path / "config"
    shutil.copytree(CONFIG_ROOT, directory)
    settings.config_dir = directory
    settings.admin_password_hash = SecretStr(hash_password(PASSWORD))
    return settings


async def test_admin_cookie_crud_and_student_token_separation(app_factory, admin_settings):
    async with app_factory() as (app, client, _, _):
        client.base_url = "https://test"
        assert (await client.get("/admin/api/users")).status_code == 401
        response = await admin_login(client)
        cookie = response.headers["set-cookie"]
        assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=strict" in cookie
        created = await client.post(
            "/admin/api/users", json={"username": "new-student", "password": "synthetic-student-password"}
        )
        assert created.status_code == 201
        user = created.json()
        assert "password" not in json.dumps(user) and "token_version" not in user
        assert verify_password(
            app.state.db.user(user_id=user["id"])["password_hash"], "synthetic-student-password"
        )
        assert (
            await client.post(
                "/admin/api/users", json={"username": "new-student", "password": "synthetic-student-password"}
            )
        ).status_code == 409
        assert (
            await client.patch(
                f"/admin/api/users/{user['id']}", json={"username": "changed", "active": False}
            )
        ).status_code == 200
        assert app.state.db.user(user_id=user["id"])["token_version"] == 1
        assert (await client.get("/admin/api/users", params={"q": "changed", "limit": 1})).json()["items"][0][
            "active"
        ] == 0
        assert (await client.delete(f"/admin/api/users/{user['id']}")).status_code == 204
        assert not app.state.db.user(username="changed")
        assert (await client.delete(f"/admin/api/users/{user['id']}")).status_code == 404
        again = await client.post(
            "/admin/api/users", json={"username": "changed", "password": "synthetic-student-password"}
        )
        assert again.json()["id"] > user["id"]
        assert (await client.post("/admin/api/logout")).status_code == 200
        assert (await client.get("/admin/api/users")).status_code == 401


async def test_admin_csrf_origin_expiry_and_password_change(app_factory, admin_settings, monkeypatch):
    async with app_factory() as (app, client, _, _):
        client.base_url = "https://test"
        await admin_login(client)
        client.headers.pop("X-CSRF-Token")
        assert (await client.post("/admin/api/restart")).status_code == 403
        csrf = (await client.get("/admin/api/session")).json()["csrf_token"]
        client.headers["X-CSRF-Token"] = csrf
        client.headers["Origin"] = "https://evil.test"
        assert (await client.post("/admin/api/restart")).status_code == 403
        client.headers["Origin"] = "https://test"
        app.state.settings.admin_password_hash = SecretStr(hash_password("changed-synthetic-password"))
        assert (await client.get("/admin/api/session")).status_code == 401
        app.state.settings.admin_password_hash = SecretStr(hash_password(PASSWORD))
        await admin_login(client)
        import api.admin_routes as module

        clock = module.time.monotonic()
        monkeypatch.setattr(module.time, "monotonic", lambda: clock + 10000)
        assert (await client.get("/admin/api/users")).status_code == 401


async def test_admin_tls_boundary_unconfigured_password_and_login_rate(app_factory, admin_settings):
    async with app_factory() as (app, client, _, _):
        # ASGITransport reports loopback but the external Host is not loopback.
        assert (await client.get("/admin")).status_code == 403
        client.headers["X-Forwarded-Proto"] = "https"
        client.headers["X-Forwarded-For"] = "127.0.0.1"
        assert (await client.get("/admin")).status_code == 403
        client.base_url = "https://test"
        app.state.settings.admin_password_hash = SecretStr("")
        client.headers["Origin"] = "https://test"
        for _ in range(5):
            assert (await client.post("/admin/api/login", json={"password": PASSWORD})).status_code == 401
        assert (await client.post("/admin/api/login", json={"password": PASSWORD})).status_code == 429


async def test_admin_assets_network_and_restart(app_factory, admin_settings):
    async with app_factory() as (app, client, _, _):
        client.base_url = "https://test"
        panel = await client.get("/admin")
        assert panel.status_code == 200 and panel.headers["cache-control"] == "no-store"
        assert "frame-ancestors 'none'" in panel.headers["content-security-policy"]
        assert (await client.get("/admin/assets/admin.js")).status_code == 200
        assert (await client.get("/admin/assets/unknown")).status_code == 404
        await admin_login(client)
        saved = (await client.get("/admin/api/network")).json()["saved"]
        saved["cors_origins"] = ["https://student.example"]
        assert (await client.put("/admin/api/network", json=saved)).status_code == 200
        value = (await client.get("/admin/api/network")).json()
        assert value["restart_required"] and value["saved"]["cors_origins"] == ["https://student.example"]
        assert value["effective"]["cors_origins"] != value["saved"]["cors_origins"]
        saved["cors_origins"] = ["*"]
        assert (await client.put("/admin/api/network", json=saved)).status_code == 422
        assert (await client.post("/admin/api/restart")).status_code == 503
        restarted = []
        app.state.restart_callback = lambda: restarted.append(True)
        app.state.harness.tasks["busy"] = asyncio.current_task()
        assert (await client.post("/admin/api/restart")).status_code == 409
        app.state.harness.tasks.pop("busy")
        assert (await client.post("/admin/api/restart")).status_code == 202
        assert restarted and not app.state.harness.accepting
        assert (await client.post("/v1/sessions", json={"message": "new"})).status_code == 503


def test_user_management_preserves_budgets_and_revokes_refresh(admin_settings):
    db = Database(admin_settings.db_path)
    db.initialize()
    encoded = hash_password(PASSWORD)
    alice = create_user(db, "alice", encoded)
    bob = create_user(db, "bob", encoded)
    assert list_users(db, limit=1)["next_after"] == alice["id"]
    assert list_users(db, after=alice["id"])["items"][0]["id"] == bob["id"]
    sid = db.create_session(alice["id"], TutorRequest(message="test").model_dump())
    with pytest.raises(StoreError, match="USER_BUSY"):
        update_user(db, alice["id"], {}, delete=True)
    assert db.user(user_id=alice["id"])["active"]
    db.save_session(sid, "completed", db.session(sid)["state"])
    budget = db.budget_snapshot(sid, alice["id"], admin_settings)
    update_user(db, alice["id"], {"password_hash": hash_password("new-synthetic-password")})
    assert db.user(user_id=alice["id"])["token_version"] == 1
    update_user(db, alice["id"], {}, delete=True)
    assert db.session(sid) and db.budget_snapshot(sid, alice["id"], admin_settings) == budget
