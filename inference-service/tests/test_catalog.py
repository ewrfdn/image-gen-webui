import json
from fastapi.testclient import TestClient
from inference_service.engine.catalog import configuration
from scripts.build_catalog import build


def test_catalog_requires_json_and_pins_revision(project, admin_headers, infer_headers):
    app, backend, root = project
    with TestClient(app) as client:
        assert client.get("/internal/v1/catalog").status_code == 401
        empty = client.get("/internal/v1/catalog", headers=admin_headers).json()
        assert (
            empty["models"] == []
            and empty["errors"][0]["reason"] == "model_config_missing"
        )
        build(root)
        catalog = client.get("/internal/v1/catalog", headers=admin_headers).json()
        assert len(catalog["models"]) == 1
        model = catalog["models"][0]
        assert model["configuration"]["reference"]["supported"] is False
        assert model["loras"][0]["sha256"]
        client.post("/internal/v1/models/sdxl-base/load", headers=admin_headers)
        result = client.post(
            "/v1/images/generations",
            headers=infer_headers,
            json={"model": "sdxl-base", "prompt": "a", "expected_revision": "wrong"},
        )
        assert (
            result.status_code == 409
            and result.json()["error"]["code"] == "resource_changed"
        )
        result = client.post(
            "/v1/images/generations",
            headers=infer_headers,
            json={
                "model": "sdxl-base",
                "prompt": "a",
                "expected_revision": model["revision"],
            },
        )
        assert result.status_code == 200
        result = client.post(
            "/v1/images/generations",
            headers=infer_headers,
            json={
                "model": "sdxl-base",
                "prompt": "a",
                "expected_revision": model["revision"],
                "loras": [{"lora_id": "style"}],
                "expected_lora_sha256": {"style": "0" * 64},
            },
        )
        assert (
            result.status_code == 409
            and result.json()["error"]["code"] == "resource_changed"
        )


def test_config_cannot_claim_unimplemented_reference_support(project, admin_headers):
    app, backend, root = project
    build(root)
    path = root / "sdxl-base" / "model-config.json"
    data = json.loads(path.read_text())
    data["reference"] = {
        "supported": True,
        "mime_types": ["image/png"],
        "min_count": 0,
        "max_count": 1,
        "max_file_bytes": 1000,
    }
    path.write_text(json.dumps(data))
    with TestClient(app) as client:
        catalog = client.get("/internal/v1/catalog", headers=admin_headers).json()
        assert catalog["models"] == []
        assert catalog["errors"][0]["reason"] == "reference_backend_not_implemented"
