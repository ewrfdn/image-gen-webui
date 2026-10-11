from contextlib import asynccontextmanager
import asyncio
import base64
import hashlib
import hmac
import secrets
from pathlib import Path
from urllib.parse import urlsplit
from fastapi import Depends, FastAPI, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from .config import Config
from .db import ApiKey, MediaFile, Node, Setting, Task, now
from .service import ApiError, Platform, WAITING


class BackendInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=128)
    base_url: str
    inference_token: str = ""
    admin_token: str = ""
    enabled: bool = True


class SettingsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_count: int = Field(ge=1, le=100000)
    refresh_seconds: int = Field(ge=1, le=3600)
    stale_seconds: int = Field(ge=1, le=86400)


def create_app(config=None, client=None):
    platform = Platform(config or Config.from_env(), client)

    @asynccontextmanager
    async def lifespan(app):
        await platform.start()
        yield
        await platform.close()

    app = FastAPI(title="Frame inference platform", version="0.1.0", lifespan=lifespan)
    app.state.platform = platform

    async def read_object(request):
        try:
            value = await request.json()
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "invalid_json", "Request body must be valid JSON")
        if not isinstance(value, dict):
            raise ApiError(400, "invalid_json", "Request body must be a JSON object")
        return value

    @app.exception_handler(ApiError)
    async def api_error(request, exc):
        return JSONResponse(
            {"error": {"code": exc.code, "message": exc.message}},
            status_code=exc.status,
        )

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        return JSONResponse(
            {
                "error": {
                    "code": "invalid_request",
                    "message": "Request parameters are invalid",
                }
            },
            status_code=400,
        )

    async def authorize(request: Request):
        auth = request.headers.get("authorization", "")
        token = auth[7:] if auth.lower().startswith("bearer ") else ""
        if token and hmac.compare_digest(token, platform.config.admin_token):
            return {"owner": "admin", "admin": True}
        async with platform.db.sessions() as db:
            key = await db.scalar(
                select(ApiKey).where(
                    ApiKey.key_hash == hashlib.sha256(token.encode()).hexdigest(),
                    ApiKey.enabled.is_(True),
                    ApiKey.revoked.is_(False),
                )
            )
            if not token or key is None:
                raise ApiError(401, "unauthorized", "Invalid platform token or API Key")
            key.last_used_at = now()
            await db.commit()
            return {"owner": "key:" + key.id, "admin": False}

    async def admin(identity=Depends(authorize)):
        if not identity["admin"]:
            raise ApiError(403, "forbidden", "Administrator permission is required")
        return identity

    @app.get("/health/live")
    async def live():
        return {"status": "live"}

    @app.get("/health/ready")
    async def ready():
        if not platform.ready:
            raise ApiError(503, "initializing", "Platform is initializing")
        return {"status": "ready"}

    @app.get("/api/session")
    async def session(identity=Depends(authorize)):
        return {"admin": identity["admin"]}

    @app.get("/api/models")
    async def models(identity=Depends(authorize)):
        async with platform.db.sessions() as db:
            count = await db.scalar(
                select(func.count()).select_from(Task).where(Task.status.in_(WAITING))
            )
        return {
            "models": platform.registry.models(),
            "queue_count": count,
            "max_count": platform.settings["max_count"],
        }

    @app.get("/v1/models")
    async def compatible_models(identity=Depends(authorize)):
        return {
            "object": "list",
            "data": [
                {
                    "id": m["model_id"],
                    "object": "model",
                    "created": 0,
                    "owned_by": "local",
                }
                for m in platform.registry.models()
                if m["status"] != "unavailable"
            ],
        }

    @app.get("/api/cluster/availability")
    async def availability(
        model_id: str,
        lora_id: str | None = None,
        revision: str | None = None,
        identity=Depends(authorize),
    ):
        return await platform.registry.availability(
            model_id, revision, [{"lora_id": lora_id}] if lora_id else []
        )

    @app.post("/api/uploads", status_code=201)
    async def upload(file: UploadFile, identity=Depends(authorize)):
        data = await file.read(20 * 1024 * 1024 + 1)
        return await platform.upload(identity["owner"], data)

    @app.post("/api/tasks", status_code=202)
    async def submit(request: Request, identity=Depends(authorize)):
        task_id = await platform.submit(identity["owner"], await read_object(request))
        return {"id": task_id, "status": "pending"}

    @app.get("/api/tasks")
    async def tasks(identity=Depends(authorize)):
        async with platform.db.sessions() as db:
            query = select(Task).order_by(Task.created_at.desc()).limit(50)
            if not identity["admin"]:
                query = query.where(Task.owner == identity["owner"])
            ids = (await db.scalars(query)).all()
        return {
            "tasks": [
                await platform.task_view(t.id, identity["owner"], identity["admin"])
                for t in ids
            ]
        }

    @app.get("/api/tasks/{task_id}")
    async def task(task_id: str, identity=Depends(authorize)):
        return await platform.task_view(task_id, identity["owner"], identity["admin"])

    @app.post("/api/admin/tasks/{task_id}/retry", dependencies=[Depends(admin)])
    async def retry(task_id: str):
        async with platform.admission_lock, platform.db.sessions() as db:
            setting = await db.scalar(
                select(Setting).where(Setting.id == 1).with_for_update()
            )
            count = await db.scalar(
                select(func.count()).select_from(Task).where(Task.status.in_(WAITING))
            )
            row = await db.get(Task, task_id)
            if row is None or row.status not in ("unknown", "failed"):
                raise ApiError(
                    409,
                    "cannot_retry",
                    "Only failed or unknown tasks can be explicitly retried",
                )
            if count >= setting.max_count:
                raise ApiError(429, "queue_full", "Waiting queue is full")
            row.status, row.error, row.wait_reason = "pending", None, "explicit_retry"
            await db.commit()
        await platform.registry.sync()
        return {
            "id": task_id,
            "status": "pending",
            "warning": "Explicit retry can duplicate an unknown execution",
        }

    @app.get("/api/files/{file_id}/content")
    async def content(file_id: str, identity=Depends(authorize)):
        async with platform.db.sessions() as db:
            row = await db.get(MediaFile, file_id)
            if row is None or (
                not identity["admin"] and row.owner != identity["owner"]
            ):
                raise ApiError(404, "file_not_found", "File not found")
        path = platform.path(row.storage_key)
        if not path.is_file():
            raise ApiError(404, "file_not_found", "File not found")
        return FileResponse(path, media_type=row.mime, filename=path.name)

    async def images(request, payload, identity):
        # Images endpoints never accept video tasks.
        match = [
            m
            for m in platform.registry.models()
            if m["model_id"] == payload.get("model")
        ]
        if match and any(m["configuration"]["type"] != "image" for m in match):
            raise ApiError(
                400, "wrong_model_type", "Images API requires an image model"
            )
        task_id = await platform.submit(identity["owner"], payload, source="api")
        headers = {"X-Task-ID": task_id, "Location": "/api/tasks/" + task_id}
        if "respond-async" in request.headers.get("prefer", ""):
            return JSONResponse(
                {"id": task_id, "status": "pending"}, status_code=202, headers=headers
            )
        deadline = asyncio.get_running_loop().time() + platform.config.http_wait_seconds
        while asyncio.get_running_loop().time() < deadline:
            result = await platform.task_view(
                task_id, identity["owner"], identity["admin"]
            )
            if result["status"] == "succeeded":
                async with platform.db.sessions() as db:
                    output = await db.get(MediaFile, result["files"][0]["id"])
                data = await asyncio.to_thread(
                    platform.path(output.storage_key).read_bytes
                )
                return JSONResponse(
                    {
                        "created": int(result["completed_at"].timestamp()),
                        "data": [{"b64_json": base64.b64encode(data).decode()}],
                    },
                    headers=headers,
                )
            if result["status"] in ("failed", "unknown"):
                return JSONResponse(
                    {
                        "error": {
                            "code": "generation_" + result["status"],
                            "message": result["error"],
                        },
                        "task_id": task_id,
                    },
                    status_code=502,
                    headers=headers,
                )
            await asyncio.sleep(0.25)
        return JSONResponse(
            {
                "error": {
                    "code": "task_wait_timeout",
                    "message": "Task remains active; query its ID",
                },
                "task_id": task_id,
            },
            status_code=504,
            headers=headers,
        )

    @app.post("/v1/images/generations")
    async def generate(request: Request, identity=Depends(authorize)):
        return await images(request, await read_object(request), identity)

    @app.post("/v1/images/edits")
    async def edits(request: Request, identity=Depends(authorize)):
        import json

        form = await request.form(
            max_files=16, max_fields=32, max_part_size=20 * 1024 * 1024
        )
        payload = {}
        refs = []
        for name, value in form.multi_items():
            if hasattr(value, "read"):
                if name not in ("image", "image[]"):
                    raise ApiError(
                        400,
                        "unsupported_parameter",
                        "Only image/image[] reference files are supported",
                    )
                file = await platform.upload(
                    identity["owner"], await value.read(20 * 1024 * 1024 + 1)
                )
                refs.append(file["id"])
            elif name == "loras":
                try:
                    payload[name] = json.loads(value)
                except ValueError:
                    raise ApiError(400, "invalid_lora", "LoRA field must be valid JSON")
            elif name in ("n", "seed", "num_inference_steps"):
                try:
                    payload[name] = int(value)
                except ValueError:
                    raise ApiError(
                        400, "invalid_parameter", "Invalid integer parameter"
                    )
            elif name == "guidance_scale":
                try:
                    payload[name] = float(value)
                except ValueError:
                    raise ApiError(400, "invalid_parameter", "Invalid guidance scale")
            else:
                payload[name] = str(value)
        if not refs:
            raise ApiError(
                400, "missing_reference", "At least one reference image is required"
            )
        payload["references"] = refs
        return await images(request, payload, identity)

    @app.get("/api/admin/backends", dependencies=[Depends(admin)])
    async def backends():
        async with platform.db.sessions() as db:
            rows = (await db.scalars(select(Node))).all()
        fresh = platform.registry.fresh()
        return {
            "nodes": [
                {
                    "id": n.id,
                    "name": n.name,
                    "base_url": n.base_url,
                    "enabled": n.enabled,
                    "connected": n.id in fresh,
                    "error": platform.registry.errors.get(n.id),
                    "models": fresh.get(n.id, {}).get("models", []),
                    "catalog_errors": fresh.get(n.id, {}).get("errors", []),
                    "quarantined": n.id in platform.registry.quarantined,
                }
                for n in rows
            ]
        }

    async def save_backend(payload, node_id=None):
        url = urlsplit(payload.base_url)
        if (
            url.scheme not in ("http", "https")
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ApiError(
                400,
                "invalid_backend_url",
                "A plain HTTP(S) backend address is required",
            )
        async with platform.db.sessions() as db:
            node = await db.get(Node, node_id) if node_id else None
            if node_id and node is None:
                raise ApiError(404, "node_not_found", "Backend not found")
            address = payload.base_url.rstrip("/")
            duplicate = await db.scalar(select(Node).where(Node.base_url == address))
            if duplicate and duplicate.id != node_id:
                raise ApiError(
                    409, "duplicate_backend", "Backend address is already configured"
                )
            if node is None:
                if not payload.inference_token or not payload.admin_token:
                    raise ApiError(
                        400, "missing_credentials", "Both backend tokens are required"
                    )
                node = Node(
                    name=payload.name,
                    base_url=address,
                    inference_secret=platform.registry.encrypt(payload.inference_token),
                    admin_secret=platform.registry.encrypt(payload.admin_token),
                )
                db.add(node)
            node.name, node.base_url, node.enabled = (
                payload.name,
                address,
                payload.enabled,
            )
            if payload.inference_token:
                node.inference_secret = platform.registry.encrypt(
                    payload.inference_token
                )
            if payload.admin_token:
                node.admin_secret = platform.registry.encrypt(payload.admin_token)
            await db.commit()
        await platform.registry.sync()
        return {"id": node.id, "connected": node.id in platform.registry.fresh()}

    @app.post("/api/admin/backends", dependencies=[Depends(admin)])
    async def add_backend(payload: BackendInput):
        return await save_backend(payload)

    @app.put("/api/admin/backends/{node_id}", dependencies=[Depends(admin)])
    async def edit_backend(node_id: str, payload: BackendInput):
        return await save_backend(payload, node_id)

    @app.post("/api/admin/backends/sync", dependencies=[Depends(admin)])
    async def sync_backends():
        await platform.registry.sync()
        return {"status": "synced"}

    @app.get("/api/admin/settings", dependencies=[Depends(admin)])
    async def get_settings():
        return platform.settings | {
            "query_timeout_seconds": 1,
            "idle_release_seconds": 3600,
        }

    @app.put("/api/admin/settings", dependencies=[Depends(admin)])
    async def settings(payload: SettingsInput):
        if payload.stale_seconds < payload.refresh_seconds:
            raise ApiError(
                400,
                "invalid_settings",
                "Node TTL cannot be shorter than the refresh interval",
            )
        async with platform.admission_lock, platform.db.sessions() as db:
            row = await db.scalar(
                select(Setting).where(Setting.id == 1).with_for_update()
            )
            for k, v in payload.model_dump().items():
                setattr(row, k, v)
            await db.commit()
            platform.settings = payload.model_dump()
        return platform.settings

    @app.get("/api/admin/keys", dependencies=[Depends(admin)])
    async def keys():
        async with platform.db.sessions() as db:
            rows = (
                await db.scalars(select(ApiKey).order_by(ApiKey.created_at.desc()))
            ).all()
        return {
            "keys": [
                {
                    k: getattr(row, k)
                    for k in (
                        "id",
                        "name",
                        "prefix",
                        "enabled",
                        "revoked",
                        "created_at",
                        "last_used_at",
                    )
                }
                for row in rows
            ]
        }

    @app.post("/api/admin/keys", dependencies=[Depends(admin)])
    async def create_key(request: Request):
        body = await read_object(request)
        name = body.get("name")
        if not isinstance(name, str) or not 1 <= len(name) <= 128:
            raise ApiError(400, "invalid_name", "Key name is required")
        token = "sk-frame-" + secrets.token_urlsafe(32)
        async with platform.db.sessions() as db:
            key = ApiKey(
                name=name,
                prefix=token[:16],
                key_hash=hashlib.sha256(token.encode()).hexdigest(),
            )
            db.add(key)
            await db.commit()
        return {"id": key.id, "key": token, "prefix": key.prefix}

    @app.patch("/api/admin/keys/{key_id}", dependencies=[Depends(admin)])
    async def change_key(key_id: str, request: Request):
        body = await read_object(request)
        if set(body) - {"enabled", "revoked"} or any(
            not isinstance(v, bool) for v in body.values()
        ):
            raise ApiError(
                400,
                "invalid_key_update",
                "Only boolean enabled/revoked fields are accepted",
            )
        async with platform.db.sessions() as db:
            key = await db.get(ApiKey, key_id)
            if key is None:
                raise ApiError(404, "key_not_found", "Key not found")
            if key.revoked:
                raise ApiError(409, "key_revoked", "Revoked keys cannot be restored")
            for k, v in body.items():
                setattr(key, k, v)
            if key.revoked:
                key.enabled = False
            await db.commit()
        return {"status": "updated"}

    static = Path(__file__).resolve().parents[2] / "ui"
    app.mount("/", StaticFiles(directory=static, html=True), name="ui")
    return app
