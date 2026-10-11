"""Account authentication and role enforcement, shared by UI sessions and API Keys."""

import asyncio
import hashlib
import hmac
import re
import secrets
from datetime import timedelta
from urllib.parse import urlsplit

from fastapi import Depends, Request, Response
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError

from .db import ApiKey, LoginSession, MediaFile, Setting, Task, User, now
from .service import ApiError

ROLES = {"user", "admin", "super_admin"}
COOKIE = "frame_session"


async def object_body(request):
    origin = request.headers.get("origin")
    if origin and (
        urlsplit(origin).netloc != request.headers.get("host")
        or urlsplit(origin).scheme != request.url.scheme
    ):
        raise ApiError(403, "invalid_origin", "Request origin is not allowed")
    try:
        body = await request.json()
    except ValueError:
        raise ApiError(400, "invalid_json", "Request body must be valid JSON") from None
    if not isinstance(body, dict):
        raise ApiError(400, "invalid_json", "Request body must be an object")
    return body


def password_hash(password):
    salt = secrets.token_hex(16)
    digest = hashlib.scrypt(
        password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1
    ).hex()
    return salt + ":" + digest


def verify_password(password, stored):
    salt, expected = stored.split(":", 1)
    actual = hashlib.scrypt(
        password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1
    ).hex()
    return hmac.compare_digest(actual, expected)


def validate_account(username, password):
    if not isinstance(username, str) or not re.fullmatch(
        r"[a-zA-Z0-9_.-]{3,64}", username
    ):
        raise ApiError(
            400,
            "invalid_username",
            "Username must be 3-64 letters, digits, dots, underscores or hyphens",
        )
    if not isinstance(password, str) or not 12 <= len(password) <= 256:
        raise ApiError(
            400, "invalid_password", "Password must contain 12-256 characters"
        )
    return username.lower()


def user_view(user):
    return {
        k: getattr(user, k) for k in ("id", "username", "role", "enabled", "created_at")
    }


class Accounts:
    def __init__(self, platform):
        self.platform = platform
        self.dummy_hash = password_hash(secrets.token_urlsafe(32))
        self.failures = {}

    async def initialize(self):
        async with self.platform.db.sessions() as db:
            await db.scalar(select(Setting).where(Setting.id == 1).with_for_update())
            if not await db.scalar(select(User.id).limit(1)):
                user = User(
                    username="admin",
                    role="super_admin",
                    password_hash=await asyncio.to_thread(
                        password_hash, self.platform.config.admin_token
                    ),
                )
                db.add(user)
                await db.flush()
                await db.execute(
                    update(ApiKey)
                    .where(ApiKey.user_id.is_(None))
                    .values(user_id=user.id)
                )
                for model in (Task, MediaFile):
                    await db.execute(
                        update(model)
                        .where(model.owner == "admin")
                        .values(owner="user:" + user.id)
                    )
                await db.commit()

    async def authorize(self, request: Request):
        auth = request.headers.get("authorization", "")
        token = auth[7:] if auth.lower().startswith("bearer ") else ""
        cookie = request.cookies.get(COOKIE, "")
        async with self.platform.db.sessions() as db:
            user = None
            kind = "session"
            if token and hmac.compare_digest(token, self.platform.config.admin_token):
                # Transitional deployment credential is valid only while the initial
                # account still has that password and retains super-admin permission.
                user = await db.scalar(select(User).where(User.username == "admin"))
                if user and (
                    user.role != "super_admin"
                    or not await asyncio.to_thread(
                        verify_password, token, user.password_hash
                    )
                ):
                    user = None
                kind = "bootstrap"
            elif token:
                key = await db.scalar(
                    select(ApiKey).where(
                        ApiKey.key_hash == hashlib.sha256(token.encode()).hexdigest(),
                        ApiKey.enabled.is_(True),
                        ApiKey.revoked.is_(False),
                    )
                )
                user = await db.get(User, key.user_id) if key and key.user_id else None
                if user and user.enabled and user.role in ("admin", "super_admin"):
                    key.last_used_at = now()
                    await db.commit()
                    return {
                        "owner": "key:" + key.id,
                        "admin": False,
                        "role": user.role,
                        "kind": "api_key",
                        "user_id": user.id,
                    }
                user = None
            elif cookie:
                session = await db.scalar(
                    select(LoginSession).where(
                        LoginSession.token_hash
                        == hashlib.sha256(cookie.encode()).hexdigest(),
                        LoginSession.expires_at > now(),
                    )
                )
                user = await db.get(User, session.user_id) if session else None
                if request.method not in ("GET", "HEAD", "OPTIONS"):
                    origin = request.headers.get("origin")
                    if origin and urlsplit(origin).netloc != request.headers.get(
                        "host"
                    ):
                        raise ApiError(
                            403, "invalid_origin", "Request origin is not allowed"
                        )
            if not user or not user.enabled:
                raise ApiError(
                    401,
                    "unauthorized",
                    "Sign in with an active account or valid administrator API Key",
                )
            return {
                "owner": "user:" + user.id,
                "admin": user.role in ("admin", "super_admin"),
                "role": user.role,
                "kind": kind,
                "user_id": user.id,
                "username": user.username,
            }

    def install(self, app):
        authorize = self.authorize

        async def super_admin(identity=Depends(authorize)):
            if identity["role"] != "super_admin" or identity["kind"] == "api_key":
                raise ApiError(
                    403, "forbidden", "Super administrator permission is required"
                )
            return identity

        @app.post("/api/auth/login")
        async def login(request: Request, response: Response):
            body = await object_body(request)
            username = body.get("username", "")
            password = body.get("password", "")
            if (
                not isinstance(username, str)
                or not isinstance(password, str)
                or len(password) > 256
            ):
                raise ApiError(400, "invalid_login", "Invalid username or password")
            username = username.lower()
            address = request.client.host if request.client else "unknown"
            attempt = self.failures.get(address, (0, now()))
            if attempt[1] + timedelta(minutes=5) < now():
                attempt = (0, now())
            if attempt[0] >= 10:
                raise ApiError(429, "login_rate_limited", "Try again in five minutes")
            # Count before hashing so concurrent attempts also respect the limit.
            self.failures[address] = (attempt[0] + 1, attempt[1])
            async with self.platform.db.sessions() as db:
                user = await db.scalar(select(User).where(User.username == username))
                valid = await asyncio.to_thread(
                    verify_password,
                    password,
                    user.password_hash if user else self.dummy_hash,
                )
                if not valid or not user or not user.enabled:
                    raise ApiError(401, "invalid_login", "Invalid username or password")
                self.failures.pop(address, None)
                token = secrets.token_urlsafe(32)
                await db.execute(
                    delete(LoginSession).where(LoginSession.expires_at <= now())
                )
                db.add(
                    LoginSession(
                        token_hash=hashlib.sha256(token.encode()).hexdigest(),
                        user_id=user.id,
                        expires_at=now() + timedelta(days=7),
                    )
                )
                await db.commit()
            response.set_cookie(
                COOKIE,
                token,
                httponly=True,
                samesite="strict",
                secure=request.url.scheme == "https",
                max_age=7 * 86400,
            )
            return user_view(user)

        @app.post("/api/auth/logout")
        async def logout(request: Request, response: Response):
            token = request.cookies.get(COOKIE, "")
            async with self.platform.db.sessions() as db:
                await db.execute(
                    delete(LoginSession).where(
                        LoginSession.token_hash
                        == hashlib.sha256(token.encode()).hexdigest()
                    )
                )
                await db.commit()
            response.delete_cookie(COOKIE)
            return {"status": "logged_out"}

        @app.post("/api/auth/password")
        async def change_password(
            request: Request, response: Response, identity=Depends(authorize)
        ):
            if identity["kind"] == "api_key":
                raise ApiError(403, "forbidden", "Account login is required")
            body = await object_body(request)
            async with self.platform.db.sessions() as db:
                user = await db.get(User, identity["user_id"])
                validate_account(user.username, body.get("password"))
                current = body.get("current_password", "")
                if (
                    not isinstance(current, str)
                    or len(current) > 256
                    or not await asyncio.to_thread(
                        verify_password, current, user.password_hash
                    )
                ):
                    raise ApiError(
                        400, "invalid_password", "Current password is incorrect"
                    )
                user.password_hash = await asyncio.to_thread(
                    password_hash, body["password"]
                )
                await db.execute(
                    delete(LoginSession).where(LoginSession.user_id == user.id)
                )
                await db.commit()
            response.delete_cookie(COOKIE)
            return {"status": "password_changed"}

        @app.get("/api/admin/users")
        async def users(identity=Depends(super_admin)):
            async with self.platform.db.sessions() as db:
                return {
                    "users": [
                        user_view(u)
                        for u in (
                            await db.scalars(select(User).order_by(User.created_at))
                        ).all()
                    ]
                }

        @app.post("/api/admin/users", status_code=201)
        async def create_user(request: Request, identity=Depends(super_admin)):
            body = await object_body(request)
            username = validate_account(body.get("username"), body.get("password"))
            role = body.get("role", "user")
            if (
                not isinstance(role, str)
                or role not in ROLES
                or set(body) - {"username", "password", "role"}
            ):
                raise ApiError(400, "invalid_user", "Invalid account fields or role")
            async with self.platform.db.sessions() as db:
                user = User(
                    username=username,
                    password_hash=await asyncio.to_thread(
                        password_hash, body["password"]
                    ),
                    role=role,
                )
                db.add(user)
                try:
                    await db.commit()
                except IntegrityError:
                    await db.rollback()
                    raise ApiError(
                        409, "username_taken", "Username is already registered"
                    ) from None
                return user_view(user)

        @app.patch("/api/admin/users/{user_id}")
        async def edit_user(
            user_id: str, request: Request, identity=Depends(super_admin)
        ):
            body = await object_body(request)
            if (
                not body
                or set(body) - {"role", "enabled", "password"}
                or (
                    "role" in body
                    and (not isinstance(body["role"], str) or body["role"] not in ROLES)
                )
                or ("enabled" in body and not isinstance(body["enabled"], bool))
            ):
                raise ApiError(400, "invalid_user", "Invalid account update")
            async with self.platform.db.sessions() as db:
                # Singleton row lock serializes last-super-admin protection.
                await db.scalar(
                    select(Setting).where(Setting.id == 1).with_for_update()
                )
                user = await db.get(User, user_id)
                if not user:
                    raise ApiError(404, "user_not_found", "User not found")
                if (
                    user.enabled
                    and user.role == "super_admin"
                    and (
                        body.get("enabled") is False
                        or body.get("role", user.role) != "super_admin"
                    )
                ):
                    count = await db.scalar(
                        select(func.count())
                        .select_from(User)
                        .where(User.enabled.is_(True), User.role == "super_admin")
                    )
                    if count <= 1:
                        raise ApiError(
                            409,
                            "last_super_admin",
                            "Keep at least one active super administrator",
                        )
                if "password" in body:
                    validate_account(user.username, body["password"])
                    user.password_hash = await asyncio.to_thread(
                        password_hash, body["password"]
                    )
                    await db.execute(
                        delete(LoginSession).where(LoginSession.user_id == user.id)
                    )
                for field in ("role", "enabled"):
                    if field in body:
                        setattr(user, field, body[field])
                if not user.enabled or user.role == "user":
                    await db.execute(
                        update(ApiKey)
                        .where(ApiKey.user_id == user.id)
                        .values(enabled=False, revoked=True)
                    )
                if not user.enabled:
                    await db.execute(
                        delete(LoginSession).where(LoginSession.user_id == user.id)
                    )
                await db.commit()
                return user_view(user)

        return super_admin
