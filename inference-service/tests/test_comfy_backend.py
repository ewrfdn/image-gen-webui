import io
import json
from pathlib import Path

import pytest
from PIL import Image

from inference_service.backends.comfyui.backend import ComfyUIBackend
from inference_service.backends.factory import CAPABILITIES, create_backend
from inference_service.config import Settings
from inference_service.engine.registry import Registry
from inference_service.resources import Checkpoint
from inference_service.schemas import GenerationRequest

CAPS = CAPABILITIES["minimax_h3_image"]


def png_bytes(size=(8, 8), color="red"):
    output = io.BytesIO()
    Image.new("RGB", size, color).save(output, format="PNG")
    return output.getvalue()


class FakeClient:
    """Stands in for ComfyUIClient; records the workflow it receives."""

    def __init__(self):
        self.timeout = 60.0
        self.graph = None
        self.uploads = []
        self.freed = False

    def object_info(self, *node_ids):
        info = {node: {"input": {"required": {}}} for node in node_ids}
        info["UNETLoader"]["input"]["required"]["unet_name"] = [["pruned.safetensors"]]
        info["CLIPLoader"]["input"]["required"]["clip_name"] = [["te.safetensors"]]
        info["VAELoader"]["input"]["required"]["vae_name"] = [["vae.safetensors"]]
        return info

    def choice_values(self, node_info, field):
        spec = node_info["input"]["required"].get(field)
        return list(spec[0]) if spec else []

    def upload_image(self, name, png):
        self.uploads.append((name, png))
        return name

    def enqueue(self, graph):
        self.graph = graph
        return "prompt-1"

    def wait(self, prompt_id, deadline):
        return {"outputs": {"save": {"images": [{"filename": "out.png", "subfolder": "", "type": "output"}]}}}

    def image(self, filename, subfolder="", folder_type="output"):
        return png_bytes()

    def free(self):
        self.freed = True


@pytest.fixture
def comfy_checkpoint(tmp_path: Path) -> Checkpoint:
    root = tmp_path / "models"
    directory = root / "minimax-h3-image"
    directory.mkdir(parents=True)
    (directory / "comfy-model.json").write_text(json.dumps({
        "schema_version": 1,
        "architecture": "minimax_h3_image",
        "backend": "comfyui",
        "models": {"unet": "pruned.safetensors", "clip": "te.safetensors", "vae": "vae.safetensors"},
        "clip_type": "minimax",
        "sampling": {"steps": 6, "cfg": 1.0, "sampler_name": "euler", "scheduler": "simple"},
    }))
    return Registry(root).checkpoints()["minimax-h3-image"]


def loaded(checkpoint, client):
    backend = ComfyUIBackend(CAPS, "http://127.0.0.1:8188", client=client)
    backend.load(checkpoint)
    return backend


def test_comfy_checkpoint_is_discovered(comfy_checkpoint):
    assert comfy_checkpoint.layout == "comfy"
    assert comfy_checkpoint.architecture == "minimax_h3_image"
    assert comfy_checkpoint.availability == "available"
    assert comfy_checkpoint.public()["format"] == "comfyui"


def test_comfy_descriptor_with_path_like_name_is_invalid(tmp_path):
    root = tmp_path / "models"
    directory = root / "bad"
    directory.mkdir(parents=True)
    (directory / "comfy-model.json").write_text(json.dumps({
        "schema_version": 1, "architecture": "minimax_h3_image", "backend": "comfyui",
        "models": {"unet": "../../etc/passwd", "clip": "te.safetensors", "vae": "vae.safetensors"},
    }))
    assert Registry(root).checkpoints() == {}
    invalid = Registry(root).checkpoints(include_invalid=True)["bad"]
    assert invalid.reason == "comfy_descriptor_invalid"


def test_factory_selects_comfy_backend(comfy_checkpoint, tmp_path):
    settings = Settings(tmp_path, "i", "a", "cpu", "float32", comfyui_url="http://127.0.0.1:8188")
    backend = create_backend(comfy_checkpoint, settings)
    assert isinstance(backend, ComfyUIBackend)
    assert backend.client.base_url == "http://127.0.0.1:8188"


def test_generate_builds_six_step_workflow(comfy_checkpoint):
    client = FakeClient()
    backend = loaded(comfy_checkpoint, client)
    request = GenerationRequest(model="minimax-h3-image", prompt="a fox", size="3104x3104")
    assert backend.generate(request, None, 7) == png_bytes()
    graph = client.graph
    assert graph["sampler"]["inputs"]["steps"] == 6
    assert graph["sampler"]["inputs"]["cfg"] == 1.0
    assert graph["sampler"]["inputs"]["seed"] == 7
    assert graph["conditioning"]["inputs"]["width"] == 3104
    assert graph["negative"]["class_type"] == "ConditioningZeroOut"
    assert graph["unet"]["inputs"]["unet_name"] == "pruned.safetensors"


def test_generate_uploads_references(comfy_checkpoint):
    client = FakeClient()
    backend = loaded(comfy_checkpoint, client)
    reference = png_bytes((8, 8), "blue")
    import base64
    request = GenerationRequest(model="minimax-h3-image", prompt="edit <Picture 1>",
                                size="2048x2048", image=[base64.b64encode(reference).decode()])
    backend.generate(request, None, 1)
    assert client.uploads and client.uploads[0][0] == "image-gen-1.png"
    assert client.graph["conditioning"]["inputs"]["image_1"] == ["load1", 0]
    assert client.graph["load1"]["class_type"] == "LoadImage"


def test_backend_rejects_unsupported_parameters(comfy_checkpoint):
    from inference_service.backends.base import UnsupportedParameter

    backend = loaded(comfy_checkpoint, FakeClient())
    with pytest.raises(UnsupportedParameter):
        backend.generate(GenerationRequest(model="m", prompt="p", size="2048x2048",
                                           num_inference_steps=12), None, 1)
    with pytest.raises(UnsupportedParameter):
        backend.generate(GenerationRequest(model="m", prompt="p", size="2048x2048",
                                           guidance_scale=4.0), None, 1)
    with pytest.raises(UnsupportedParameter):
        backend.generate(GenerationRequest(model="m", prompt="p", size="2048x2048",
                                           negative_prompt="blurry"), None, 1)


def test_load_rejects_missing_model_file(comfy_checkpoint, tmp_path):
    from inference_service.backends.comfyui.client import ComfyUIUnavailable

    class Missing(FakeClient):
        def choice_values(self, node_info, field):
            return ["other.safetensors"]

    with pytest.raises(ComfyUIUnavailable, match="pruned.safetensors"):
        loaded(comfy_checkpoint, Missing())


def test_unload_frees_comfyui(comfy_checkpoint):
    client = FakeClient()
    backend = loaded(comfy_checkpoint, client)
    backend.unload()
    assert client.freed is True


def test_engine_parameters_follow_capabilities():
    from inference_service.engine.service import Engine, ServiceError

    ok = GenerationRequest(model="m", prompt="p", size="2048x2048")
    Engine._validate_parameters(CAPS, ok)
    for bad, message in (
        (GenerationRequest(model="m", prompt="p", size="2048x2048", num_inference_steps=20), "num_inference_steps"),
        (GenerationRequest(model="m", prompt="p", size="2048x2048", guidance_scale=2.0), "guidance_scale"),
        (GenerationRequest(model="m", prompt="p", size="2048x2048", negative_prompt="x"), "negative_prompt"),
        (GenerationRequest(model="m", prompt="p", size="2048x2048",
                           loras=[{"lora_id": "style", "scale": 1.0}]), "LoRA"),
    ):
        with pytest.raises(ServiceError) as error:
            Engine._validate_parameters(CAPS, bad)
        assert error.value.code == "unsupported_parameter"
        assert message in error.value.message


def api_client(tmp_path, backend):
    from fastapi.testclient import TestClient
    from inference_service.app import create_app
    from inference_service.config import Settings

    root = tmp_path / "api-models"
    directory = root / "minimax-h3-image"
    directory.mkdir(parents=True)
    (directory / "comfy-model.json").write_text(json.dumps({
        "schema_version": 1, "architecture": "minimax_h3_image", "backend": "comfyui",
        "models": {"unet": "pruned.safetensors", "clip": "te.safetensors", "vae": "vae.safetensors"},
        "sampling": {"steps": 6, "cfg": 1.0, "sampler_name": "euler", "scheduler": "simple"},
    }))
    settings = Settings(root, "infer-secret", "admin-secret", "cpu", "float32", comfyui_url="http://127.0.0.1:1")
    return TestClient(create_app(settings, lambda checkpoint: backend)), root


def test_api_reports_backend_outage_on_load_as_503(tmp_path):
    from inference_service.backends.base import BackendUnavailable

    class Down:
        def load(self, checkpoint):
            raise BackendUnavailable("ComfyUI /object_info is unreachable")

        def generate(self, request, lora, seed):
            raise AssertionError("must not be called")

        def unload(self):
            pass

    client, _ = api_client(tmp_path, Down())
    with client:
        response = client.post("/internal/v1/models/minimax-h3-image/load",
                               headers={"Authorization": "Bearer admin-secret"})
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "backend_unavailable"
        assert response.headers["Retry-After"] == "5"
        # The slot must not stay poisoned: a later load can succeed.
        assert client.get("/internal/v1/models", headers={"Authorization": "Bearer admin-secret"}).json()["models"][0]["state"] == "unloaded"


def test_api_reports_backend_outage_on_generation_as_503(tmp_path):
    from inference_service.backends.base import BackendUnavailable

    class HalfUp:
        def load(self, checkpoint):
            pass

        def generate(self, request, lora, seed):
            raise BackendUnavailable("ComfyUI /prompt is unreachable")

        def unload(self):
            pass

    client, _ = api_client(tmp_path, HalfUp())
    with client:
        admin = {"Authorization": "Bearer admin-secret"}
        assert client.post("/internal/v1/models/minimax-h3-image/load", headers=admin).status_code == 200
        response = client.post("/v1/images/generations", headers={"Authorization": "Bearer infer-secret"},
                               json={"model": "minimax-h3-image", "prompt": "x", "size": "2048x2048"})
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "backend_unavailable"
        snapshot = client.get("/internal/v1/models", headers=admin).json()["models"][0]
        assert snapshot["state"] == "ready" and snapshot["can_generate"] is True


def test_api_maps_comfy_out_of_memory_to_gpu_oom(tmp_path):
    class Oom:
        def load(self, checkpoint):
            pass

        def generate(self, request, lora, seed):
            raise RuntimeError("ComfyUI ran out of memory: Allocation on device 0 would exceed allowed memory")

        def unload(self):
            pass

    client, _ = api_client(tmp_path, Oom())
    with client:
        admin = {"Authorization": "Bearer admin-secret"}
        assert client.post("/internal/v1/models/minimax-h3-image/load", headers=admin).status_code == 200
        response = client.post("/v1/images/generations", headers={"Authorization": "Bearer infer-secret"},
                               json={"model": "minimax-h3-image", "prompt": "x", "size": "4096x4096"})
        assert response.status_code == 500
        assert response.json()["error"]["code"] == "gpu_oom"


def test_warmup_renders_at_minimum_size(comfy_checkpoint):
    client = FakeClient()
    backend = ComfyUIBackend(CAPS, "http://127.0.0.1:8188", warmup=True, client=client)
    backend.load(comfy_checkpoint)
    assert client.graph is not None
    assert client.graph["conditioning"]["inputs"]["width"] == CAPS.min_size
    assert client.graph["sampler"]["inputs"]["steps"] == 6


def test_execution_error_message_is_propagated(monkeypatch):
    from inference_service.backends.comfyui.client import ComfyUIClient, ComfyUIUnavailable

    client = ComfyUIClient("http://127.0.0.1:1")
    entry = {"status": {"status_str": "error", "messages": [
        ["execution_start", {}],
        ["execution_error", {"exception_message": "CUDA error: out of memory"}]]}}

    monkeypatch.setattr(client, "_request", lambda *a, **k: json.dumps({"prompt-1": entry}).encode())
    with pytest.raises(RuntimeError, match="out of memory"):
        client.wait("prompt-1", deadline=0)

    entry["status"]["messages"][1][1]["exception_message"] = "ValueError: bad shape"
    with pytest.raises(ComfyUIUnavailable, match="bad shape"):
        client.wait("prompt-1", deadline=0)
