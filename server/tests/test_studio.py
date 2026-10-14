import pytest
from sqlalchemy import select

from platform_service.db import MediaFile, ModelDefault, Task, now, uid
from platform_service.service import Platform
from test_accounts import account, login
from test_platform import drain, setup  # noqa: F401
from test_platform import model


@pytest.mark.asyncio
async def test_model_defaults_persist_and_apply_to_new_tasks(setup):
    p, api, backend = setup
    values = dict(model_id="test-image", revision="r1", default_size="768x512",
                  default_steps=12, default_guidance_scale=2.5, default_seed=123)
    assert (await api.put("/api/admin/model-defaults", json=values)).status_code == 200
    await p.registry.sync()
    generation = (await api.get("/api/models")).json()["models"][0]["configuration"]["generation"]
    assert generation["default_steps"] == 12
    assert generation["default_seed"] == 123
    # A new service instance reads durable defaults, without a process-local cache.
    restarted = Platform(p.config)
    restarted.registry = p.registry
    try:
        assert (await restarted.models_with_defaults())[0]["configuration"]["generation"]["default_seed"] == 123
    finally:
        await restarted.close()
    first = (await api.post("/api/tasks", json={"model": "test-image", "prompt": "default settings"})).json()["id"]
    second = (await api.post("/api/tasks", json={"model": "test-image", "prompt": "custom settings", "seed": 0, "num_inference_steps": 6, "size": "512x512"})).json()["id"]
    async with p.db.sessions() as db:
        a, b = await db.get(Task, first), await db.get(Task, second)
        assert a.request_body["seed"] == 123 and a.request_body["num_inference_steps"] == 12
        assert a.request_body["size"] == "768x512" and a.request_body["guidance_scale"] == 2.5
        assert b.request_body["seed"] == 0 and b.request_body["num_inference_steps"] == 6
    for change in [{"default_steps": 101}, {"default_size": "999x512"}, {"default_seed": -1}, {"default_steps": True}, {"revision": "missing"}]:
        assert (await api.put("/api/admin/model-defaults", json=values | change)).status_code in (400, 404)
    assert (await api.put("/api/admin/model-defaults", json=values | {"default_seed": None})).status_code == 200
    third = (await api.post("/api/tasks", json={"model": "test-image", "prompt": "random seed"})).json()["id"]
    async with p.db.sessions() as db:
        assert "seed" not in (await db.get(Task, third)).request_body
        assert len((await db.scalars(select(ModelDefault))).all()) == 1
    # Defaults are scoped to the pinned model revision.
    backend.models[0]["revision"] = "r2"
    backend.models[0]["configuration"]["revision"] = "r2"
    await p.registry.sync()
    assert (await api.get("/api/models")).json()["models"][0]["configuration"]["generation"]["default_steps"] == 4


@pytest.mark.asyncio
async def test_history_filter_pagination_and_user_permissions(setup):
    p, api, _ = setup
    alice_account = await account(api, "alice")
    await account(api, "bobby")
    alice, bob = await login(api, "alice"), await login(api, "bobby")
    try:
        first = (await alice.post("/api/tasks", json={"model": "test-image", "prompt": "alice landscape"})).json()["id"]
        await drain(p)
        await alice.post("/api/tasks", json={"model": "test-image", "prompt": "alice second"})
        await bob.post("/api/tasks", json={"model": "test-image", "prompt": "bob picture"})
        result = (await api.get("/api/admin/history?q=alice&limit=1")).json()
        assert result["total"] == 2 and len(result["tasks"]) == 1
        assert result["tasks"][0]["username"] == "alice"
        exact = (await api.get("/api/admin/history?user_id=" + alice_account["id"] + "&q=bobby")).json()
        assert exact["total"] == 2 and all(t["username"] == "alice" for t in exact["tasks"])
        next_page = (await api.get("/api/admin/history?q=alice&limit=1&offset=1")).json()
        assert next_page["tasks"][0]["id"] != result["tasks"][0]["id"]
        completed = (await api.get("/api/admin/history?q=alice&status=succeeded")).json()
        assert completed["total"] == 1 and completed["tasks"][0]["id"] == first
        assert completed["tasks"][0]["prompt"] == "alice landscape"
        assert completed["tasks"][0]["parameters"]["size"] == "512x512"
        assert completed["tasks"][0]["parameters"]["seed"] == 42
        assert (await alice.get("/api/admin/history")).status_code == 403
        assert (await bob.put("/api/admin/model-defaults", json={})).status_code == 403
        assert len((await alice.get("/api/tasks?mine=true")).json()["tasks"]) == 2
        assert (await api.get("/api/tasks?mine=true")).json()["tasks"] == []
        assert (await api.get("/api/admin/history?q=absent")).json()["total"] == 0
        assert (await api.get("/api/admin/history?limit=0")).status_code == 400
    finally:
        await alice.aclose()
        await bob.aclose()


@pytest.mark.asyncio
async def test_loras_are_bound_to_model_and_revision(setup):
    p, api, backend = setup
    second = model("video")
    second["loras"][0]["lora_id"] = "video-style"
    backend.models.append(second)
    # A node cannot declare a foreign or stale LoRA under the current model.
    backend.models[0]["loras"].extend([
        {"lora_id": "wrong-model", "model_id": "test-video", "sha256": "3" * 64},
        {"lora_id": "old-revision", "base_model_revision": "r0", "sha256": "4" * 64},
    ])
    await p.registry.sync()
    models = (await api.get("/api/models")).json()["models"]
    image = next(m for m in models if m["model_id"] == "test-image")
    assert [l["lora_id"] for l in image["loras"]] == ["style"]
    assert image["loras"][0]["model_id"] == "test-image"
    assert image["loras"][0]["base_model_revision"] == "r1"
    for model_id, lora_id in [("test-video", "style"), ("test-image", "video-style"), ("test-image", "wrong-model"), ("test-image", "old-revision")]:
        response = await api.post("/api/tasks", json={"model": model_id, "prompt": "incompatible LoRA", "loras": [{"lora_id": lora_id}]})
        assert response.status_code == 400 and response.json()["error"]["code"] == "invalid_lora"
    accepted = await api.post("/api/tasks", json={"model": "test-image", "prompt": "correct LoRA", "loras": [{"lora_id": "style"}]})
    assert accepted.status_code == 202
    # Binding must also hold if a node changes its catalogue after queueing.
    backend.models[0]["loras"][0]["model_id"] = "test-video"
    await p.execute(accepted.json()["id"], "node-a", True)
    result = (await api.get("/api/tasks/" + accepted.json()["id"])).json()
    assert result["status"] == "pending"
    assert result["error"] == "Pinned model or LoRA is no longer present"
    assert not any(r.url.path == "/v1/images/generations" for r in backend.requests)


@pytest.mark.asyncio
async def test_personal_gallery_pagination_and_recent_limit(setup):
    from datetime import timedelta
    p, api, _ = setup
    alice_account = await account(api, "gallery-alice")
    alice = await login(api, "gallery-alice")
    admin_owner = (await api.get("/api/session")).json()["user_id"]
    alice_owner = "user:" + alice_account["id"]
    async with p.db.sessions() as db:
        for index in range(55):
            owner = alice_owner if index < 52 else "user:" + admin_owner
            task = Task(id=uid(), type="image", status="succeeded", owner=owner,
                        model_id="test-image", model_revision="r1", model_sha256="1" * 64,
                        capabilities=model()["configuration"], request_body={"prompt": f"artwork {index}", "size": "512x512"},
                        created_at=now() - timedelta(seconds=index))
            db.add(task)
            await db.flush()
            db.add(MediaFile(task_id=task.id, owner=owner, role="output", storage_key=f"gallery/{task.id}.png", mime="image/png", size=100, sha256="2" * 64))
        db.add(Task(id=uid(), type="image", status="pending", owner=alice_owner,
                    model_id="test-image", model_revision="r1", model_sha256="1" * 64,
                    capabilities=model()["configuration"], request_body={"prompt": "still waiting"}))
        await db.commit()
    try:
        pages = [(await alice.get(f"/api/history?limit=24&offset={offset}")).json() for offset in (0, 24, 48)]
        assert [len(page["tasks"]) for page in pages] == [24, 24, 4]
        assert all(page["total"] == 52 for page in pages)
        ids = [t["id"] for page in pages for t in page["tasks"]]
        assert len(set(ids)) == 52
        assert all(t["status"] == "succeeded" and t["files"] for page in pages for t in page["tasks"])
        assert all("owner" not in t for page in pages for t in page["tasks"])
        assert (await api.get("/api/history")).json()["total"] == 3
        recent = (await alice.get("/api/tasks?mine=true&limit=5")).json()["tasks"]
        assert len(recent) == 5 and recent[0]["prompt"] == "still waiting"
        assert (await alice.get("/api/history?limit=0")).status_code == 400
        assert (await alice.get("/api/history?offset=-1")).status_code == 400
        assert (await alice.get("/api/tasks?limit=0")).status_code == 400
    finally:
        await alice.aclose()
