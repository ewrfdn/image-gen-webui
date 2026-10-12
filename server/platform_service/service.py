import asyncio
import base64
import hashlib
import io
import json
import logging
from pathlib import Path
import secrets
import time
from urllib.parse import quote
import httpx
from PIL import Image
from sqlalchemy import func, select
from .db import ApiKey, Attempt, Database, MediaFile, ModelDefault, Node, Setting, Task, now, uid
from .cluster import Registry, valid_size

LOG = logging.getLogger(__name__)
WAITING = ("pending", "processing", "loading")
TERMINAL = ("succeeded", "failed", "unknown")


class ApiError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message


class Platform:
    def __init__(self, config, client=None):
        self.config = config
        self.db = Database(config.database_url)
        self.client = client or httpx.AsyncClient(follow_redirects=False)
        self.registry = Registry(self)
        self.settings = {"max_count": 100, "refresh_seconds": 5, "stale_seconds": 15}
        self.admission_lock = asyncio.Lock()
        self.background_tasks = set()
        self.stop_event = asyncio.Event()
        self.runners = []
        self.ready = False
        self.dispatch_cursor = {"image": 0, "video": 0}

    async def start(self):
        await self.db.initialize()
        async with self.db.sessions() as db:
            setting = await db.get(Setting, 1)
            self.settings = {k: getattr(setting, k) for k in self.settings}
            # Before send: restart safely. After send: execution outcome is unknown.
            tasks = (
                await db.scalars(
                    select(Task).where(Task.status.not_in(TERMINAL + ("pending",)))
                )
            ).all()
            for task in tasks:
                task.status = (
                    "pending" if task.status in ("processing", "loading") else "unknown"
                )
                task.error = (
                    None
                    if task.status == "pending"
                    else "Server restarted before confirming execution result"
                )
                task.updated_at = now()
            await db.commit()
        await self.registry.sync()
        await self.recover_artifacts()
        self.ready = True
        if self.config.background:
            self.runners = [
                asyncio.create_task(
                    self.periodic(
                        self.registry.sync, lambda: self.settings["refresh_seconds"]
                    )
                ),
                asyncio.create_task(self.periodic(self.dispatch, lambda: 2)),
                asyncio.create_task(self.periodic(self.release_idle, lambda: 60)),
                asyncio.create_task(self.periodic(self.recover_artifacts, lambda: 30)),
            ]

    async def close(self):
        self.stop_event.set()
        for task in self.runners:
            task.cancel()
        for task in self.background_tasks:
            task.cancel()
        await asyncio.gather(
            *self.runners, *self.background_tasks, return_exceptions=True
        )
        await self.client.aclose()
        await self.db.engine.dispose()

    async def periodic(self, action, interval):
        while not self.stop_event.is_set():
            try:
                await action()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("Background operation failed")
            try:
                await asyncio.wait_for(self.stop_event.wait(), interval())
            except TimeoutError:
                pass

    def path(self, storage_key):
        target = (self.config.data_root / storage_key).resolve()
        if not target.is_relative_to(self.config.data_root):
            raise ApiError(400, "invalid_path", "Invalid storage key")
        return target

    async def upload(self, owner, data):
        if len(data) > 20 * 1024 * 1024:
            raise ApiError(400, "file_too_large", "Reference limit is 20 MB per file")
        try:
            with Image.open(io.BytesIO(data)) as image:
                image.verify()
                mime = Image.MIME[image.format]
            if mime not in ("image/png", "image/jpeg", "image/webp"):
                raise ValueError("Unsupported image")
        except Exception as exc:
            raise ApiError(
                400, "invalid_reference", "Reference must be a valid PNG, JPEG or WebP"
            ) from exc
        file_id = uid()
        suffix = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}[mime]
        key = f"inputs/{file_id}/reference.{suffix}"
        await asyncio.to_thread(write_atomic, self.path(key), data)
        async with self.db.sessions() as db:
            file = MediaFile(
                id=file_id,
                owner=owner,
                role="reference",
                storage_key=key,
                mime=mime,
                size=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
            )
            db.add(file)
            await db.commit()
        return {
            "id": file_id,
            "mime": mime,
            "size": len(data),
            "url": f"/api/files/{file_id}/content",
        }

    async def models_with_defaults(self):
        async with self.db.sessions() as db:
            defaults = (await db.scalars(select(ModelDefault))).all()
        overrides = {(d.model_id, d.revision): d.generation for d in defaults}
        return [dict(m, configuration=dict(m["configuration"], generation={
            **m["configuration"]["generation"],
            **overrides.get((m["model_id"], m["revision"]), {}),
        })) for m in self.registry.models()]

    async def submit(self, owner, payload, source="ui"):
        if not self.ready:
            raise ApiError(503, "registry_initializing", "Catalogue is initializing")
        allowed = {
            "model",
            "revision",
            "prompt",
            "size",
            "n",
            "loras",
            "references",
            "seed",
            "num_inference_steps",
            "guidance_scale",
            "negative_prompt",
            "output_format",
            "quality",
            "response_format",
        }
        if set(payload) - allowed:
            raise ApiError(
                400, "unsupported_parameter", "Unsupported request parameter"
            )
        if (
            payload.get("n", 1) != 1
            or payload.get("output_format", "png") != "png"
            or payload.get("quality", "auto") != "auto"
        ):
            raise ApiError(
                400,
                "unsupported_parameter",
                "Only n=1, PNG and quality=auto are currently supported",
            )
        if payload.get("response_format", "b64_json") != "b64_json":
            raise ApiError(
                400,
                "unsupported_parameter",
                "Only b64_json is supported by Images compatibility",
            )
        prompt = payload.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 4000:
            raise ApiError(
                400, "invalid_prompt", "Prompt is required, maximum 4000 characters"
            )
        matches = [
            m
            for m in await self.models_with_defaults()
            if m["model_id"] == payload.get("model")
            and (not payload.get("revision") or m["revision"] == payload["revision"])
        ]
        if len(matches) != 1 or matches[0]["status"] == "unavailable":
            raise ApiError(
                409,
                "model_unavailable",
                "Model is unavailable or its revision is ambiguous",
            )
        model = matches[0]
        c = model["configuration"]
        body = dict(payload)
        body["size"] = (
            c["generation"]["default_size"]
            if payload.get("size", "auto") == "auto"
            else payload["size"]
        )
        body.setdefault("num_inference_steps", c["generation"]["default_steps"])
        body.setdefault("guidance_scale", c["generation"]["default_guidance_scale"])
        if c["generation"].get("default_seed") is not None:
            body.setdefault("seed", c["generation"]["default_seed"])
        if not valid_size(body["size"], c["generation"]):
            raise ApiError(400, "unsupported_size", "Size is outside model limits")
        selected_loras = payload.get("loras", [])
        if (
            not isinstance(selected_loras, list)
            or len(selected_loras) > c["lora"]["max_count"]
        ):
            raise ApiError(400, "invalid_lora", "Invalid LoRA selection count")
        loras = []
        for selection in selected_loras:
            if not isinstance(selection, dict) or set(selection) - {"lora_id", "scale"}:
                raise ApiError(400, "invalid_lora", "Invalid LoRA selection")
            available = [
                l
                for l in model.get("loras", [])
                if l["lora_id"] == selection.get("lora_id")
            ]
            if not available or len({l["sha256"] for l in available}) != 1:
                raise ApiError(
                    400,
                    "invalid_lora",
                    "LoRA is unavailable or has conflicting versions",
                )
            scale = selection.get("scale", 1)
            if not isinstance(scale, (int, float)) or not 0 <= scale <= 2:
                raise ApiError(
                    400, "invalid_lora", "LoRA scale must be between 0 and 2"
                )
            loras.append({**available[0], "scale": scale})
        refs = payload.get("references", [])
        if (
            not isinstance(refs, list)
            or not all(isinstance(v, str) for v in refs)
            or len(set(refs)) != len(refs)
        ):
            raise ApiError(
                400, "invalid_reference", "References must contain unique upload IDs"
            )
        if not c["reference"]["min_count"] <= len(refs) <= c["reference"]["max_count"]:
            raise ApiError(
                400, "invalid_reference", "Reference count is outside model limits"
            )
        # Validate numerical extensions before queueing, not after GPU dispatch.
        checks = [
            ("seed", 0, 2**32 - 1, int),
            ("num_inference_steps", 1, c["generation"]["steps_max"], int),
            ("guidance_scale", 0, 20, (int, float)),
        ]
        for key, low, high, kind in checks:
            if key in body and (
                isinstance(body[key], bool)
                or not isinstance(body[key], kind)
                or not low <= body[key] <= high
            ):
                raise ApiError(400, "invalid_parameter", f"Invalid {key}")
        async with self.admission_lock, self.db.sessions() as db:
            setting = await db.scalar(
                select(Setting).where(Setting.id == 1).with_for_update()
            )
            count = await db.scalar(
                select(func.count()).select_from(Task).where(Task.status.in_(WAITING))
            )
            if count >= setting.max_count:
                raise ApiError(429, "queue_full", "Waiting queue is full")
            reference_files = []
            for file_id in refs:
                file = await db.get(MediaFile, file_id)
                if (
                    not file
                    or file.owner != owner
                    or file.role != "reference"
                    or file.task_id
                ):
                    raise ApiError(
                        400,
                        "invalid_reference",
                        "Reference upload is missing or already used",
                    )
                if (
                    file.mime not in c["reference"]["mime_types"]
                    or file.size > c["reference"]["max_file_bytes"]
                ):
                    raise ApiError(
                        400,
                        "invalid_reference",
                        "Reference type or size is outside model limits",
                    )
                reference_files.append(file)
            task = Task(
                id=uid(),
                type=c["type"],
                owner=owner,
                source=source,
                model_id=model["model_id"],
                model_revision=model["revision"],
                model_sha256=model["artifact_sha256"],
                loras=loras,
                capabilities=c,
                request_body=body,
            )
            db.add(task)
            await db.flush()
            for i, file in enumerate(reference_files):
                file.task_id, file.position = task.id, i
            await db.commit()
        return task.id

    async def task_view(self, task_id, owner, admin=False):
        async with self.db.sessions() as db:
            task = await db.get(Task, task_id)
            if task is None or (not admin and task.owner != owner):
                raise ApiError(404, "task_not_found", "Task not found")
            files = (
                await db.scalars(
                    select(MediaFile)
                    .where(MediaFile.task_id == task.id, MediaFile.role == "output")
                    .order_by(MediaFile.position)
                )
            ).all()
            return {
                k: getattr(task, k)
                for k in (
                    "id",
                    "type",
                    "status",
                    "model_id",
                    "model_revision",
                    "node_id",
                    "wait_reason",
                    "error",
                    "attempt_count",
                    "created_at",
                    "completed_at",
                )
            } | {
                "prompt": task.request_body.get("prompt", ""),
                "parameters": {
                    **{k: v for k, v in task.request_body.items() if k in (
                        "size", "seed", "num_inference_steps", "guidance_scale")},
                    **({"seed": task.raw_result["seed"]}
                       if task.raw_result and task.raw_result.get("seed") is not None else {}),
                },
                **({"owner": task.owner, "source": task.source} if admin else {}),
                "files": [
                    {
                        "id": f.id,
                        "mime": f.mime,
                        "storage_key": f.storage_key,
                        "url": f"/api/files/{f.id}/content",
                    }
                    for f in files
                ]
            }

    async def dispatch(self):
        # Both media types use the same node reservation lock. Skip unserviceable tasks.
        async with self.registry.lock:
            async with self.db.sessions() as db:
                candidates = []
                for kind in ("image", "video"):
                    count = await db.scalar(
                        select(func.count())
                        .select_from(Task)
                        .where(Task.status == "pending", Task.type == kind)
                    )
                    offset = (
                        self.dispatch_cursor[kind]
                        if self.dispatch_cursor[kind] < count
                        else 0
                    )
                    batch = (
                        await db.scalars(
                            select(Task)
                            .where(Task.status == "pending", Task.type == kind)
                            .order_by(Task.created_at, Task.id)
                            .offset(offset)
                            .limit(20)
                        )
                    ).all()
                    candidates.extend(batch)
                    self.dispatch_cursor[kind] = offset + len(batch)
                candidates.sort(key=lambda t: t.created_at)
            for task in candidates:
                refs = []
                async with self.db.sessions() as db:
                    files = (
                        await db.scalars(
                            select(MediaFile).where(
                                MediaFile.task_id == task.id,
                                MediaFile.role == "reference",
                            )
                        )
                    ).all()
                    refs = [{"mime": f.mime, "size": f.size} for f in files]
                result = await self.registry.availability(
                    task.model_id,
                    task.model_revision,
                    task.loras,
                    refs,
                    task.request_body["size"],
                )
                async with self.db.sessions() as db:
                    current = await db.scalar(
                        select(Task).where(Task.id == task.id).with_for_update()
                    )
                    if current.status != "pending":
                        continue
                    current.wait_reason = result["status"]
                    if result["status"] not in ("available", "need_load"):
                        await db.commit()
                        continue
                    node_id = result["node_id"]
                    self.registry.reserved.add(node_id)
                    current.node_id = node_id
                    current.status = (
                        "loading" if result["status"] == "need_load" else "processing"
                    )
                    current.updated_at = now()
                    try:
                        await db.commit()
                    except Exception:
                        self.registry.reserved.discard(node_id)
                        raise
                runner = asyncio.create_task(
                    self.execute(task.id, node_id, result["status"] == "need_load")
                )
                self.background_tasks.add(runner)
                runner.add_done_callback(self.background_tasks.discard)

    async def execute(self, task_id, node_id, load):
        sent = False
        attempt_id = None
        key = None
        try:
            async with self.db.sessions() as db:
                task = await db.get(Task, task_id)
                node = await db.get(Node, node_id)
                references = (
                    await db.scalars(
                        select(MediaFile)
                        .where(
                            MediaFile.task_id == task_id, MediaFile.role == "reference"
                        )
                        .order_by(MediaFile.position)
                    )
                ).all()
            key = (task.model_id, task.model_revision)
            if load:
                await self.registry.node_request(
                    node,
                    "POST",
                    "/internal/v1/models/" + quote(task.model_id, safe="") + "/load",
                    timeout=900,
                )
            # Revalidate exact model/LoRA identity immediately before actual dispatch.
            catalogue = (
                await self.registry.node_request(node, "GET", "/internal/v1/catalog")
            ).json()
            match = next(
                (
                    m
                    for m in catalogue["models"]
                    if m["model_id"] == task.model_id
                    and m["revision"] == task.model_revision
                    and m["artifact_sha256"] == task.model_sha256
                ),
                None,
            )
            if match is None or any(
                not any(
                    l["lora_id"] == w["lora_id"] and l["sha256"] == w["sha256"]
                    for l in self.registry.model_loras(match)
                )
                for w in task.loras
            ):
                raise ApiError(
                    409, "resource_changed", "Pinned model or LoRA is no longer present"
                )
            attempt_id = uid()
            async with self.db.sessions() as db:
                current = await db.get(Task, task_id)
                current.status, current.attempt_id = "submitted", attempt_id
                current.attempt_count += 1
                current.submitted_at, current.updated_at = now(), now()
                current.wait_reason = None
                db.add(Attempt(id=attempt_id, task_id=task_id, node_id=node_id))
                await db.commit()
            body = {
                k: v
                for k, v in task.request_body.items()
                if k
                not in (
                    "revision",
                    "references",
                    "output_format",
                    "quality",
                    "response_format",
                )
            }
            body["model"] = task.model_id
            body["expected_revision"] = task.model_revision
            body["expected_artifact_sha256"] = task.model_sha256
            body["expected_lora_sha256"] = {
                l["lora_id"]: l["sha256"] for l in task.loras
            }
            body["loras"] = [
                {"lora_id": l["lora_id"], "scale": l["scale"]} for l in task.loras
            ]
            sent = True
            self.registry.last_invoked[key] = time.monotonic()
            timeout = task.capabilities["generation"]["timeout_seconds"]
            if task.type == "image" and not references:
                response = await self.registry.node_request(
                    node, "POST", "/v1/images/generations", timeout=timeout, json=body
                )
            else:
                endpoint = "/v1/images/edits" if task.type == "image" else "/v1/videos"
                multipart = [
                    (
                        "image[]",
                        (
                            f"ref-{i}",
                            await asyncio.to_thread(
                                self.path(f.storage_key).read_bytes
                            ),
                            f.mime,
                        ),
                    )
                    for i, f in enumerate(references)
                ]
                fields = {
                    k: (json.dumps(v) if isinstance(v, (list, dict)) else str(v))
                    for k, v in body.items()
                }
                response = await self.registry.node_request(
                    node,
                    "POST",
                    endpoint,
                    timeout=timeout,
                    data=fields,
                    files=multipart
                    if multipart
                    else [("request", (None, json.dumps(body), "application/json"))],
                )
            raw = response.json()
            if task.type == "video" and raw.get("id"):
                async with self.db.sessions() as db:
                    current = await db.get(Task, task_id)
                    current.status, current.external_task_id = "running", raw["id"]
                    await db.commit()
                deadline = time.monotonic() + timeout
                while raw.get("status") not in ("completed", "succeeded", "failed"):
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Video execution deadline exceeded")
                    await asyncio.sleep(3)
                    raw = (
                        await self.registry.node_request(
                            node,
                            "GET",
                            "/v1/videos/" + quote(raw["id"], safe=""),
                            timeout=10,
                        )
                    ).json()
                if raw["status"] == "failed":
                    raise ApiError(
                        500, "video_failed", "Video backend reported a failure"
                    )
                response = await self.registry.node_request(
                    node,
                    "GET",
                    "/v1/videos/" + quote(raw["id"], safe="") + "/content",
                    timeout=timeout,
                )
                content, mime = response.content, "video/mp4"
            else:
                content = base64.b64decode(raw["data"][0]["b64_json"], validate=True)
                mime = "image/png" if task.type == "image" else "video/mp4"
            if len(content) > 512 * 1024 * 1024:
                raise ApiError(
                    500, "result_too_large", "Generated media exceeds storage limit"
                )
            if task.type == "image":
                with Image.open(io.BytesIO(content)) as image:
                    image.verify()
                    if image.format != "PNG":
                        raise ApiError(
                            500, "invalid_output", "Backend did not return a PNG"
                        )
            elif len(content) < 12 or content[4:8] != b"ftyp":
                raise ApiError(500, "invalid_output", "Backend did not return an MP4")
            async with self.db.sessions() as db:
                current = await db.get(Task, task_id)
                current.status, current.updated_at = "persisting", now()
                await db.commit()
            storage_key = f"outputs/{task_id}/{attempt_id}/0." + (
                "png" if task.type == "image" else "mp4"
            )
            await asyncio.to_thread(write_atomic, self.path(storage_key), content)
            manifest = {
                "task_id": task_id,
                "attempt_id": attempt_id,
                "owner": task.owner,
                "storage_key": storage_key,
                "mime": mime,
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "metadata": raw.get("metadata", {}),
            }
            await asyncio.to_thread(
                write_atomic,
                self.path(f"outputs/{task_id}/{attempt_id}/completion.json"),
                json.dumps(manifest).encode(),
            )
            await self.complete(manifest)
        except asyncio.CancelledError:
            if sent:
                await self.mark_error(
                    task_id,
                    attempt_id,
                    "unknown",
                    "Execution interrupted; result is unknown",
                )
            raise
        except (httpx.TimeoutException, httpx.NetworkError, TimeoutError):
            await self.mark_error(
                task_id,
                attempt_id,
                "unknown" if sent else "pending",
                "Node timed out; execution result may be unknown",
            )
        except httpx.HTTPStatusError as exc:
            try:
                code = exc.response.json().get("error", {}).get("code", "backend_error")
            except ValueError:
                code = "backend_error"
            safe = code in (
                "engine_busy",
                "model_not_loaded",
                "model_transitioning",
                "resource_changed",
                "capacity_conflict",
            )
            await self.mark_error(
                task_id, attempt_id, "pending" if safe else "failed", code
            )
        except ApiError as exc:
            await self.mark_error(
                task_id, attempt_id, "failed" if sent else "pending", exc.message
            )
        except Exception:
            LOG.exception("Task %s failed", task_id)
            # If the completion manifest exists, recovery must retry persistence, never generation.
            has_manifest = (
                attempt_id
                and self.path(
                    f"outputs/{task_id}/{attempt_id}/completion.json"
                ).exists()
            )
            if not has_manifest:
                await self.mark_error(
                    task_id,
                    attempt_id,
                    "unknown" if sent else "pending",
                    "Unexpected execution or persistence error",
                )
        finally:
            if key and sent:
                self.registry.last_invoked[key] = time.monotonic()
            self.registry.reserved.discard(node_id)
            await self.registry.sync()

    async def mark_error(self, task_id, attempt_id, status, message):
        async with self.db.sessions() as db:
            task = await db.get(Task, task_id)
            task.status, task.error, task.updated_at = status, message, now()
            if status == "unknown" and task.node_id:
                self.registry.quarantined.add(task.node_id)
            if attempt_id:
                attempt = await db.get(Attempt, attempt_id)
                attempt.status, attempt.error, attempt.completed_at = (
                    status,
                    message,
                    now(),
                )
            await db.commit()

    async def complete(self, manifest):
        async with self.db.sessions() as db:
            task = await db.get(Task, manifest["task_id"])
            if task is None or task.attempt_id != manifest["attempt_id"]:
                return
            existing = await db.scalar(
                select(MediaFile).where(
                    MediaFile.storage_key == manifest["storage_key"]
                )
            )
            if existing is None:
                db.add(
                    MediaFile(
                        id=uid(),
                        task_id=task.id,
                        attempt_id=manifest["attempt_id"],
                        owner=task.owner,
                        role="output",
                        storage_key=manifest["storage_key"],
                        mime=manifest["mime"],
                        size=manifest["size"],
                        sha256=manifest["sha256"],
                    )
                )
            task.status, task.error, task.completed_at, task.updated_at = (
                "succeeded",
                None,
                now(),
                now(),
            )
            task.raw_result = manifest["metadata"]
            attempt = await db.get(Attempt, manifest["attempt_id"])
            attempt.status, attempt.completed_at = "succeeded", now()
            await db.commit()

    async def recover_artifacts(self):
        # Bounded by unfinished durable tasks, not an ever-growing full disk scan.
        async with self.db.sessions() as db:
            tasks = (
                await db.scalars(
                    select(Task).where(Task.status.in_(("persisting", "unknown")))
                )
            ).all()
        for task in tasks:
            if not task.attempt_id:
                continue
            manifest = self.path(f"outputs/{task.id}/{task.attempt_id}/completion.json")
            if manifest.exists():
                data = json.loads(await asyncio.to_thread(manifest.read_text))
                content = await asyncio.to_thread(
                    self.path(data["storage_key"]).read_bytes
                )
                if hashlib.sha256(content).hexdigest() == data["sha256"]:
                    await self.complete(data)

    async def release_idle(self):
        async with self.registry.lock:
            async with self.db.sessions() as db:
                waiting = (
                    await db.scalars(select(Task).where(Task.status.in_(WAITING)))
                ).all()
            protected = {(t.model_id, t.model_revision) for t in waiting}
            for node_id, snap in self.registry.fresh().items():
                if (
                    node_id in self.registry.reserved
                    or node_id in self.registry.quarantined
                ):
                    continue
                for runtime in snap["runtime"].get("models", []):
                    if not runtime.get("can_generate") or runtime.get(
                        "active_requests"
                    ):
                        continue
                    model = next(
                        (
                            m
                            for m in snap["models"]
                            if m["model_id"] == runtime["model_id"]
                        ),
                        None,
                    )
                    if not model:
                        continue
                    key = (model["model_id"], model["revision"])
                    if (
                        key in protected
                        or time.monotonic()
                        - self.registry.last_invoked.get(key, time.monotonic())
                        < 3600
                    ):
                        continue
                    self.registry.reserved.add(node_id)
                    try:
                        await self.registry.node_request(
                            snap["node"],
                            "POST",
                            "/internal/v1/models/"
                            + quote(model["model_id"], safe="")
                            + "/unload",
                            timeout=120,
                        )
                    finally:
                        self.registry.reserved.discard(node_id)
        await self.registry.sync()


def write_atomic(path: Path, content: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + ".tmp")
    with temporary.open("wb") as stream:
        stream.write(content)
        stream.flush()
        import os

        os.fsync(stream.fileno())
    temporary.replace(path)
