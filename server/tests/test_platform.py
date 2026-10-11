import asyncio
import base64
import copy
import io
import time
import os
from pathlib import Path
import httpx
import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from PIL import Image
from sqlalchemy import func, select
from platform_service.app import create_app
from platform_service.config import Config
from platform_service.db import Attempt, Base, MediaFile, Node, Setting, Task


def png():
    out = io.BytesIO()
    Image.new("RGB", (512, 512), (64, 96, 128)).save(out, format="PNG")
    return out.getvalue()


def model(kind="image", references=False):
    result = {
        "model_id": "test-" + kind,
        "revision": "r1",
        "artifact_sha256": "1" * 64,
        "local_available": True,
        "configuration": {
            "schema_version": 1,
            "model_id": "test-" + kind,
            "display_name": "Test " + kind,
            "revision": "r1",
            "artifact_sha256": "1" * 64,
            "type": kind,
            "architecture": "test",
            "reference": {
                "supported": references,
                "mime_types": ["image/png"] if references else [],
                "min_count": 0,
                "max_count": 2 if references else 0,
                "max_file_bytes": 20 * 1024 * 1024 if references else 0,
            },
            "lora": {"supported": True, "max_count": 1},
            "generation": {
                "default_size": "512x512",
                "width_min": 512,
                "width_max": 1024,
                "height_min": 512,
                "height_max": 1024,
                "dimension_multiple": 64,
                "max_pixels": 1048576,
                "default_steps": 4,
                "steps_max": 100,
                "default_guidance_scale": 1,
                "output_mime_types": ["image/png" if kind == "image" else "video/mp4"],
                "timeout_seconds": 30,
            },
        },
        "loras": [
            {
                "lora_id": "style",
                "sha256": "2" * 64,
                "revision": "lr1",
                "display_name": "Style",
            }
        ],
    }
    if kind == "video":
        result["configuration"]["generation"]["video"] = {
            "default_duration_seconds": 5,
            "max_duration_seconds": 10,
            "fps": 24,
        }
    return result


class Backend:
    def __init__(self):
        self.models = [model()]
        self.loaded = None
        self.requests = []
        self.query_delay = 0
        self.slow_host = None
        self.generate_timeout = False
        self.unloads = 0

    def runtime(self):
        return {
            "models": (
                []
                if self.loaded is None
                else [
                    {
                        "model_id": self.loaded,
                        "state": "ready",
                        "can_generate": True,
                        "active_requests": 0,
                    }
                ]
            ),
            "busy": False,
        }

    async def handle(self, request):
        self.requests.append(request)
        path = request.url.path
        if path == "/internal/v1/catalog":
            return httpx.Response(
                200, json={"models": self.models, "runtime": self.runtime()}
            )
        if path == "/internal/v1/models":
            await asyncio.sleep(
                self.query_delay
                if self.slow_host is None or request.url.host == self.slow_host
                else 0
            )
            return httpx.Response(200, json=self.runtime())
        if path.endswith("/load"):
            self.loaded = path.split("/")[-2]
            return httpx.Response(200, json={"state": "ready"})
        if path.endswith("/unload"):
            self.loaded = None
            self.unloads += 1
            return httpx.Response(200, json={"state": "unloaded"})
        if path in ("/v1/images/generations", "/v1/images/edits"):
            if self.generate_timeout:
                raise httpx.ReadTimeout("lost", request=request)
            return httpx.Response(
                200,
                json={
                    "data": [{"b64_json": base64.b64encode(png()).decode()}],
                    "metadata": {"seed": 42},
                },
            )
        if path == "/v1/videos":
            return httpx.Response(200, json={"id": "video-1", "status": "completed"})
        if path == "/v1/videos/video-1/content":
            return httpx.Response(
                200, content=b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 16
            )
        return httpx.Response(404, json={})


@pytest_asyncio.fixture
async def setup(tmp_path):
    backend = Backend()
    outbound = httpx.AsyncClient(transport=httpx.MockTransport(backend.handle))
    database_url = os.getenv(
        "TEST_DATABASE_URL", "sqlite+aiosqlite:///" + str(tmp_path / "db.sqlite")
    )
    if database_url.startswith("postgresql"):
        assert database_url.rsplit("/", 1)[-1].endswith("_verify"), (
            "Never reset a non-verification database"
        )
    config = Config(
        database_url,
        "a" * 32,
        Fernet.generate_key().decode(),
        tmp_path / "data",
        False,
        2,
    )
    app = create_app(config, outbound)
    platform = app.state.platform
    if database_url.startswith("postgresql"):
        async with platform.db.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
    await platform.start()
    async with platform.db.sessions() as db:
        node = Node(
            id="node-a",
            name="DGX test",
            base_url="http://fake",
            admin_secret=platform.registry.encrypt("admin"),
            inference_secret=platform.registry.encrypt("infer"),
        )
        db.add(node)
        await db.commit()
    await platform.registry.sync()
    api = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://platform",
        headers={"Authorization": "Bearer " + "a" * 32},
    )
    yield platform, api, backend
    await api.aclose()
    await platform.close()


async def drain(platform):
    await platform.dispatch()
    if platform.background_tasks:
        await asyncio.gather(*platform.background_tasks)


@pytest.mark.asyncio
async def test_queue_admission_is_atomic_and_unavailable_waits(setup):
    p, api, backend = setup
    response = await api.put(
        "/api/admin/settings",
        json={"max_count": 3, "refresh_seconds": 5, "stale_seconds": 15},
    )
    assert response.status_code == 200
    responses = await asyncio.gather(
        *(
            api.post("/api/tasks", json={"model": "test-image", "prompt": "a"})
            for _ in range(12)
        )
    )
    assert sum(r.status_code == 202 for r in responses) == 3
    assert all(r.status_code in (202, 429) for r in responses)
    backend.query_delay = 2
    before = time.monotonic()
    result = await p.registry.availability("test-image")
    assert result["status"] == "unavailable" and time.monotonic() - before < 1.2
    await p.dispatch()
    async with p.db.sessions() as db:
        rows = (await db.scalars(select(Task))).all()
    assert len(rows) == 3 and all(
        t.status == "pending" and t.attempt_count == 0 for t in rows
    )


@pytest.mark.asyncio
async def test_load_execute_persist_and_images_compatibility(setup):
    p, api, backend = setup
    assert (await api.get("/api/cluster/availability?model_id=test-image")).json()[
        "status"
    ] == "need_load"
    response = await api.post(
        "/v1/images/generations",
        headers={"Prefer": "respond-async"},
        json={
            "model": "test-image",
            "prompt": "a",
            "loras": [{"lora_id": "style", "scale": 0.8}],
        },
    )
    assert response.status_code == 202
    task_id = response.json()["id"]
    await drain(p)
    task = (await api.get("/api/tasks/" + task_id)).json()
    assert task["status"] == "succeeded" and task["node_id"] == "node-a"
    assert task["files"][0]["storage_key"].startswith("outputs/" + task_id + "/")
    assert (await api.get(task["files"][0]["url"])).content == png()
    sent = next(r for r in backend.requests if r.url.path == "/v1/images/generations")
    import json

    assert json.loads(sent.content)["expected_revision"] == "r1"
    assert (
        await api.get("/api/cluster/availability?model_id=test-image&lora_id=missing")
    ).json()["status"] == "unavailable"
    # Default compatibility must wait for persisted result and return 200, not 202.
    pending = asyncio.create_task(
        api.post("/v1/images/generations", json={"model": "test-image", "prompt": "b"})
    )
    await asyncio.sleep(0.1)
    await drain(p)
    response = await pending
    assert (
        response.status_code == 200
        and base64.b64decode(response.json()["data"][0]["b64_json"]) == png()
    )
    assert response.headers["x-task-id"]


@pytest.mark.asyncio
async def test_reference_image_and_video_flow(setup):
    p, api, backend = setup
    backend.models = [model(references=True)]
    await p.registry.sync()
    response = await api.post(
        "/v1/images/edits",
        headers={"Prefer": "respond-async"},
        data={"model": "test-image", "prompt": "edit"},
        files={"image[]": ("ref.png", png(), "image/png")},
    )
    assert response.status_code == 202
    await drain(p)
    assert (await api.get("/api/tasks/" + response.json()["id"])).json()[
        "status"
    ] == "succeeded"
    assert any(r.url.path == "/v1/images/edits" for r in backend.requests)
    backend.models = [model("video", True)]
    backend.loaded = None
    await p.registry.sync()
    uploaded = (
        await api.post("/api/uploads", files={"file": ("ref.png", png(), "image/png")})
    ).json()
    response = await api.post(
        "/api/tasks",
        json={
            "model": "test-video",
            "prompt": "animate",
            "references": [uploaded["id"]],
        },
    )
    assert response.status_code == 202
    await drain(p)
    task = (await api.get("/api/tasks/" + response.json()["id"])).json()
    assert task["status"] == "succeeded" and task["files"][0]["mime"] == "video/mp4"


@pytest.mark.asyncio
async def test_key_revocation_and_file_ownership(setup):
    p, api, backend = setup
    created = (await api.post("/api/admin/keys", json={"name": "client"})).json()
    other = {"Authorization": "Bearer " + created["key"]}
    assert (await api.get("/api/admin/settings", headers=other)).status_code == 403
    assert "key" not in (await api.get("/api/admin/keys")).json()["keys"][0]
    output = (
        await api.post("/api/uploads", files={"file": ("a.png", png(), "image/png")})
    ).json()
    assert (await api.get(output["url"], headers=other)).status_code == 404
    assert (
        await api.patch("/api/admin/keys/" + created["id"], json={"revoked": True})
    ).status_code == 200
    assert (await api.get("/api/models", headers=other)).status_code == 401
    assert (
        await api.patch("/api/admin/keys/" + created["id"], json={"enabled": True})
    ).status_code == 409


@pytest.mark.asyncio
async def test_timeout_quarantines_node_without_retry(setup):
    p, api, backend = setup
    backend.generate_timeout = True
    task_id = (
        await api.post("/api/tasks", json={"model": "test-image", "prompt": "a"})
    ).json()["id"]
    await drain(p)
    task = (await api.get("/api/tasks/" + task_id)).json()
    assert task["status"] == "unknown" and task["attempt_count"] == 1
    assert "node-a" in p.registry.quarantined
    await p.dispatch()
    assert (
        len([r for r in backend.requests if r.url.path == "/v1/images/generations"])
        == 1
    )


@pytest.mark.asyncio
async def test_release_only_after_idle_hour_and_no_waiting_tasks(setup):
    p, api, backend = setup
    backend.loaded = "test-image"
    await p.registry.sync()
    key = ("test-image", "r1")
    p.registry.last_invoked[key] = time.monotonic() - 3599
    await p.release_idle()
    assert backend.unloads == 0
    p.registry.last_invoked[key] = time.monotonic() - 3601
    await p.release_idle()
    assert backend.unloads == 1


@pytest.mark.asyncio
async def test_catalog_conflict_expiry_and_invalid_inputs(setup):
    p, api, backend = setup
    assert (
        await api.post(
            "/api/tasks", json={"model": "test-image", "prompt": "a", "seed": -1}
        )
    ).status_code == 400
    assert (
        await api.post(
            "/api/tasks", json={"model": "test-image", "prompt": "a", "size": 123}
        )
    ).status_code == 400
    assert (
        await api.post(
            "/api/tasks", json={"model": "test-image", "prompt": "a", "stream": True}
        )
    ).status_code == 400
    assert (await api.post("/api/tasks", json=[])).status_code == 400
    assert (await api.post("/api/tasks", content="{")).status_code == 400
    assert (
        await api.post(
            "/api/tasks",
            json={"model": "test-image", "prompt": "a", "references": ["missing"]},
        )
    ).status_code == 400
    snap = p.registry.snapshots["node-a"]
    p.registry.snapshots["node-b"] = {
        **snap,
        "models": [copy.deepcopy(backend.models[0])],
    }
    p.registry.snapshots["node-b"]["models"][0]["artifact_sha256"] = "3" * 64
    assert p.registry.models()[0]["status"] == "unavailable"
    p.registry.snapshots.pop("node-b")
    snap["observed"] = time.monotonic() - 16
    assert p.registry.models() == []


@pytest.mark.asyncio
async def test_one_slow_node_does_not_hide_a_healthy_node(setup):
    p, api, backend = setup
    async with p.db.sessions() as db:
        db.add(
            Node(
                id="slow-node",
                name="slow",
                base_url="http://slow",
                admin_secret=p.registry.encrypt("admin"),
                inference_secret=p.registry.encrypt("infer"),
            )
        )
        await db.commit()
    await p.registry.sync()
    backend.slow_host = "slow"
    backend.query_delay = 2
    start = time.monotonic()
    result = await p.registry.availability("test-image")
    assert result["node_id"] == "node-a" and result["status"] == "need_load"
    assert time.monotonic() - start < 1.2


@pytest.mark.asyncio
async def test_persistence_recovery_does_not_generate_twice(setup):
    p, api, backend = setup
    original_complete = p.complete

    async def fail_complete(manifest):
        raise RuntimeError("DB association temporarily unavailable")

    p.complete = fail_complete
    task_id = (
        await api.post("/api/tasks", json={"model": "test-image", "prompt": "a"})
    ).json()["id"]
    await drain(p)
    assert (await api.get("/api/tasks/" + task_id)).json()["status"] == "persisting"
    p.complete = original_complete
    await p.recover_artifacts()
    await p.recover_artifacts()
    assert (await api.get("/api/tasks/" + task_id)).json()["status"] == "succeeded"
    assert (
        len([r for r in backend.requests if r.url.path == "/v1/images/generations"])
        == 1
    )
    async with p.db.sessions() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(MediaFile)
                .where(MediaFile.role == "output")
            )
            == 1
        )


@pytest.mark.asyncio
async def test_restart_recovers_only_tasks_that_were_not_sent(setup):
    p, api, backend = setup
    first = (
        await api.post("/api/tasks", json={"model": "test-image", "prompt": "a"})
    ).json()["id"]
    second = (
        await api.post("/api/tasks", json={"model": "test-image", "prompt": "b"})
    ).json()["id"]
    async with p.db.sessions() as db:
        a = await db.get(Task, first)
        a.status = "loading"
        a.node_id = "node-a"
        b = await db.get(Task, second)
        b.status = "submitted"
        b.node_id = "node-a"
        await db.commit()
    await p.start()
    assert (await api.get("/api/tasks/" + first)).json()["status"] == "pending"
    assert (await api.get("/api/tasks/" + second)).json()["status"] == "unknown"
    assert "node-a" in p.registry.quarantined


@pytest.mark.asyncio
async def test_unavailable_prefix_does_not_starve_later_same_type_tasks(setup):
    p, api, backend = setup
    other = model()
    other["model_id"] = "other-image"
    other["configuration"]["model_id"] = "other-image"
    backend.models.append(other)
    await p.registry.sync()
    for _ in range(25):
        assert (
            await api.post("/api/tasks", json={"model": "test-image", "prompt": "wait"})
        ).status_code == 202
    target = (
        await api.post("/api/tasks", json={"model": "other-image", "prompt": "run"})
    ).json()["id"]
    backend.models = [other]
    await p.registry.sync()
    await drain(p)
    assert (await api.get("/api/tasks/" + target)).json()["status"] == "pending"
    await drain(p)
    assert (await api.get("/api/tasks/" + target)).json()["status"] == "succeeded"
