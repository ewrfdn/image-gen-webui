from datetime import timedelta
import hashlib

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from cryptography.fernet import Fernet

from platform_service.db import LoginSession, User, now
from platform_service.app import create_app
from platform_service.config import Config
from test_platform import drain, setup  # noqa: F401 -- shared pytest fixture


async def account(api, username, role="user"):
    response = await api.post(
        "/api/admin/users",
        json={"username": username, "password": "test-password-123", "role": role},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def login(api, username, password="test-password-123"):
    client = httpx.AsyncClient(transport=api._transport, base_url="http://platform")
    response = await client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200, response.text
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=strict" in response.headers["set-cookie"]
    return client


@pytest.mark.asyncio
async def test_user_platform_generation_and_isolation(setup):
    p, api, _ = setup
    await account(api, "alice")
    await account(api, "bobby")
    alice = await login(api, "alice")
    bob = await login(api, "bobby")
    try:
        for method, path in [
            ("GET", "/api/admin/users"),
            ("POST", "/api/admin/keys"),
            ("GET", "/api/admin/backends"),
            ("GET", "/v1/models"),
            ("POST", "/v1/images/generations"),
            ("POST", "/v1/images/edits"),
        ]:
            assert (await alice.request(method, path, json={})).status_code == 403
        assert (await alice.post("/api/auth/register", json={})).status_code in (
            404,
            405,
        )
        task = (
            await alice.post(
                "/api/tasks", json={"model": "test-image", "prompt": "a cabin"}
            )
        ).json()["id"]
        await drain(p)
        result = (await alice.get("/api/tasks/" + task)).json()
        assert result["status"] == "succeeded"
        assert (await alice.get(result["files"][0]["url"])).status_code == 200
        assert (await bob.get("/api/tasks/" + task)).status_code == 404
        assert (await bob.get(result["files"][0]["url"])).status_code == 404
        assert (await bob.get("/api/tasks")).json()["tasks"] == []
        assert (
            await alice.post(
                "/api/tasks", headers={"Origin": "https://evil.example"}, json={}
            )
        ).status_code == 403
        assert (await alice.post("/api/auth/logout")).status_code == 200
        assert (await alice.get("/api/session")).status_code == 401
    finally:
        await alice.aclose()
        await bob.aclose()


@pytest.mark.asyncio
async def test_admin_keys_ownership_and_demotion(setup):
    _, api, _ = setup
    row = await account(api, "manager", "admin")
    await account(api, "manager2", "admin")
    manager = await login(api, "manager")
    other = await login(api, "manager2")
    try:
        assert (await manager.post("/api/admin/users", json={})).status_code == 403
        key = (
            await manager.post("/api/admin/keys", json={"name": "manager-key"})
        ).json()
        assert (await other.get("/api/admin/keys")).json()["keys"] == []
        assert (
            await other.patch("/api/admin/keys/" + key["id"], json={"revoked": True})
        ).status_code == 404
        headers = {"Authorization": "Bearer " + key["key"]}
        assert (await api.get("/v1/models", headers=headers)).status_code == 200
        assert (
            await api.post("/api/admin/users", headers=headers, json={})
        ).status_code == 403
        assert (
            await api.patch("/api/admin/users/" + row["id"], json={"role": "user"})
        ).status_code == 200
        assert (await api.get("/v1/models", headers=headers)).status_code == 401
        assert (
            await manager.post("/api/admin/keys", json={"name": "blocked"})
        ).status_code == 403
        assert (await manager.get("/api/session")).json()["role"] == "user"
        assert (await manager.get("/v1/models")).status_code == 403
    finally:
        await manager.aclose()
        await other.aclose()


@pytest.mark.asyncio
async def test_super_admin_protection_password_and_disable(setup):
    p, api, _ = setup
    root = (await api.get("/api/admin/users")).json()["users"][0]
    for change in ({"enabled": False}, {"role": "admin"}):
        assert (
            await api.patch("/api/admin/users/" + root["id"], json=change)
        ).status_code == 409
    user = await account(api, "normal")
    first = await login(api, "normal")
    second = await login(api, "normal")
    try:
        assert (
            await first.post(
                "/api/auth/password",
                json={
                    "current_password": "test-password-123",
                    "password": "new-password-123",
                },
            )
        ).status_code == 200
        assert (await second.get("/api/session")).status_code == 401
        third = await login(api, "normal", "new-password-123")
        try:
            assert (
                await api.patch(
                    "/api/admin/users/" + user["id"], json={"enabled": False}
                )
            ).status_code == 200
            assert (await third.get("/api/session")).status_code == 401
        finally:
            await third.aclose()
        async with p.db.sessions() as db:
            row = await db.get(User, user["id"])
            assert "new-password-123" not in row.password_hash
            assert not (
                await db.scalars(
                    select(LoginSession).where(LoginSession.user_id == user["id"])
                )
            ).all()
    finally:
        await first.aclose()
        await second.aclose()


@pytest.mark.asyncio
async def test_expiry_invalid_payload_and_bootstrap_rotation(setup):
    p, api, _ = setup
    user = await account(api, "expire")
    client = await login(api, "expire")
    try:
        async with p.db.sessions() as db:
            session = await db.scalar(
                select(LoginSession).where(LoginSession.user_id == user["id"])
            )
            session.expires_at = now() - timedelta(seconds=1)
            await db.commit()
        assert (await client.get("/api/session")).status_code == 401
        assert (await api.post("/api/auth/login", json=[])).status_code == 400
        assert (
            await api.post(
                "/api/admin/users",
                json={"username": "bad", "password": "test-password-123", "role": []},
            )
        ).status_code == 400
        root = await login(api, "admin", "a" * 32)
        try:
            assert (
                await root.post(
                    "/api/auth/password",
                    json={
                        "current_password": "a" * 32,
                        "password": "root-password-123",
                    },
                )
            ).status_code == 200
            assert (await api.get("/api/admin/users")).status_code == 401
        finally:
            await root.aclose()
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_existing_key_table_upgrade(tmp_path):
    url = "sqlite+aiosqlite:///" + str(tmp_path / "legacy.sqlite")
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE api_keys (id VARCHAR(36) PRIMARY KEY, name VARCHAR(128), prefix VARCHAR(24), key_hash VARCHAR(64), enabled BOOLEAN, revoked BOOLEAN, created_at DATETIME, last_used_at DATETIME)"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO api_keys VALUES ('old-key','old-key','sk-frame-old',:hash,1,0,'2026-01-01',NULL)"
            ),
            {"hash": hashlib.sha256(b"legacy-key").hexdigest()},
        )
    await engine.dispose()
    app = create_app(
        Config(url, "a" * 32, Fernet.generate_key().decode(), tmp_path / "data", False)
    )
    p = app.state.platform
    await p.start()
    await app.state.accounts.initialize()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://platform",
            headers={"Authorization": "Bearer legacy-key"},
        ) as client:
            assert (await client.get("/v1/models")).status_code == 200
        async with p.db.sessions() as db:
            from platform_service.db import ApiKey

            key = await db.get(ApiKey, "old-key")
            assert key.user_id and await db.get(User, key.user_id)
        # Repeated initialization must not reset existing passwords or create duplicate accounts.
        await app.state.accounts.initialize()
        async with p.db.sessions() as db:
            assert len((await db.scalars(select(User))).all()) == 1
    finally:
        await p.close()
