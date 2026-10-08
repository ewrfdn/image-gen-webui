import base64
import threading

from fastapi.testclient import TestClient


def test_discovery_auth_and_lifecycle(project, admin_headers, infer_headers):
    app, backend, root = project
    with TestClient(app) as client:
        assert client.get("/health/ready").status_code == 200
        assert client.get("/internal/v1/checkpoints").status_code == 401
        catalog = client.get("/internal/v1/checkpoints", headers=admin_headers).json()
        assert [item["model_id"] for item in catalog["checkpoints"]] == ["sdxl-base"]
        loras = client.get("/internal/v1/checkpoints/sdxl-base/loras", headers=admin_headers).json()
        assert [item["lora_id"] for item in loras["loras"]] == ["style"]
        assert client.get("/v1/models", headers=infer_headers).json()["data"][0]["id"] == "sdxl-base"
        unloaded = client.post("/v1/images/generations", headers=infer_headers,
                               json={"model": "sdxl-base", "prompt": "test"})
        assert unloaded.status_code == 409
        assert unloaded.json()["error"]["code"] == "model_not_loaded"
        assert client.post("/internal/v1/models/sdxl-base/load", headers=admin_headers).status_code == 200
        assert client.post("/internal/v1/models/sdxl-base/load", headers=admin_headers).status_code == 200
        assert backend.loads == 1
        assert client.get("/internal/v1/models?loaded=true", headers=admin_headers).json()["models"][0]["can_generate"]
        assert client.post("/internal/v1/models/sdxl-base/unload", headers=admin_headers).status_code == 200
        assert client.post("/internal/v1/models/sdxl-base/unload", headers=admin_headers).status_code == 200


def test_lora_request_isolation_and_png(project, admin_headers, infer_headers):
    app, backend, _ = project
    with TestClient(app) as client:
        client.post("/internal/v1/models/sdxl-base/load", headers=admin_headers)
        for loras in ([], [{"lora_id": "style", "scale": 0.4}], []):
            response = client.post("/v1/images/generations", headers=infer_headers,
                json={"model": "sdxl-base", "prompt": "cabin", "seed": 42, "loras": loras})
            assert response.status_code == 200, response.text
            body = response.json()
            assert base64.b64decode(body["data"][0]["b64_json"]).startswith(b"\x89PNG")
            assert body["metadata"]["seed"] == 42
            assert body["metadata"]["request_id"] == response.headers["x-request-id"]
        assert [call[0] for call in backend.calls] == [None, "style", None]


def test_busy_health_and_unload_conflict(project, admin_headers, infer_headers):
    app, backend, _ = project
    backend.wait_started = threading.Event()
    backend.wait_release = threading.Event()
    with TestClient(app) as client:
        client.post("/internal/v1/models/sdxl-base/load", headers=admin_headers)
        result = []
        thread = threading.Thread(target=lambda: result.append(client.post("/v1/images/generations",
            headers=infer_headers, json={"model": "sdxl-base", "prompt": "first"})))
        thread.start()
        assert backend.wait_started.wait(3)
        assert client.get("/health/live").status_code == 200
        assert client.get("/health/ready").status_code == 200
        busy = client.post("/v1/images/generations", headers=infer_headers,
                           json={"model": "sdxl-base", "prompt": "second"})
        assert busy.status_code == 503
        assert busy.json()["error"]["code"] == "engine_busy"
        assert busy.headers["retry-after"] == "1"
        unload = client.post("/internal/v1/models/sdxl-base/unload", headers=admin_headers)
        assert unload.status_code == 409
        assert unload.json()["error"]["code"] == "model_in_use"
        backend.wait_release.set()
        thread.join(4)
        assert result[0].status_code == 200


def test_invalid_requests_and_poisoned_engine(project, admin_headers, infer_headers):
    app, backend, root = project
    with TestClient(app) as client:
        client.post("/internal/v1/models/sdxl-base/load", headers=admin_headers)
        invalid = client.post("/v1/images/generations", headers=infer_headers,
                              json={"model": "sdxl-base", "prompt": "x", "size": "100x100"})
        assert invalid.status_code == 400
        assert invalid.json()["error"]["code"] == "invalid_request"
        missing = client.post("/v1/images/generations", headers=infer_headers,
                              json={"model": "sdxl-base", "prompt": "x", "loras": [{"lora_id": "other"}]})
        assert missing.status_code == 404
        assert backend.calls == []
        backend.fail_cleanup = True
        failure = client.post("/v1/images/generations", headers=infer_headers,
                              json={"model": "sdxl-base", "prompt": "x", "loras": [{"lora_id": "style"}]})
        assert failure.status_code == 500
        assert failure.json()["error"]["code"] == "adapter_cleanup_failed"
        assert client.get("/health/ready").status_code == 503
        assert client.post("/v1/images/generations", headers=infer_headers,
                           json={"model": "sdxl-base", "prompt": "x"}).status_code == 503


def test_unhealthy_backend_requires_unload(project, admin_headers, infer_headers):
    from inference_service.backends.base import BackendUnhealthy

    app, backend, _ = project
    def fail(*args):
        raise BackendUnhealthy("private process exited")
    backend.generate = fail
    with TestClient(app) as client:
        assert client.post("/internal/v1/models/sdxl-base/load", headers=admin_headers).status_code == 200
        response = client.post("/v1/images/generations", headers=infer_headers,
                               json={"model": "sdxl-base", "prompt": "x"})
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "backend_unhealthy"
        assert client.get("/health/ready").status_code == 503
        assert client.post("/internal/v1/models/sdxl-base/unload", headers=admin_headers).status_code == 200


def test_file_change_blocks_new_generation(project, admin_headers, infer_headers):
    app, _, root = project
    with TestClient(app) as client:
        client.post("/internal/v1/models/sdxl-base/load", headers=admin_headers)
        (root / "sdxl-base" / "model" / "unet" / "new.safetensors").write_text("changed")
        response = client.post("/v1/images/generations", headers=infer_headers,
                               json={"model": "sdxl-base", "prompt": "x"})
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "resource_changed"
        assert client.get("/internal/v1/models?loaded=true", headers=admin_headers).json()["models"][0]["source_status"] == "changed"


def test_removed_loaded_source_stays_visible_until_unload(project, admin_headers, infer_headers):
    app, _, root = project
    with TestClient(app) as client:
        client.post("/internal/v1/models/sdxl-base/load", headers=admin_headers)
        (root / "sdxl-base" / "model" / "model_index.json").unlink()
        loaded = client.get("/internal/v1/models?loaded=true", headers=admin_headers).json()["models"]
        assert loaded[0]["source_status"] == "missing"
        response = client.post("/v1/images/generations", headers=infer_headers,
                               json={"model": "sdxl-base", "prompt": "x"})
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "resource_changed"
        assert client.post("/internal/v1/models/sdxl-base/unload", headers=admin_headers).status_code == 200
