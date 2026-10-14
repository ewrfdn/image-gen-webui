"""Simple user management following FDE's schema/controller/service structure.

Unlike FDE, only super administrators manage users here, as requested.
"""

import asyncio
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    field_validator,
    model_validator,
)
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError

from .accounts import object_body, password_hash, user_view
from .db import ApiKey, LoginSession, Setting, User
from .service import ApiError

Role = Literal["user", "admin", "super_admin"]


class UserCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(
        strict=True, min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$"
    )
    password: str = Field(strict=True, min_length=12, max_length=256)
    role: Role = "user"

    @field_validator("username")
    @classmethod
    def normalize(cls, value):
        return value.lower()


class UserUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str | None = Field(
        default=None,
        strict=True,
        min_length=3,
        max_length=64,
        pattern=r"^[A-Za-z0-9_.-]+$",
    )
    password: str | None = Field(
        default=None, strict=True, min_length=12, max_length=256
    )
    role: Role | None = None
    enabled: StrictBool | None = None

    @model_validator(mode="before")
    @classmethod
    def nonempty(cls, value):
        if isinstance(value, dict) and (
            not value or any(v is None for v in value.values())
        ):
            raise ValueError("An update requires at least one non-null field")
        return value

    @field_validator("username")
    @classmethod
    def normalize(cls, value):
        return value.lower() if value else value


class UserService:
    def __init__(self, db):
        self.db = db

    async def get(self, user_id):
        user = await self.db.get(User, user_id)
        if user is None:
            raise ApiError(404, "user_not_found", "User not found")
        return user

    async def list(self, limit, offset, search):
        condition = (
            User.username.contains(search.lower(), autoescape=True) if search else True
        )
        total = await self.db.scalar(
            select(func.count()).select_from(User).where(condition)
        )
        rows = (
            await self.db.scalars(
                select(User)
                .where(condition)
                .order_by(User.created_at.desc(), User.id)
                .limit(limit)
                .offset(offset)
            )
        ).all()
        return {
            "users": [user_view(u) for u in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    async def commit(self):
        try:
            await self.db.commit()
        except IntegrityError:
            await self.db.rollback()
            raise ApiError(
                409, "username_taken", "Username is already registered"
            ) from None

    async def create(self, payload):
        user = User(
            username=payload.username,
            password_hash=await asyncio.to_thread(password_hash, payload.password),
            role=payload.role,
        )
        self.db.add(user)
        await self.commit()
        return user_view(user)

    async def update(self, actor, user_id, payload):
        await self.db.scalar(select(Setting).where(Setting.id == 1).with_for_update())
        user = await self.get(user_id)
        body = payload.model_dump(exclude_unset=True)
        if body.get("enabled") is False and user.id == actor["user_id"]:
            raise ApiError(
                409, "cannot_disable_self", "You cannot disable your own account"
            )
        if (
            user.enabled
            and user.role == "super_admin"
            and (
                body.get("enabled") is False
                or body.get("role", user.role) != "super_admin"
            )
        ):
            count = await self.db.scalar(
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
            user.password_hash = await asyncio.to_thread(
                password_hash, body.pop("password")
            )
            await self.db.execute(
                delete(LoginSession).where(LoginSession.user_id == user.id)
            )
        enabled = body.get("enabled", user.enabled)
        role = body.get("role", user.role)
        if not enabled or role == "user":
            await self.db.execute(
                update(ApiKey)
                .where(ApiKey.user_id == user.id)
                .values(enabled=False, revoked=True)
            )
        if not enabled:
            await self.db.execute(
                delete(LoginSession).where(LoginSession.user_id == user.id)
            )
        for name, value in body.items():
            setattr(user, name, value)
        await self.commit()
        return user_view(user)


def install_users(app, platform, super_admin):
    router = APIRouter(prefix="/api/admin/users", tags=["Users"])

    @router.get("")
    async def users(
        limit: int = Query(20, ge=1, le=100),
        offset: int = Query(0, ge=0),
        q: str = Query("", max_length=64),
        identity=Depends(super_admin),
    ):
        async with platform.db.sessions() as db:
            return await UserService(db).list(limit, offset, q.strip())

    @router.get("/{user_id}")
    async def user(user_id: str, identity=Depends(super_admin)):
        async with platform.db.sessions() as db:
            return user_view(await UserService(db).get(user_id))

    @router.post("", status_code=201)
    async def create_user(
        payload: UserCreate, request: Request, identity=Depends(super_admin)
    ):
        await object_body(request)
        async with platform.db.sessions() as db:
            return await UserService(db).create(payload)

    @router.patch("/{user_id}")
    async def update_user(
        user_id: str,
        payload: UserUpdate,
        request: Request,
        identity=Depends(super_admin),
    ):
        await object_body(request)
        async with platform.db.sessions() as db:
            return await UserService(db).update(identity, user_id, payload)

    app.include_router(router)
