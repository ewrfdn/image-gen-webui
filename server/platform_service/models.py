"""Durable business records. Model and LoRA catalogues deliberately have no tables."""

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def now():
    return datetime.now(UTC)


def uid():
    return str(uuid4())


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(32), default="user")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class LoginSession(Base):
    __tablename__ = "login_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Node(Base):
    __tablename__ = "backend_nodes"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(128))
    base_url: Mapped[str] = mapped_column(Text, unique=True)
    inference_secret: Mapped[str] = mapped_column(Text)
    admin_secret: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class ApiKey(Base):
    __tablename__ = "api_keys"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(128))
    prefix: Mapped[str] = mapped_column(String(32))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), index=True)


class Setting(Base):
    __tablename__ = "system_settings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    max_count: Mapped[int] = mapped_column(Integer, default=100)
    refresh_seconds: Mapped[int] = mapped_column(Integer, default=5)
    stale_seconds: Mapped[int] = mapped_column(Integer, default=15)
    idle_release_seconds: Mapped[int] = mapped_column(Integer, default=3600)


class ModelDefault(Base):
    __tablename__ = "model_defaults"
    model_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    revision: Mapped[str] = mapped_column(String(128), primary_key=True)
    generation: Mapped[dict] = mapped_column(JSON)


class Task(Base):
    __tablename__ = "media_tasks"
    __table_args__ = (
        Index("ix_tasks_status_type_created", "status", "type", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    type: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="pending")
    owner: Mapped[str] = mapped_column(String(128), index=True)
    source: Mapped[str] = mapped_column(String(16), default="ui")
    model_id: Mapped[str] = mapped_column(String(128))
    model_revision: Mapped[str] = mapped_column(String(128))
    model_sha256: Mapped[str] = mapped_column(String(64))
    loras: Mapped[list] = mapped_column(JSON, default=list)
    capabilities: Mapped[dict] = mapped_column(JSON)
    request_body: Mapped[dict] = mapped_column(JSON)
    raw_request: Mapped[dict | None] = mapped_column(JSON)
    node_id: Mapped[str | None] = mapped_column(ForeignKey("backend_nodes.id"))
    attempt_id: Mapped[str | None] = mapped_column(String(36))
    external_task_id: Mapped[str | None] = mapped_column(String(128))
    wait_reason: Mapped[str | None] = mapped_column(String(128))
    error: Mapped[str | None] = mapped_column(Text)
    raw_result: Mapped[dict | None] = mapped_column(JSON)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Attempt(Base):
    __tablename__ = "task_attempts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    task_id: Mapped[str] = mapped_column(ForeignKey("media_tasks.id"), index=True)
    node_id: Mapped[str] = mapped_column(ForeignKey("backend_nodes.id"))
    status: Mapped[str] = mapped_column(String(32), default="submitted")
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Blob(Base):
    __tablename__ = "blobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    sha256: Mapped[str] = mapped_column(String(64), unique=True)
    storage_key: Mapped[str] = mapped_column(Text, unique=True)
    mime: Mapped[str] = mapped_column(String(128))
    size: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Asset(Base):
    __tablename__ = "assets"
    __table_args__ = (Index("ix_assets_owner_blob", "owner", "blob_id", unique=True),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    owner: Mapped[str] = mapped_column(String(128), index=True)
    blob_id: Mapped[str] = mapped_column(ForeignKey("blobs.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class MediaFile(Base):
    __tablename__ = "media_files"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    task_id: Mapped[str | None] = mapped_column(
        ForeignKey("media_tasks.id"), index=True
    )
    attempt_id: Mapped[str | None] = mapped_column(ForeignKey("task_attempts.id"))
    owner: Mapped[str] = mapped_column(String(128))
    role: Mapped[str] = mapped_column(String(16))
    position: Mapped[int] = mapped_column(Integer, default=0)
    storage_key: Mapped[str] = mapped_column(Text, unique=True)
    mime: Mapped[str] = mapped_column(String(128))
    size: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


